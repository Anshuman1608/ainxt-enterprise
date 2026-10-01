# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt MODEL ROUTER
# Signal-based routing — approved models only.
#
# Routing table:
#   simple   → Local LLM (in-house)        private, free, low-latency
#   medium   → GPT-5.4                    coding, reasoning, agents
#   complex  → Claude Sonnet 4.6          complex reasoning, SDLC
#   deep     → GPT-5-5                    latest OpenAI (explicit selection)
#   solution → Claude Opus 4.7            final synthesis (explicit selection)
#   opus-4-8 → Claude Opus 4.8            CLI/IDE opt-in
#   opus-5   → Claude Opus 5              CLI/IDE opt-in (ENABLE_CLI_OPUS_5)
#   vision   → Gemini 2.5 Flash           image / visual tasks (auto-detected)
#   gemini   → Gemini 2.5 Flash           explicit Gemini selection (text)
#
# BLOCKED: claude-opus-4-5 and older, GPT-5.2 Pro, GPT-5.2
#
# Fallback chains (primary unavailable):
#   simple   → Local → GPT-5 mini → Claude Sonnet → error
#   medium   → GPT-5.4 → Claude Sonnet → error
#   complex  → Claude Sonnet → GPT-5.4 → error
#   deep     → GPT-5-5 → Claude Sonnet → error
#   solution → Claude Opus 4.7 → Claude Sonnet → error
#   opus-4-8 → Claude Opus 4.8 → Claude Sonnet → error
#   opus-5   → Claude Opus 5   → Claude Sonnet → error
#   vision   → Gemini → Claude Sonnet → error
#
# Signals evaluated (in priority order):
#   1. caller model_hint
#   2. vision keyword detection
#   3. complexity classifier (Redis-cached)
#
# Public API:
#   model_router.generate(prompt, model_hint=None) -> str
#   model_router.stream(prompt, model_hint=None)   -> Generator[str, None, None]
#   model_router.route(prompt, model_hint=None)    -> RoutingDecision
# ============================================================

import inspect
import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional, Union

from core.logger import logger
from core.proxy_tool_use import llm_proxy_headers as _llm_proxy_headers
# core.tiers is a leaf module (stdlib only) — safe to import at module scope.
from core.tiers import Tier

# core.tier_resolver reaches the DB and the registry, so it is imported lazily
# inside route(). This one constant is duplicated rather than imported to keep
# the module-scope import surface as narrow as it was before Phase 5; a test
# asserts the two agree (tests/models/test_tier_switchover.py).
_ROLE_REVIEW = "review"
from core.model_registry import (
    OPENAI_SIMPLE_MODEL,
    OPENAI_CODING_MODEL,
    OPENAI_LATEST_MODEL,
    OPENAI_TERA_MODEL,
    OPENAI_LUNA_MODEL,
    OPENAI_OSS_MODEL,
    CHAT_FALLBACK_CHAIN,
    CLAUDE_PRIMARY_MODEL,
    CLAUDE_HAIKU,
    CLAUDE_OPUS_MODEL,
    CLAUDE_OPUS_46_MODEL,
    CLAUDE_OPUS_48_MODEL,
    CLAUDE_OPUS_5_MODEL,
    CLAUDE_SONNET_5_MODEL,
    ENABLE_OPUS,
    ENABLE_GPT56_TERA,
    ENABLE_GPT56_LUNA,
    SOLUTION_MODEL,
    GEMINI_VISION_MODEL,
    GEMINI_TEXT_MODEL,
    GEMINI_CODING_LITE_MODEL,
    GEMINI_IMAGE_MODEL,
    BLOCKED_MODELS,
    CLAUDE_PRIMARY_DISPLAY,
    CLAUDE_HAIKU_DISPLAY,
    CLAUDE_OPUS_DISPLAY,
    CLAUDE_OPUS_48_DISPLAY,
    CLAUDE_OPUS_5_DISPLAY,
    CLAUDE_SONNET_5_DISPLAY,
    OPENAI_CODING_DISPLAY,
    OPENAI_SIMPLE_DISPLAY,
    OPENAI_LATEST_DISPLAY,
    OPENAI_TERA_DISPLAY,
    OPENAI_LUNA_DISPLAY,
    OPENAI_OSS_DISPLAY,
    GEMINI_DISPLAY,
    GEMINI_TEXT_DISPLAY,
    GEMINI_CODING_LITE_DISPLAY,
    GEMINI_IMAGE_DISPLAY,
    LOCAL_LLM_DISPLAY,
)
from core.circuit_breaker import get_breaker

# ── LLM Proxy config ──────────────────────────────────────────
# When set, external model calls (OpenAI / Claude / Gemini) are forwarded
# to the LLM proxy service instead of calling APIs directly.
# In prod: LLM_PROXY_URL=http://your-llm-proxy:8003
# Leave empty to call APIs directly.
def _llm_proxy_url() -> str:
    return os.getenv("LLM_PROXY_URL", "").rstrip("/")

def _llm_timeout() -> float:
    """Total read timeout for LLM calls (seconds). Set LLM_TIMEOUT_SEC=0 to disable."""
    v = os.getenv("LLM_TIMEOUT_SEC", "300")
    f = float(v)
    return None if f <= 0 else f


# Persistent connection pool for all LLM proxy calls.
# A new httpx.Client per request forces a fresh TCP handshake every time
# and hard-caps throughput at ~40–50 concurrent (proxy thread ceiling).
# This singleton keeps connections alive across requests so the proxy
# can serve hundreds of concurrent calls with no per-request setup overhead.
import httpx as _httpx_mod
_PROXY_CLIENT_LOCK = threading.Lock()
_PROXY_CLIENT: "_httpx_mod.Client | None" = None


def _get_proxy_client() -> "_httpx_mod.Client":
    """Return the module-level persistent httpx.Client for LLM proxy calls."""
    global _PROXY_CLIENT
    if _PROXY_CLIENT is None:
        with _PROXY_CLIENT_LOCK:
            if _PROXY_CLIENT is None:
                _t = _llm_timeout()
                _PROXY_CLIENT = _httpx_mod.Client(
                    headers=_llm_proxy_headers(),
                    timeout=_httpx_mod.Timeout(_t, connect=10.0),
                    # trust_env=False: the gateway→LLM-proxy hop is INTERNAL.
                    # It must NOT be routed through the Squid HTTPS_PROXY (which is
                    # for outbound cloud APIs only) — Squid buffers the SSE/ndjson
                    # response, so cloud-model tokens arrive all-at-once instead of
                    # streaming. Bypassing env proxies restores per-token streaming.
                    trust_env=False,
                    limits=_httpx_mod.Limits(
                        max_connections=200,
                        max_keepalive_connections=100,
                        keepalive_expiry=30.0,
                    ),
                )
                logger.info(
                    "ModelRouter: proxy HTTP client initialised "
                    "(max_conn=200, keepalive=100, timeout=%s)", _t
                )
    return _PROXY_CLIENT


# ── Async HTTP client lifecycle ───────────────────────────────
# RQ workers are SYNC processes; each job may run asyncio.run() which creates
# a FRESH event loop.  A module-level AsyncClient is bound to the event loop
# that created it — reusing it in a new loop raises "Event loop is closed".
#
# Fix: by default (SDLC_PER_LOOP_HTTP_CLIENT != "0") we create a fresh
# AsyncClient per async_generate() call.  Per-call clients are cheap for the
# low-volume async IDE path and are loop-agnostic.  The old shared-singleton
# path (SDLC_PER_LOOP_HTTP_CLIENT=0) is kept for easy rollback if the IDE
# endpoint ever needs high-concurrency connection reuse.
import asyncio as _asyncio

def _use_per_call_async_client() -> bool:
    """Safe default: True (per-call). Set SDLC_PER_LOOP_HTTP_CLIENT=0 to use shared."""
    return os.getenv("SDLC_PER_LOOP_HTTP_CLIENT", "1") != "0"

# Shared singleton — only used when SDLC_PER_LOOP_HTTP_CLIENT=0.
_ASYNC_PROXY_CLIENT: "_httpx_mod.AsyncClient | None" = None
_ASYNC_PROXY_CLIENT_LOCK = threading.Lock()


def _get_async_proxy_client() -> "_httpx_mod.AsyncClient":
    """Return the shared httpx.AsyncClient (legacy mode, SDLC_PER_LOOP_HTTP_CLIENT=0).

    WARNING: The returned client is bound to the event loop that created it.
    Reusing it across different event loops (e.g. RQ worker asyncio.run() calls)
    raises 'Event loop is closed'.  Use _make_async_client() instead for the
    safe per-call path.
    """
    global _ASYNC_PROXY_CLIENT
    if _ASYNC_PROXY_CLIENT is None:
        with _ASYNC_PROXY_CLIENT_LOCK:
            if _ASYNC_PROXY_CLIENT is None:
                _t = _llm_timeout()
                _ASYNC_PROXY_CLIENT = _httpx_mod.AsyncClient(
                    headers=_llm_proxy_headers(),
                    timeout=_httpx_mod.Timeout(_t, connect=10.0),
                    limits=_httpx_mod.Limits(
                        max_connections=500,
                        max_keepalive_connections=200,
                        keepalive_expiry=30.0,
                    ),
                )
                logger.info(
                    "ModelRouter: async proxy HTTP client initialised "
                    "(max_conn=500, keepalive=200, timeout=%s)", _t
                )
    return _ASYNC_PROXY_CLIENT


def _make_async_client() -> "_httpx_mod.AsyncClient":
    """Create a fresh httpx.AsyncClient bound to the CURRENT event loop.

    Always safe for RQ workers (each asyncio.run() = new loop).
    The caller must use it as an async context manager or call aclose() after use.
    """
    _t = _llm_timeout()
    return _httpx_mod.AsyncClient(
        timeout=_httpx_mod.Timeout(_t, connect=10.0),
        limits=_httpx_mod.Limits(
            max_connections=100,
            max_keepalive_connections=50,
            keepalive_expiry=30.0,
        ),
    )


class _ProxyGateway:
    """
    Drop-in replacement for ClaudeGateway / OpenAIGateway / GeminiGateway.
    Forwards all LLM calls to the LLM proxy service over the internal network.
    """

    def __init__(self, provider: str):
        self.provider = provider
        self._last_input_tokens          = 0
        self._last_output_tokens         = 0
        self._last_cache_read_tokens     = 0
        self._last_cache_creation_tokens = 0
        logger.info(f"ModelRouter: {provider} → LLM proxy ({_llm_proxy_url()})")

    def generate(
            self,
            prompt=None,
            model: str = None,
            content_blocks: list = None,
            precleared: bool = False,
            precleared_findings: list = None,
    ):
        """precleared / precleared_findings:
            Compliance (detection + redaction) is performed HERE in the backend
            gateway layer (Tier 1). The LLM proxy performs no compliance and
            forwards text verbatim. `precleared=True` is forwarded as the
            `compliance_precleared` body field, which tells the proxy's
            /llm/generate endpoint to skip its minimal HardBlock safety net
            (that net exists only for un-precleared callers, e.g. the ABStudio
            sandbox tool). `precleared_findings` is accepted for backward compat
            but no longer forwarded — the text is already redacted upstream."""
        import httpx

        self._last_input_tokens          = 0
        self._last_output_tokens         = 0
        self._last_cache_read_tokens     = 0
        self._last_cache_creation_tokens = 0

        proxy_url = _llm_proxy_url()

        if content_blocks is not None:
            # Structured path: send provider + content_blocks (no prompt key)
            payload: dict = {"provider": self.provider, "content_blocks": content_blocks}
        elif isinstance(prompt, list):
            # Multi-turn path: send the structured messages list as-is so the
            # downstream gateway can compliance-check only the last user turn
            # (not the entire flattened conversation history). Flattening to a
            # single string caused gateway_*.py compliance to re-validate prior
            # turns and produce false-positive PCI blocks on benign new prompts.
            payload = {"provider": self.provider, "messages": prompt}
        else:
            payload = {"provider": self.provider, "prompt": prompt}
        if model:
            payload["model"] = model

        from core.logger import get_request_id as _get_req_id
        _rid = _get_req_id()
        if _rid:
            payload["request_id"] = _rid

        # Compliance preclear signal. Compliance (detection + redaction) is done
        # here in the backend gateway layer (Tier 1); the proxy performs NO
        # compliance and forwards verbatim. Setting this flag tells the proxy's
        # /llm/generate endpoint to skip its minimal HardBlock safety net (which
        # exists only for un-precleared callers such as the ABStudio sandbox
        # tool). Sent for ALL providers since Tier-1 already validated the prompt.
        if precleared:
            payload["compliance_precleared"] = True

        _mode = (
            "content_blocks" if content_blocks is not None
            else ("messages" if isinstance(prompt, list) else "prompt")
        )
        _payload_chars = (
            sum(len(b.get("text", "")) for b in content_blocks)
            if content_blocks is not None
            else (
                sum(len(str(m.get("content", ""))) for m in prompt)
                if isinstance(prompt, list)
                else len(str(prompt or ""))
            )
        )
        _n_cached = sum(1 for b in (content_blocks or []) if b.get("cache"))
        logger.info(
            f"[PROXY HOP-2] {self.provider} → {proxy_url}/llm/generate "
            f"model={model!r} mode={_mode} "
            + (f"blocks={len(content_blocks)} cached={_n_cached} chars={_payload_chars}"
               if content_blocks is not None else f"prompt_chars={_payload_chars}")
        )

        # Detailed outbound-payload diagnostics — shape, role breakdown, last-user preview,
        # and full content lengths so a compliance/PCI block can be triaged from logs alone
        # without needing to re-trace the call. Previews are truncated to keep log volume sane.
        try:
            if content_blocks is not None:
                _block_summary = ", ".join(
                    f"block{_i}={len(b.get('text', ''))}c/cache={'Y' if b.get('cache') else 'N'}"
                    for _i, b in enumerate(content_blocks)
                )
                logger.info(
                    f"[PROXY HOP-2 PAYLOAD] shape=content_blocks count={len(content_blocks)} "
                    f"total_chars={_payload_chars} | {_block_summary}"
                )
            elif isinstance(prompt, list):
                _role_counts: dict = {}
                for _m in prompt:
                    _role_counts[_m.get("role", "?")] = _role_counts.get(_m.get("role", "?"), 0) + 1
                _last_user = next(
                    (m.get("content", "") for m in reversed(prompt) if m.get("role") == "user"),
                    "",
                )
                _last_user_str = _last_user if isinstance(_last_user, str) else str(_last_user)
                _preview = _last_user_str[:200].replace("\n", " ")
                logger.info(
                    f"[PROXY HOP-2 PAYLOAD] shape=list turns={len(prompt)} roles={_role_counts} "
                    f"total_chars={_payload_chars} last_user_len={len(_last_user_str)} "
                    f"last_user_preview={_preview!r}"
                )
            else:
                _prompt_str = prompt if isinstance(prompt, str) else str(prompt or "")
                _preview = _prompt_str[:200].replace("\n", " ")
                logger.info(
                    f"[PROXY HOP-2 PAYLOAD] shape=str chars={len(_prompt_str)} preview={_preview!r}"
                )
        except Exception as _diag_err:
            logger.debug(f"[PROXY HOP-2 PAYLOAD] diagnostic log failed: {_diag_err}")

        _lines_received = 0

        try:
            client = _get_proxy_client()
            with client.stream(
                    "POST",
                    f"{proxy_url}/llm/generate",
                    json=payload,
            ) as resp:
                logger.info(
                    f"[PROXY HOP-2] {self.provider} stream opened status={resp.status_code}"
                )
                resp.raise_for_status()
                # Read RAW bytes and split on newlines ourselves so each ndjson
                # line is emitted the instant it arrives. httpx's iter_lines()
                # adds an internal buffer layer that can delay per-token flush on
                # the internal proxy hop, defeating streaming for cloud models.
                _buf = ""

                def _handle(_line: str):
                    nonlocal _lines_received
                    if not _line:
                        return None
                    _lines_received += 1
                    try:
                        obj = json.loads(_line)
                    except json.JSONDecodeError:
                        return ("raw", _line)
                    if "error" in obj:
                        logger.error(
                            f"[PROXY HOP-2] {self.provider} proxy returned error "
                            f"after {_lines_received} lines: {obj['error']}"
                        )
                        raise RuntimeError(f"LLM proxy error: {obj['error']}")
                    if "r" in obj:
                        # Reasoning delta from a reasoning model (e.g. gpt-5.4).
                        # Return a ReasoningMarker so the gateway emits a live
                        # {reasoning:{delta}} SSE frame instead of dropping it.
                        try:
                            from pipeline.stream_events import ReasoningMarker as _RM
                            return ("r", _RM(delta=obj["r"]))
                        except Exception:
                            return None
                    if "t" in obj:
                        return ("t", obj["t"])
                    if "m" in obj:
                        self._last_input_tokens          = obj["m"].get("in",           0)
                        self._last_output_tokens         = obj["m"].get("out",          0)
                        self._last_cache_read_tokens     = obj["m"].get("cache_read",   0)
                        self._last_cache_creation_tokens = obj["m"].get("cache_created", 0)
                        logger.info(
                            f"[PROXY HOP-2] {self.provider} metadata received "
                            f"in={self._last_input_tokens} out={self._last_output_tokens} "
                            f"cache_read={self._last_cache_read_tokens} "
                            f"cache_created={self._last_cache_creation_tokens}"
                        )
                    return None

                for chunk in resp.iter_raw():
                    if not chunk:
                        continue
                    _buf += chunk.decode("utf-8", "replace")
                    while "\n" in _buf:
                        _line, _buf = _buf.split("\n", 1)
                        _res = _handle(_line.strip())
                        if _res is not None:
                            yield _res[1]
                # Flush any trailing partial line (no terminating newline).
                if _buf.strip():
                    _res = _handle(_buf.strip())
                    if _res is not None:
                        yield _res[1]
        except Exception as e:
            logger.error(
                f"[PROXY HOP-2] {self.provider}: call failed after {_lines_received} lines "
                f"[{type(e).__name__}] → {e}"
            )
            raise RuntimeError(f"LLM proxy call failed: {e}") from e

    def generate_image(
            self,
            prompt: str,
            image_b64: str,
            mime_type: str = "image/jpeg",
            system_prompt: str = "",
            images_b64: "list[str] | None" = None,
            mime_types: "list[str] | None" = None,
    ) -> tuple[str, int, int, str]:
        """
        Forward an image+prompt to the LLM proxy's /llm/generate-image endpoint.
        The proxy handles primary (Gemini) + fallback (OpenAI) internally.
        Returns (text, in_tok, out_tok, actual_model) where actual_model reflects
        which provider actually ran (gemini or openai fallback).

        Multi-image (optional, backward-compatible): pass `images_b64` (list
        of base64 strings) + matching `mime_types` to analyse multiple images
        in one call. `image_b64`/`mime_type` are still sent as the legacy
        singular fields (first image) so an older, not-yet-upgraded proxy
        deployment ignores the extra fields and keeps working exactly as
        before (analyses the first image only).
        """
        import httpx

        proxy_url = _llm_proxy_url()
        if not proxy_url:
            raise RuntimeError("LLM_PROXY_URL not set — cannot route image call through proxy")

        payload = {
            "provider":      self.provider,
            "prompt":        prompt,
            "image_b64":     image_b64,
            "mime_type":     mime_type,
            "system_prompt": system_prompt,
        }
        if images_b64:
            payload["images_b64"] = images_b64
            payload["mime_types"] = mime_types or [mime_type] * len(images_b64)
        try:
            client = _get_proxy_client()
            resp = client.post(f"{proxy_url}/llm/generate-image", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return (
                data.get("text", ""),
                data.get("in_tok", 0),
                data.get("out_tok", 0),
                data.get("actual_model", self.provider),  # proxy tells us who actually ran
            )
        except Exception as e:
            logger.error(f"_ProxyGateway.generate_image({self.provider}): failed → {e}")
            raise RuntimeError(f"LLM proxy image call failed: {e}") from e

    async def async_generate(self, prompt, model: str = None,
                              _override_client=None) -> str:
        """Async version of generate() — caller supplies an AsyncClient.

        Parameters
        ----------
        _override_client : httpx.AsyncClient | None
            When provided, this client is used for the request instead of the
            shared singleton.  ModelRouter.async_generate() passes a fresh
            per-call client here (SDLC_PER_LOOP_HTTP_CLIENT != "0", default)
            so this method is safe to call from any event loop, including RQ
            worker asyncio.run() contexts.

            When None, falls back to the shared singleton from
            _get_async_proxy_client() — legacy behaviour, only safe under a
            single long-running event loop (e.g. uvicorn).
        """
        proxy_url = _llm_proxy_url()
        if isinstance(prompt, list):
            prompt = "\n".join(
                f"{m['role'].title()}: {m.get('content', '')}" for m in prompt
            )
        payload: dict = {"provider": self.provider, "prompt": prompt}
        if model:
            payload["model"] = model

        from core.logger import get_request_id as _get_req_id
        _rid = _get_req_id()
        if _rid:
            payload["request_id"] = _rid

        _client = _override_client if _override_client is not None else _get_async_proxy_client()
        try:
            chunks = []
            async with _client.stream(
                    "POST",
                    f"{proxy_url}/llm/generate",
                    json=payload,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        chunks.append(line)
                        continue
                    if "error" in obj:
                        raise RuntimeError(f"LLM proxy error: {obj['error']}")
                    elif "t" in obj:
                        chunks.append(obj["t"])
                    elif "m" in obj:
                        self._last_input_tokens  = obj["m"].get("in", 0)
                        self._last_output_tokens = obj["m"].get("out", 0)
            return "".join(chunks)
        except Exception as e:
            logger.error(f"_ProxyGateway({self.provider}).async_generate: failed → {e}")
            raise RuntimeError(f"LLM proxy async call failed: {e}") from e

    async def async_stream(
            self,
            prompt,
            model: str = None,
            precleared: bool = False,
            precleared_findings: list = None,
            _override_client=None,
    ):
        """Async streaming generator — yields str tokens as they arrive from the proxy.

        Mirrors _call_proxy_stream() but runs entirely on the event loop so
        FastAPI's async StreamingResponse can flush each token to the client
        the instant it arrives, without blocking a thread-pool worker.

        This is the async counterpart of generate() (which is sync + blocking).
        Use this from async generators (e.g. _general_stream_async in gateway.py)
        so the /ask SSE path matches the per-token delivery of the CLI path.
        """
        proxy_url = _llm_proxy_url()
        if not proxy_url:
            # No proxy configured — fall back to sync generate() in a thread.
            import asyncio as _asyncio
            loop = _asyncio.get_event_loop()
            for tok in self.generate(prompt, model=model, precleared=precleared,
                                     precleared_findings=precleared_findings):
                yield tok
            return

        _mode = "messages" if isinstance(prompt, list) else "prompt"
        _payload_chars = (
            sum(len(str(m.get("content", ""))) for m in prompt)
            if isinstance(prompt, list)
            else len(str(prompt or ""))
        )
        logger.info(
            f"[PROXY HOP-2] {self.provider} → {proxy_url}/llm/generate "
            f"model={model!r} mode={_mode} prompt_chars={_payload_chars} [async]"
        )

        if isinstance(prompt, list):
            payload: dict = {"provider": self.provider, "messages": prompt}
        else:
            payload = {"provider": self.provider, "prompt": prompt}
        if model:
            payload["model"] = model

        from core.logger import get_request_id as _get_req_id
        _rid = _get_req_id()
        if _rid:
            payload["request_id"] = _rid
        if precleared:
            payload["compliance_precleared"] = True

        _per_call = _use_per_call_async_client()
        _owned = _make_async_client() if (_per_call and _override_client is None) else None
        _client = _override_client or _owned or _get_async_proxy_client()
        _buf = ""
        _lines_received = 0
        try:
            async with _client.stream(
                    "POST",
                    f"{proxy_url}/llm/generate",
                    json=payload,
            ) as resp:
                logger.info(
                    f"[PROXY HOP-2] {self.provider} stream opened status={resp.status_code} [async]"
                )
                resp.raise_for_status()
                # Read raw bytes and split on newlines — same strategy as the
                # sync generate() so each ndjson line is processed the instant
                # it arrives without httpx's internal line buffer delay.
                async for chunk in resp.aiter_raw():
                    if not chunk:
                        continue
                    _buf += chunk.decode("utf-8", "replace")
                    while "\n" in _buf:
                        _line, _buf = _buf.split("\n", 1)
                        _line = _line.strip()
                        if not _line:
                            continue
                        _lines_received += 1
                        try:
                            obj = json.loads(_line)
                        except json.JSONDecodeError:
                            yield _line
                            continue
                        if "error" in obj:
                            logger.error(
                                f"[PROXY HOP-2] {self.provider} proxy returned error "
                                f"after {_lines_received} lines: {obj['error']} [async]"
                            )
                            raise RuntimeError(f"LLM proxy error: {obj['error']}")
                        if "r" in obj:
                            # Reasoning delta from a reasoning model (e.g. gpt-5.4).
                            # Yield a ReasoningMarker so the gateway's _general_stream
                            # emits a live {reasoning:{delta}} SSE frame instead of
                            # dropping the chunk (the cause of the 25 s hang).
                            try:
                                from pipeline.stream_events import ReasoningMarker as _RM
                                yield _RM(delta=obj["r"])
                            except Exception:
                                pass
                        elif "t" in obj:
                            yield obj["t"]
                        elif "m" in obj:
                            self._last_input_tokens          = obj["m"].get("in",           0)
                            self._last_output_tokens         = obj["m"].get("out",          0)
                            self._last_cache_read_tokens     = obj["m"].get("cache_read",   0)
                            self._last_cache_creation_tokens = obj["m"].get("cache_created", 0)
                            logger.info(
                                f"[PROXY HOP-2] {self.provider} metadata received "
                                f"in={self._last_input_tokens} out={self._last_output_tokens} "
                                f"cache_read={self._last_cache_read_tokens} "
                                f"cache_created={self._last_cache_creation_tokens} [async]"
                            )
                # Flush any trailing partial line.
                if _buf.strip():
                    try:
                        obj = json.loads(_buf.strip())
                        if "r" in obj:
                            try:
                                from pipeline.stream_events import ReasoningMarker as _RM
                                yield _RM(delta=obj["r"])
                            except Exception:
                                pass
                        elif "t" in obj:
                            yield obj["t"]
                        elif "m" in obj:
                            self._last_input_tokens  = obj["m"].get("in",  0)
                            self._last_output_tokens = obj["m"].get("out", 0)
                    except json.JSONDecodeError:
                        yield _buf.strip()
        except Exception as e:
            logger.error(
                f"[PROXY HOP-2] {self.provider}: async_stream failed after "
                f"{_lines_received} lines [{type(e).__name__}] → {e}"
            )
            raise RuntimeError(f"LLM proxy async stream failed: {e}") from e
        finally:
            if _owned is not None:
                await _owned.aclose()


# ── Circuit breakers — one per provider ───────────────────────
_CB_LOCAL  = get_breaker("local",  failure_threshold=3, recovery_timeout=30)
_CB_OPENAI = get_breaker("openai", failure_threshold=5, recovery_timeout=60)
_CB_CLAUDE = get_breaker("claude", failure_threshold=5, recovery_timeout=60)
_CB_GEMINI = get_breaker("gemini", failure_threshold=5, recovery_timeout=60)

# Minimum confidence to trust the regex classifier.
# Below this, classify_with_confidence_llm() escalates to Claude Haiku.
# This constant is kept for logging context only — routing logic uses LLM directly.
_CONFIDENCE_LOG_THRESHOLD = 0.7


# ============================================================
# TIER CONSTANTS
# ============================================================

TIER_SIMPLE    = "simple"
TIER_MINI      = "mini"       # direct GPT-5-mini (no local LLM hop)
TIER_LOCAL_MINI = "local_mini"  # in-house hosted GPT-OSS-120B (OpenAI-compat, no cloud egress); lightweight fast tier
TIER_MEDIUM    = "medium"
TIER_COMPLEX   = "complex"
TIER_HAIKU     = "haiku"      # explicit Haiku selection → Claude Haiku (lightweight, fast)
TIER_VISION    = "vision"     # auto-detected image/visual queries → Gemini
TIER_GEMINI    = "gemini"     # explicit Gemini selection → Gemini (text, no Vision label)
TIER_SOLUTION  = "solution"   # final synthesis — Opus 4.7 if ENABLE_OPUS=true, else Sonnet
TIER_OPUS_48   = "opus-4-8"   # CLI/IDE Claude Opus 4.8 selection (not shown in chat picker, not used by SDLC)
TIER_OPUS_5    = "opus-5"     # CLI/IDE Claude Opus 5 selection (opt-in, ENABLE_CLI_OPUS_5)
TIER_SONNET_5  = "sonnet-5"   # explicit Claude Sonnet 5 selection (available on ALL channels)
TIER_DEEP      = "deep"       # explicit GPT-5-5 latest tier
TIER_TERA      = "tera"       # GPT-5.6 Terra — high-capacity variant (Chat + CLI, ENABLE_GPT56_TERA)
TIER_LUNA      = "luna"       # GPT-5.6 Luna — efficient variant (Chat + CLI, ENABLE_GPT56_LUNA)
# Admin-configured provider registry (core.llm_provider_registry) dispatch —
# extra Anthropic/OpenAI models and any openai_compatible/OpenRouter model
# added via the "LLM Providers" admin screen. Always paired with
# provider_model_override (the exact model_id); _dispatch() re-resolves the
# model's family at dispatch time to pick the right client. No cross-vendor
# fallback (unlike every other tier above) — see _dispatch()'s TIER_REGISTRY
# branch.
TIER_REGISTRY  = "registry"

# Caller hint → tier mapping.
# Static entries cover shorthand hints; dynamic entries cover full model IDs
# so env-var-configured model names are automatically routed to the right tier.
_HINT_MAP = {
    "simple":        TIER_SIMPLE,    # auto-routing: local LLM first, gpt-5-mini fallback
    "local":         TIER_SIMPLE,
    "mini":          TIER_MINI,      # direct GPT-5-mini, no local LLM hop
    "gpt-mini":      TIER_MINI,
    "gpt-5-mini":    TIER_MINI,
    "local_mini":    TIER_LOCAL_MINI,  # in-house GPT-OSS-120B (used by CIL intent classifier)
    "gpt-oss":       TIER_LOCAL_MINI,  # legacy alias
    "gpt-oss-120b":  TIER_LOCAL_MINI,
    OPENAI_OSS_MODEL: TIER_LOCAL_MINI,
    "medium":        TIER_MEDIUM,
    "coding":        TIER_MEDIUM,
    "agents":        TIER_MEDIUM,
    "gpt":           TIER_MEDIUM,
    "gpt-5.4":       TIER_MEDIUM,    # explicit gpt-5.4 coding hint
    "complex":       TIER_COMPLEX,
    "sonnet":        TIER_COMPLEX,
    "claude":        TIER_COMPLEX,
    "haiku":         TIER_HAIKU,
    "vision":        TIER_VISION,
    # Explicit Gemini selection — does NOT show "Vision" label
    "gemini":        TIER_GEMINI,
    # Legacy aliases — route to current Gemini default via _GEMINI_SPECIFIC_HINTS
    "gemini-2.5-flash":       TIER_GEMINI,
    "gemini-2.0-flash":       TIER_GEMINI,
    "gemini-3.5-flash":       TIER_GEMINI,
    "gemini-3.1-flash-lite":  TIER_GEMINI,
    "gemini-3.1-flash-image": TIER_VISION,
    # Solution tier — Opus 4.7 if ENABLE_OPUS, else Sonnet
    "solution":      TIER_SOLUTION,
    "opus":          TIER_SOLUTION,
    # Explicit Opus 4.8 selection (CLI/IDE only)
    "opus-4-8":      TIER_OPUS_48,
    "claude-opus-4-8": TIER_OPUS_48,
    # Explicit Opus 5 selection (CLI/IDE opt-in)
    "opus-5":        TIER_OPUS_5,
    "claude-opus-5": TIER_OPUS_5,
    # Explicit Sonnet 5 selection (all channels)
    "sonnet-5":       TIER_SONNET_5,
    "claude-sonnet-5": TIER_SONNET_5,
    # Dynamic — ensures env-var model ID overrides are also mapped
    OPENAI_SIMPLE_MODEL:  TIER_MINI,      # explicit model ID → direct access
    OPENAI_CODING_MODEL:  TIER_MEDIUM,
    OPENAI_LATEST_MODEL:  TIER_DEEP,
    CLAUDE_PRIMARY_MODEL: TIER_COMPLEX,
    CLAUDE_HAIKU:         TIER_HAIKU,
    GEMINI_VISION_MODEL:       TIER_VISION,   # vision analysis model (gemini-3.5-flash by default)
    GEMINI_TEXT_MODEL:         TIER_GEMINI,
    GEMINI_CODING_LITE_MODEL:  TIER_GEMINI,
    GEMINI_IMAGE_MODEL:        TIER_VISION,
    CLAUDE_OPUS_MODEL:    TIER_SOLUTION,
    CLAUDE_OPUS_48_MODEL: TIER_OPUS_48,
    CLAUDE_OPUS_5_MODEL:  TIER_OPUS_5,
    CLAUDE_SONNET_5_MODEL: TIER_SONNET_5,
    # Deep tier explicit hints
    "deep":               TIER_DEEP,
    "gpt-5-5":            TIER_DEEP,
    # GPT-5.6 Tera — high-capacity variant (Chat + CLI)
    "tera":               TIER_TERA,
    "gpt-5.6-terra":      TIER_TERA,
    OPENAI_TERA_MODEL:    TIER_TERA,
    # GPT-5.6 Luna — efficient variant (Chat + CLI)
    "luna":               TIER_LUNA,
    "gpt-5.6-luna":       TIER_LUNA,
    OPENAI_LUNA_MODEL:    TIER_LUNA,
}

# ── Falsy-key guard ───────────────────────────────────────────────────────────
# Blank env vars (e.g. OPENAI_SIMPLE_MODEL="") produce empty-string keys in
# _HINT_MAP.  "anything".startswith("") is always True, so the FIRST entry
# whose key is "" would match every model ID and route everything to that tier.
# Strip them out now, then hard-fail so the operator knows which env var to fix.
_HINT_MAP = {k: v for k, v in _HINT_MAP.items() if k}

_empty_keys = [k for k in _HINT_MAP if not k]
if _empty_keys:
    raise ValueError(
        "Model routing map contains empty-string keys — check that all "
        "OPENAI_*/ANTHROPIC_*/GEMINI_* model env vars are set. "
        "An empty key matches every model ID via startswith('') and routes "
        "everything to the first provider."
    )

# ============================================================
# PRIVACY FLOOR (hard enterprise safety invariant)
# ============================================================
# docs/architecture/10-model-router.md §10.2 + core/rag_acl.py classification
# ladder. When a request carries data at/above CONFIDENTIAL sensitivity, it must
# NEVER egress to a cloud provider (OpenAI/Claude/Gemini) — it is pinned to the
# in-house Local model (TIER_SIMPLE). profiles/routing.py implements the pure
# decision logic; this is the LIVE enforcement point in the router itself.
#
# This is a HARD invariant, not best-effort: the override runs BEFORE hint /
# vision / complexity routing so nothing downstream can re-route restricted data
# to the cloud. Enforcement can be disabled only via an explicit env opt-out
# (default ON) and every enforcement is logged for audit/alerting.
_PRIVACY_FLOOR_ENFORCE = os.getenv("PRIVACY_FLOOR_ENFORCE", "true").lower() == "true"

# Classifications that must stay on-prem (local-only). Ascending ladder from
# core/rag_acl.py: PUBLIC < INTERNAL < CONFIDENTIAL < RESTRICTED < PCI_SENSITIVE.
# INTERNAL and PUBLIC may use cloud models; everything above stays local.
_LOCAL_ONLY_CLASSIFICATIONS = frozenset({"CONFIDENTIAL", "RESTRICTED", "PCI_SENSITIVE"})

# Returned verbatim when the privacy floor is in force and the local model is
# down. Hoisted to a module constant so _try_local_simple can recognise it by
# IDENTITY coming back out of the shared dispatcher — the alternative, matching
# on the text, would break the first time someone reworded it.
_PRIVACY_FAIL_CLOSED_TEXT = (
    "Error: the in-house (local) model was requested but is not "
    "available, and this request may not be sent to a cloud "
    "provider. Check that LOCAL_LLM_BASE_URL points at a running "
    "OpenAI-compatible server (e.g. http://localhost:11434 for "
    "Ollama) and that it has at least one model pulled."
)

# ============================================================
# CONTEXT-SIZE ROUTING (frontier pattern #5 — "context size = routing")
# ============================================================
# docs/architecture/02 §2.5/§2.8. Context size is a first-class routing
# dimension: when a turn's estimated token footprint would not fit (or barely
# fits) a tier's context window, the router promotes to a larger-window model
# rather than risking truncation/compaction. Fail-safe: on any error or when no
# larger tier is warranted, the complexity-derived tier is unchanged.
_CONTEXT_SIZE_ROUTING = os.getenv("CONTEXT_SIZE_ROUTING", "true").lower() == "true"

# Fraction of a model's window a turn may occupy (headroom for the answer):
# becomes the min_context_window constraint on the requested tier (§M.2).
_CONTEXT_FIT_FRACTION = float(os.getenv("CONTEXT_FIT_FRACTION", "0.8"))
def classification_from_policy(policy) -> Optional[str]:
    """Derive a request data_classification from a resolved policy/profile.

    Maps the profile's RoutingPolicy.privacy_floor (public|internal|confidential|
    restricted, per profiles/schema.py) into the router's classification
    vocabulary so callers have ONE correct way to feed the privacy floor into
    route()/generate(). Returns None when no floor is set (→ no override). Never
    raises. The floor is a *minimum* handling tier: a 'confidential' floor means
    even otherwise-unclassified traffic on that profile stays local.
    """
    try:
        if policy is None:
            return None
        routing = getattr(policy, "routing", None)
        floor = getattr(routing, "privacy_floor", None) if routing is not None else None
        if not floor:
            return None
        f = str(floor).strip().lower()
        if f in ("confidential", "restricted"):
            return f.upper()
        return None  # public/internal floors do not force local
    except Exception:  # noqa: BLE001
        return None


def _privacy_requires_local(data_classification: Optional[str]) -> bool:
    """True when the given data classification must be handled by a local model
    only (never egress to a cloud provider). Unknown/None → False (no override),
    matching today's behavior for unclassified traffic. Never raises."""
    try:
        if not _PRIVACY_FLOOR_ENFORCE or not data_classification:
            return False
        return str(data_classification).strip().upper() in _LOCAL_ONLY_CLASSIFICATIONS
    except Exception:  # noqa: BLE001 — safety check must never break routing
        return False

# ============================================================
# TIER GOVERNANCE
# ============================================================
#
# A request for a CAPABILITY resolves through llm_tier_models, the assignments
# an administrator made on the Tiers screen. Phase 8 removed the env-constant
# fallback and the TIER_GOVERNANCE_ENABLED switch: a tier with nothing eligible
# raises NoEligibleModel.


# Sentinel tier for a decision that came from the resolver. Not one of the
# eight: it names the PATH, not a capability, in the same way TIER_REGISTRY
# names the user-explicit path. The tier that was actually requested travels
# on RoutingDecision.requested_tier, which is what the §L.5 audit columns read.
TIER_GOVERNED = "governed"


class ModelsBlockedByPolicy(Exception):
    """Every model eligible for this tier is denied to this user (§N.1 step 9).

    Deliberately NOT a NoEligibleModel. The two failures look alike and must
    be handled in opposite ways:

      NoEligibleModel      the DEPLOYMENT cannot serve the tier — nothing is
                           assigned, or every candidate fails a constraint.
                           The fix is an assignment on the Tiers screen.

      ModelsBlockedByPolicy
                           the deployment can serve it and an ADMINISTRATOR
                           said this user may not. The fix is an access rule,
                           which is why this is a distinct type.

    So this propagates to the caller, which turns it into the 403 the Chat
    Auto path already returns. `blocked` is the ordered candidate list that
    was refused, so the log line can name what the rule actually hit.
    """

    def __init__(self, tier, blocked: list):
        self.tier = tier
        self.blocked = list(blocked)
        name = getattr(tier, "value", tier)
        super().__init__(
            f"every model eligible for tier {name!r} is blocked for this "
            f"user ({', '.join(self.blocked) or 'no candidates'})"
        )


# The shape a caller passes to apply its own access-control list to a tier's
# candidates. Mirrors routers.model_governance_router.filter_allowed_models,
# minus the identity and session it closes over: given model ids, return the
# permitted subset. A callable rather than a precomputed set because the
# candidate ids are not known until the tier resolves — a set would mean
# resolving twice and the second resolution could disagree with the first.
AclFilter = Callable[[List[str]], List[str]]


# ============================================================
# MIGRATED CALL SITES  (Phase 6)
# ============================================================
#
# Several §D.2 tasks have a per-feature env var that names a model or a hint
# — CIL_INTENT_MODEL, DOC_INTENT_MODEL, ENRICH_MODEL. §I.3 replaces each with
# a tier request while KEEPING the variable as a deprecated override for one
# release, so an operator who had pinned something does not lose the pin on
# upgrade. That is three-way logic (override / tier / pre-migration hint) and
# it should exist once, not once per feature.

_TIER_OVERRIDE_WARNED: set = set()


def tier_request(tier: Tier, legacy_hint: str,
                 override: Optional[str] = None, *,
                 override_name: str = "") -> dict:
    """Routing kwargs for a call site migrated to the tier vocabulary.

    Returns a mapping to splat into generate()/stream():

        model_router.generate(prompt, **tier_request(
            Tier.INTENT_CLASSIFICATION, "local_mini",
            _INTENT_MODEL, override_name="CIL_INTENT_MODEL"))

    Precedence, highest first:

      1. `override` — a non-blank per-feature env var. The operator named a
         model explicitly and governance must not second-guess that, for the
         same reason a user's dropdown pick is not governed. Warned once per
         variable per process, because it is going away in Phase 8.
      2. `tier` — the administrator's assignment, when governance is on.
      3. `legacy_hint` — what this call site passed before it was migrated,
         used whenever (2) produces nothing. This is D15; see _coerce_tier.

    `legacy_hint` is REQUIRED rather than optional so a migration cannot
    forget it — the failure it prevents is silent, and a positional argument
    is the cheapest way to make forgetting impossible.
    """
    if override and override.strip():
        value = override.strip()
        if override_name and override_name not in _TIER_OVERRIDE_WARNED:
            _TIER_OVERRIDE_WARNED.add(override_name)
            logger.warning(
                "%s=%r is set, so it overrides the %r tier assignment. This "
                "variable is DEPRECATED — assign a model to %r on Model "
                "Governance > Tiers and unset it; it is removed in a later "
                "release.", override_name, value, tier.value, tier.value,
            )
        return {"model_hint": value}
    return {"tier": tier, "legacy_hint": legacy_hint}


# The complexity classifier's output vocabulary → the tier that serves it
# (§N.1 step 9, targets from plan.html §D/§F). models.classifier._VALID_TIERS
# and cil.intent._VALID_COMPLEXITY are both exactly these three keys, and
# cil.intent._RETIRED_COMPLEXITY has already collapsed deep/solution into
# "complex" by the time any caller gets here.
#
# Note what "simple" does: it used to dispatch the in-house local model, and
# Tier.SIMPLE is whatever the administrator assigned. That is the intended
# change (§D.2 — a task's tier expresses the capability it needs, not the fact
# that the old implementation happened to run locally) and it is the one entry
# in this map that can move a turn off the machine.
_CHAT_COMPLEXITY_TIERS: dict = {
    "simple":  Tier.SIMPLE,
    "medium":  Tier.MEDIUM,
    "complex": Tier.COMPLEX,
}


def chat_complexity_route(complexity: Optional[str]) -> dict:
    """Routing kwargs for a classifier verdict, shared by all four chat paths.

    Lives here rather than in each caller because gateway.py, kb_ask_router,
    chat_worker and agents/tools all map the same three words, and four copies
    of a routing table is how the pre-migration code ended up with two.

    An unrecognised or EMPTY verdict returns ``{"model_hint": <value>}``
    unchanged, which is not an oversight:

      - route() gates its hint branch on ``if model_hint:``, so an empty hint
        means "classify this prompt yourself" — there is no tier that says
        that, and there is no _HINT_MAP key for it either.
      - _coerce_tier RAISES on a legacy_hint that is not a _HINT_MAP key, and
        strips falsy ones, so ``tier_request(X, "")`` cannot express it.

    agents/tools.py reaches the empty case by default: its no-repo-context
    downgrade assigns ``os.getenv("DOWNGRADE_MODEL", "")``. Coercing that to a
    tier would have raised on the first such turn — the same assumption that
    bit §N.1 step 6 on ENRICH_MODEL.
    """
    key = (complexity or "").strip().lower()
    if not key:
        # Normalised, not passed through: "   " is falsy to a human and TRUTHY
        # to route()'s `if model_hint:`, so forwarding it verbatim would take
        # the hint branch with a blank hint instead of classifying.
        return {"model_hint": ""}
    tier = _CHAT_COMPLEXITY_TIERS.get(key)
    if tier is None:
        # Unrecognised: forward the ORIGINAL, not `key`. Lowercasing here
        # would mangle a case-sensitive model id, which is the defect step 8
        # hit on DOC_MODEL_PROVIDER.
        return {"model_hint": complexity}
    # legacy_hint is the verdict's own word, not the tier's name: with
    # governance off, model_hint="simple" must keep meaning what it meant.
    return tier_request(tier, key)


def sdlc_stage_route(stage: str) -> dict:
    """Routing kwargs for an SDLC stage (§N.1 step 10).

    The in-process twin of core.model_registry.cli_tier_model_id(), and the
    sibling of chat_complexity_route() above: one place where a stage name
    becomes a tier request, rather than one per call site. That is not
    tidiness — the pre-migration code had the SAME stage resolved through two
    different tables in _core.py and _phases.py, and they had diverged.

    The stage table lives in core.model_registry because the CLI resolver
    needs it too and that module is importable without pulling in the router.

    Returns `{"tier": …, "legacy_hint": …}` plus any §M constraints the stage
    declares — today only `require_role` on the review gate. An unknown stage
    is NOT a tier request: it returns the empty hint, which route() reads as
    "classify this prompt yourself". That matches what sdlc_stage_hint's
    `default="complex"` was reaching for without pretending an unregistered
    stage has a governed answer.
    """
    from core.model_registry import SDLC_STAGE_TIERS

    entry = SDLC_STAGE_TIERS.get((stage or "").strip().lower())
    if entry is None:
        logger.warning(
            "sdlc_stage_route(%r): no such SDLC stage — routing unhinted. The "
            "stage table is core.model_registry.SDLC_STAGE_TIERS.", stage)
        return {"model_hint": ""}

    tier, legacy_hint, extra = entry
    # SDLC_MODEL_<STAGE> survives as a DEPRECATED per-stage pin (§I, Phase 8),
    # applied through tier_request's override so it warns once per variable
    # per process exactly like CIL_INTENT_MODEL and DOC_INTENT_MODEL do.
    override_name = f"SDLC_MODEL_{stage.strip().upper()}"
    route = tier_request(tier, legacy_hint, os.getenv(override_name, ""),
                         override_name=override_name)
    # An override won: it is a bare hint, and a §M constraint on top of an
    # explicitly named model would be answering a question the operator did
    # not ask.
    if "tier" not in route:
        return route
    return {**route, **extra}


def sdlc_llm_route(hint=None) -> dict:
    """Normalise the three shapes SDLC's `_llm(prompt, hint=…)` accepts.

    `_llm` exists twice — agents/sdlc_pipeline/_core.py and
    agents/sdlc_state_machine.py — and before §N.1 step 10 the two disagreed
    about their own default (`"solution"` in one, `"complex"` reaching the
    other through `_llm_traced`). One helper so they cannot drift again.

      dict  → routing kwargs from sdlc_stage_route(); used as-is.
      str   → a legacy hint or a concrete model id; route() decides which.
              SDLC_MODEL_<STAGE> can still name a raw id (§I, Phase 8).
      None  → Tier.COMPLEX. The legacy hint is "solution" so flag-off is
              byte-identical, but note what that means flag-ON: "solution"
              carries require_role="review" through _LEGACY_TO_GOVERNED, and
              this does NOT. That is deliberate (D43) — the review role now
              belongs to the two review gates, not to every hintless call.
    """
    if isinstance(hint, dict):
        return dict(hint)
    if hint:
        return {"model_hint": hint}
    return tier_request(Tier.COMPLEX, "solution")


def route_label(route: dict) -> str:
    """A short human name for what a route ASKED for — logs, not audit rows.

    `tier=Tier.COMPLEX` reads as "complex"; a bare hint reads as itself. Used
    where the old SDLC code logged its hint string, so the log line keeps
    saying something a reader recognises after the hint is gone.
    """
    tier = (route or {}).get("tier")
    if tier is not None:
        return getattr(tier, "value", str(tier))
    return str((route or {}).get("model_hint") or "auto")


def dispatched_model_id(router, fallback: str = "") -> str:
    """The concrete model id the LAST dispatch on this thread used.

    For audit rows (§L.5) and cost, which must name the model that RAN — not
    the tier that was requested. `router.last_model_id` is thread-local and
    set by the dispatcher; "auto" is its never-called sentinel, so that and a
    blank both mean "we do not actually know" and yield the caller's fallback
    rather than a confident wrong answer.
    """
    try:
        mid = (router.last_model_id or "").strip()
    except Exception:  # noqa: BLE001 — an audit row must never fail a run
        return fallback
    return mid if mid and mid.lower() not in ("auto", "unknown") else fallback


def model_family(model_id: Optional[str]) -> str:
    """The registry family of a concrete model id, or "" if it is unknown.

    The input to `distinct_from_family` (§M.3b), which is a relational
    constraint: "not whoever wrote the thing being judged". A caller that
    knows the AUTHOR's model id can turn it into the family the resolver
    compares against without learning the registry's schema.

    Returns "" rather than guessing, and the caller then omits the constraint
    — an unknown author is not grounds for refusing to review.
    """
    mid = (model_id or "").strip()
    if not mid:
        return ""
    try:
        from core.llm_provider_registry import get_model as _get_registry_model
        row = _get_registry_model(mid)
        return (row or {}).get("family") or ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("model_family(%r): registry unavailable (%s)", mid, exc)
        return ""


def no_eligible_message(exc) -> str:
    """The user-facing text for a NoEligibleModel: a privacy refusal or an unassigned tier."""
    if getattr(getattr(exc, "constraints", None), "no_cloud_egress", False):
        logger.error("ModelRouter: PRIVACY FLOOR — %s", exc)
        return ("Error: this request carries data that may not be sent to a "
                f"cloud provider, and no in-house model can serve it ({exc}). "
                "Assign a deployment-local model to this tier, or configure a "
                "local provider.")
    logger.error("ModelRouter: %s", exc)
    return (f"Error: {exc}. An administrator can assign a model to this tier "
            "in Admin → Model Governance → Tiers.")


def fully_blocked_candidates(tier, acl_filter: Optional[AclFilter]) -> Optional[List[str]]:
    """The tier's candidate ids when the ACL denies ALL of them, else None.

    A pre-flight for callers that must reject before they start streaming.
    ``route(acl_filter=…)`` remains the authoritative filter — this only
    answers "is it worth starting", so that a governance denial is an HTTP 403
    with a reason rather than an exception halfway through an SSE body the
    client has already begun rendering.

    Returns None — meaning "go ahead" — for every other outcome, including a
    deployment with nothing assigned: that is NoEligibleModel's, raised by route().
    """
    if acl_filter is None:
        return None
    try:
        from core.tier_resolver import resolve_tier_candidates
        ids = [rm.model_id for rm in resolve_tier_candidates(Tier(tier))]
        if ids and not set(acl_filter(ids)):
            return ids
    except Exception as exc:  # noqa: BLE001 — a pre-flight never blocks a turn
        logger.debug("fully_blocked_candidates(%r): %s", tier, exc)
    return None


def resolve_pick_to_model_id(hint: Optional[str]) -> str:
    """The model id a USER-SUPPLIED pick will actually dispatch to (§N.1 step 9).

    The honest replacement for hint_to_model_id() on the access-control path.
    hint_to_model_id maps a hint to an .env CONSTANT, which stopped being the
    same thing as "the model that runs" when the registry arrived: it answers
    'gpt-5.4' for "medium" on a deployment that dispatches claude-sonnet-4-6,
    and '' for "claude-sonnet-5" — a model that is in llm_models and is the
    head of the complex tier — because CLAUDE_SONNET_5_MODEL is unset.

    Resolution order mirrors route()'s own, so the id an ACL is checked
    against is the id that gets called:

      1. ``local:<id>``      — an exact in-house model, by address.
      2. a registry model id — route() takes its TIER_REGISTRY branch and
                               dispatches this exact id. Checked BEFORE the
                               alias table because several registry ids are
                               also alias keys (claude-sonnet-5, claude-opus-5)
                               and the table's answer for those is the empty
                               env constant.
      3. a legacy alias      — with governance on, route() resolves the tier it
                               means, so the ACL target is that tier's head.
      4. otherwise           — hint_to_model_id's legacy answer, which is
                               correct precisely when governance is off.

    Returns "" when nothing resolves, so a caller can fall through to its own
    fallback rather than distinguishing None from ''.
    """
    key = (hint or "").strip()
    if not key:
        return ""
    # "auto" means "the platform chooses", so there is no pick to check. It
    # needs saying because core.tiers.LEGACY_INBOUND_ALIASES maps "auto" to
    # Tier.SIMPLE — reasonable for an inbound CLI hint, wrong as an answer to
    # "which model did the user name". gateway.py normalises these to None
    # before calling, but this is a public helper and the next caller may not.
    if key.lower() in ("auto", "default", "none"):
        return ""
    if key.lower().startswith("local:"):
        return key

    try:
        from core.llm_provider_registry import get_enabled_models
        for m in get_enabled_models():
            if m["model_id"] == key:
                return key
    except Exception as exc:  # noqa: BLE001
        logger.debug("resolve_pick_to_model_id(%r): registry unavailable (%s)", key, exc)

    try:
        from core.tiers import (
            EXPLICIT_MODEL, note_legacy_alias, resolve_legacy_alias,
        )
        alias = resolve_legacy_alias(key)
        if isinstance(alias, Tier):
            # Feeds the same counter that gates the shim's removal in
            # Phase 10, exactly as §N.1 step 11's boundary does.
            note_legacy_alias(key, "chat-pick")
            from core.tier_resolver import resolve_tier
            return resolve_tier(alias).model_id
        if alias == EXPLICIT_MODEL:
            note_legacy_alias(key, "chat-pick")
            # The client named a vendor, not a model. There is nothing
            # concrete to check; fall through to the legacy answer.
    except Exception as exc:  # noqa: BLE001
        logger.debug("resolve_pick_to_model_id(%r): tier lookup failed (%s)", key, exc)

    return hint_to_model_id(key) or ""


# Hints that named the in-house model directly, pre-migration. Retained only
# as the governance-OFF answer for is_local_route(): with the flag off there
# is no resolution to inspect, so the string is all there is.
_LEGACY_LOCAL_HINTS = ("local", "simple", "inhouse", "in-house")


def is_deployment_local(model_id: Optional[str]) -> bool:
    """True when `model_id` runs inside the estate (§N.1 step 9).

    Reads ``capabilities.privacy_class``, the same field ``no_cloud_egress``
    filters on (core/tier_resolver.py:275), so "is this local" has one answer
    across the platform instead of one per caller.

    It replaces ``hint in ("local", "simple")``, which two chat paths used to
    decide both the KV-cache hoist and whether to show the budget chip. Once
    Tier.SIMPLE can hold a cloud model — which is the point of letting an
    administrator assign it — that test starts calling a paid Claude turn
    "local" and suppressing its budget chip: the user is billed and the budget
    bar silently stops moving.

    A union of two sources rather than a replacement for either, because they
    answer different questions and a model can be in one and not the other:

      capabilities.privacy_class      registry POLICY — the administrator
                                      declared this model deployment-local.
      gateway_local_llm.is_local_model  live DISPATCH — the Local LLM proxy's
                                      /v1/models catalog serves it. Already
                                      what _estimate_cost consults to bill
                                      in-house models at $0, so dropping it
                                      would make this disagree with the cost
                                      the same turn is charged.

    Fails CLOSED (returns False → "treat as billable") on any lookup problem.
    Showing a budget chip for a free turn is cosmetic; hiding one for a paid
    turn loses money silently.
    """
    mid = (model_id or "").strip()
    if not mid:
        return False
    if mid.lower().startswith("local:"):
        return True
    try:
        from core.llm_provider_registry import get_enabled_models
        for m in get_enabled_models():
            if m["model_id"] == mid:
                caps = m.get("capabilities") or {}
                if caps.get("privacy_class") == "deployment_local":
                    return True
                break
    except Exception as exc:  # noqa: BLE001
        logger.debug("is_deployment_local(%r): registry unavailable (%s)", mid, exc)
    try:
        from gateway_local_llm import is_local_model as _proxy_serves
        return bool(_proxy_serves(mid))
    except Exception as exc:  # noqa: BLE001
        logger.debug("is_deployment_local(%r): local catalog unavailable (%s)", mid, exc)
    return False


def is_local_route(local_model: Optional[str], route_kwargs: Optional[dict] = None) -> bool:
    """Best-effort "will this turn stay in the estate", BEFORE dispatch.

    Needed because the KV-cache hoist shapes the prompt and therefore has to
    decide before the model is known. A wrong answer here costs a slightly
    different system message, nothing more — so this peeks at the tier's head
    rather than plumbing the resolution out of route().

    The post-dispatch decisions (the budget chip) must NOT use this. They have
    the real model and should ask is_deployment_local() about it.
    """
    if local_model:
        return True
    kw = route_kwargs or {}
    tier = kw.get("tier")
    if tier is not None:
        try:
            from core.tier_resolver import resolve_tier
            return is_deployment_local(resolve_tier(Tier(tier)).model_id)
        except Exception:  # noqa: BLE001 — a peek must never raise into a turn
            return False
    hint = (kw.get("model_hint") or "").strip().lower()
    if hint.startswith("local:"):
        return True
    # Governance off, or an explicit pick: the hint is all we have. For a
    # concrete model id the registry still knows the answer.
    return hint in _LEGACY_LOCAL_HINTS or is_deployment_local(kw.get("model_hint"))


# Hints that resolve to a specific Gemini model ID. Covers both the well-known
# literal (used by CLI / IDE clients) and the registry constant (used when an
# env override changes the resolved ID) — both must reach the same target.
_GEMINI_SPECIFIC_HINTS: dict = {m: m for m in (GEMINI_TEXT_MODEL, GEMINI_CODING_LITE_MODEL, GEMINI_IMAGE_MODEL)}
_GEMINI_SPECIFIC_HINTS.update({
    "gemini-3.5-flash":       GEMINI_TEXT_MODEL,
    "gemini-3.1-flash-lite":  GEMINI_CODING_LITE_MODEL,
    "gemini-3.1-flash-image": GEMINI_IMAGE_MODEL,
})

def _as_str(prompt) -> str:
    """Return the text content of prompt regardless of whether it is a str or messages list.
    Used internally for routing decisions (vision detection, complexity classification).
    """
    if isinstance(prompt, list):
        # Use the last user message content for routing signals
        for m in reversed(prompt):
            if m.get("role") == "user":
                return m.get("content") or ""
        return ""
    return prompt or ""


# Vision keyword detector
_VISION_RE = re.compile(
    r"\b(image|picture|photo|screenshot|diagram|chart|graph|"
    r"visual|figure|pixel|ocr|thumbnail|render|canvas|drawing)\b",
    re.IGNORECASE,
)

# Model-hint prefixes that indicate a non-vision local/code model. When the
# caller has already pinned one of these, vision auto-detection is skipped so
# that code-heavy conversations containing words like "render" or "canvas" are
# not silently rerouted to the Gemini image model.
_NON_VISION_HINT_PREFIXES = (
    "local", "kimi", "glm", "qwen", "deepseek", "llama", "gemma", "mistral",
    "mini", "medium", "complex", "deep", "solution", "haiku", "oss",
)


def _prompt_has_image(prompt: object) -> bool:
    """Return True only when the prompt contains a real image content block.

    Checks for Anthropic-style ``{"type": "image", "source": {...}}`` blocks
    and OpenAI-style ``{"type": "image_url", ...}`` blocks inside any message's
    content list. A plain string prompt never contains an image.
    """
    if not isinstance(prompt, list):
        return False
    for m in prompt:
        if not isinstance(m, dict):
            continue
        content = m.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type in ("image_url", "image"):
                return True
            # Anthropic source block: {"type": "image", "source": {"type": "base64"|"url", ...}}
            src = block.get("source", {})
            if isinstance(src, dict) and src.get("type") in ("base64", "url"):
                return True
    return False


# ============================================================
# ROUTING DECISION
# ============================================================

@dataclass
class RoutingDecision:
    tier:       str           # simple | medium | complex | vision
    model:      str           # display label
    complexity: str           # raw complexity from classifier
    is_vision:  bool
    hint:       Optional[str]
    fallback:   bool          # True if primary was unavailable
    # Optional provider-specific model ID override. When set, the dispatcher
    # forwards this to the chosen gateway instead of letting the gateway use
    # its module-level default. Provider-agnostic so future OpenAI/Claude
    # multi-model splits can reuse the same channel.
    provider_model_override: Optional[str] = None
    # Phase 5, governed path only. `resolved` is the FULL ordered candidate
    # list, not just the winner: §M.5's within-tier fallback ("candidate 1
    # fails, candidate 2 is tried") is a dispatch-time question, and re-asking
    # the resolver after a failed call would return the same model — one
    # failure does not open a circuit breaker.
    resolved: Optional[list] = None
    requested_tier: Optional[object] = None   # core.tiers.Tier, for §L.5 audit


@dataclass
class FallbackInfo:
    """Describes the routing decision made by the last generate() call.

    Exported so callers can do:
        from models.model_router import FallbackInfo
        fi: FallbackInfo = model_router.last_decision

    Fields
    ------
    fallback_occurred : bool
        True when the primary gateway was unavailable and a different one was
        selected.  False when the primary was used successfully.
    from_tier : str
        The tier that was originally requested (matches RoutingDecision.tier).
    from_label : str
        Human-readable display label for the original tier (e.g.
        "Claude Sonnet 4.6 (claude-sonnet-4-6)").
    to_tier : str
        The tier that was actually used.  Equals from_tier when no fallback.
    to_label : str
        Human-readable display label for the tier that was actually used.
        When no fallback this is the same as from_label.
    reason : str
        Short machine-readable reason tag.
        "primary"       — normal path, no fallback.
        "tier_fallback" — the RESOLVER walked TIER_FALLBACK_LADDER: nothing
                          assigned to the requested tier survived the
                          constraints, so a weaker tier supplied the model.
        "unavailable"   — primary gateway was None / circuit breaker open.
        "error"         — primary call raised an exception.
        "empty"         — primary returned an empty or Error: response.
        "not_set"       — sentinel: no routed call yet on this thread.

    Notes
    -----
    * Set by _record_selection() — generate(), stream(), async_generate() and
      async_stream(). Reports the RESOLVER walking TIER_FALLBACK_LADDER.
    * Does NOT report a gateway failing over mid-call; that stays visible only
      through last_model_label's "[fallback]" suffix, on generate() only.
    * generate_structured() does not call _record_selection(), so it leaves
      this and the §L.5 fields untouched (pre-existing).
    * Thread-safe: backed by threading.local() on ModelRouter._tl.
    """
    fallback_occurred: bool
    from_tier:         str
    from_label:        str
    to_tier:           str
    to_label:          str
    reason:            str


# Sentinel used when generate() has not yet been called on a thread.
_FALLBACK_INFO_NOT_SET = FallbackInfo(
    fallback_occurred=False,
    from_tier="",
    from_label="",
    to_tier="",
    to_label="",
    reason="not_set",
)


def _local_display_label(tier: str = "simple") -> str:
    """Human-readable label for the local model currently selected for a tier."""
    try:
        from gateway_local_llm import get_local_gateway
        gw = get_local_gateway()
        mid = gw._catalog_pick(tier) if hasattr(gw, "_catalog_pick") else None
        if mid:
            return f"Local ({mid})"
    except Exception:
        pass
    return LOCAL_LLM_DISPLAY


# Display label for a specific Gemini model ID. Used so that the streaming
# meta and chat "model name" footer reflect the EXACT model the user picked
# (e.g. gemini-3.1-flash-lite) instead of collapsing to the generic
# TIER_GEMINI label (which would always show gemini-3.5-flash).
def _gemini_model_label(model_id: str) -> str:
    if model_id == GEMINI_TEXT_MODEL:
        return f"{GEMINI_TEXT_DISPLAY} ({GEMINI_TEXT_MODEL})"
    if model_id == GEMINI_CODING_LITE_MODEL:
        return f"{GEMINI_CODING_LITE_DISPLAY} ({GEMINI_CODING_LITE_MODEL})"
    if model_id == GEMINI_IMAGE_MODEL:
        return f"{GEMINI_IMAGE_DISPLAY} ({GEMINI_IMAGE_MODEL})"
    return f"{GEMINI_DISPLAY} ({model_id})"


# Model display labels per tier — evaluated at routing time so the label
# reflects the live model list rather than a boot-time snapshot.
def _tier_label(tier: str) -> str:
    if tier == TIER_MINI:
        return f"{OPENAI_SIMPLE_DISPLAY} ({_resolve_tier_model(OPENAI_SIMPLE_MODEL, 'openai', 'simple')})"
    if tier == TIER_LOCAL_MINI:
        return f"{OPENAI_OSS_DISPLAY} ({_resolve_tier_model(OPENAI_OSS_MODEL, 'openai', 'oss')})"
    if tier == TIER_SIMPLE:
        try:
            from gateway_local_llm import _catalog
            mid = _catalog.pick("simple")
            return f"{LOCAL_LLM_DISPLAY} ({mid})" if mid else LOCAL_LLM_DISPLAY
        except Exception:
            return LOCAL_LLM_DISPLAY
    if tier == TIER_MEDIUM:
        return f"{OPENAI_CODING_DISPLAY} ({_resolve_tier_model(OPENAI_CODING_MODEL, 'openai', 'medium')})"
    if tier == TIER_COMPLEX:
        return f"{CLAUDE_PRIMARY_DISPLAY} ({_resolve_tier_model(CLAUDE_PRIMARY_MODEL, 'anthropic', 'complex')})"
    if tier == TIER_HAIKU:
        return f"{CLAUDE_HAIKU_DISPLAY} ({_resolve_tier_model(CLAUDE_HAIKU, 'anthropic', 'haiku')})"
    if tier == TIER_VISION:
        return f"{GEMINI_IMAGE_DISPLAY} ({GEMINI_IMAGE_MODEL})"
    if tier == TIER_GEMINI:
        return f"{GEMINI_TEXT_DISPLAY} ({GEMINI_TEXT_MODEL})"
    if tier == TIER_SOLUTION:
        if ENABLE_OPUS:
            return f"{CLAUDE_OPUS_DISPLAY} ({CLAUDE_OPUS_MODEL})"
        return f"{CLAUDE_PRIMARY_DISPLAY} ({CLAUDE_PRIMARY_MODEL}) [solution]"
    if tier == TIER_OPUS_48:
        return f"{CLAUDE_OPUS_48_DISPLAY} ({_resolve_tier_model(CLAUDE_OPUS_48_MODEL, 'anthropic', 'opus-4-8')})"
    if tier == TIER_OPUS_5:
        return f"{CLAUDE_OPUS_5_DISPLAY} ({_resolve_tier_model(CLAUDE_OPUS_5_MODEL, 'anthropic', 'opus-5')})"
    if tier == TIER_SONNET_5:
        return f"{CLAUDE_SONNET_5_DISPLAY} ({_resolve_tier_model(CLAUDE_SONNET_5_MODEL, 'anthropic', 'sonnet-5')})"
    if tier == TIER_DEEP:
        return f"{OPENAI_LATEST_DISPLAY} ({_resolve_tier_model(OPENAI_LATEST_MODEL, 'openai', 'deep')})"
    if tier == TIER_TERA:
        return f"{OPENAI_TERA_DISPLAY} ({_resolve_tier_model(OPENAI_TERA_MODEL, 'openai', 'gpt56-tera')})"
    if tier == TIER_LUNA:
        return f"{OPENAI_LUNA_DISPLAY} ({_resolve_tier_model(OPENAI_LUNA_MODEL, 'openai', 'gpt56-luna')})"
    return "Unknown"


def hint_to_model_id(hint: str) -> Optional[str]:
    """Resolve a model hint string to a concrete model ID, using the same
    _HINT_MAP + model_registry constants that the router uses at runtime.

    All model IDs are read from model_registry (which reads from .env), so
    changing CLAUDE_PRIMARY_MODEL, OPENAI_CODING_MODEL, etc. in .env is
    automatically reflected here — no code change needed.

    Returns None for "simple" / "local" hints (local LLM — caller must
    resolve via q.local_model).  Returns the hint as-is for "local:<id>"
    prefixed hints so the caller can pass it straight to filter_allowed_models.
    """
    if not hint:
        return None

    key = hint.lower().strip()

    # "local:<model-id>" — pass through as-is (e.g. "local:Kimi-k2.5")
    if key.startswith("local:"):
        return key

    # "simple" / "local" — local LLM, no concrete cloud model ID
    if key in ("simple", "local"):
        return None

    # Resolve via _HINT_MAP → tier → concrete model ID
    tier = _HINT_MAP.get(key)
    if tier is None:
        return None

    # tier → concrete model ID (mirrors _tier_label but returns just the ID)
    _tier_map = {
        TIER_MINI:      OPENAI_SIMPLE_MODEL,
        TIER_MEDIUM:    OPENAI_CODING_MODEL,
        TIER_DEEP:      OPENAI_LATEST_MODEL,
        TIER_COMPLEX:   CLAUDE_PRIMARY_MODEL,
        TIER_HAIKU:     CLAUDE_HAIKU,
        TIER_SOLUTION:  CLAUDE_OPUS_MODEL if ENABLE_OPUS else CLAUDE_PRIMARY_MODEL,
        # TIER_OPUS_46 removed — claude-opus-4-6 is retired and always in BLOCKED_MODELS
        TIER_OPUS_48:   CLAUDE_OPUS_48_MODEL,
        TIER_OPUS_5:    CLAUDE_OPUS_5_MODEL,
        TIER_SONNET_5:  CLAUDE_SONNET_5_MODEL,
        TIER_VISION:    GEMINI_IMAGE_MODEL,
        TIER_GEMINI:    GEMINI_TEXT_MODEL,
        TIER_TERA:      OPENAI_TERA_MODEL,
        TIER_LUNA:      OPENAI_LUNA_MODEL,
    }
    return _tier_map.get(tier)


def _registry_has_family(family: str) -> bool:
    """True if at least one enabled registry model exists for `family`.

    Used by _get_claude()/_get_openai()/_get_gemini()'s "is this provider
    configured at all" gate, alongside the .env role constants. Without this,
    those gates returned None whenever CLAUDE_PRIMARY_MODEL/OPENAI_SIMPLE_
    MODEL/GEMINI_TEXT_MODEL was blank — which install.sh's admin-only
    provider setup always leaves blank — silently breaking "Auto (Routing)"
    and every complexity-tier dispatch even though _resolve_tier_model()
    (see below) fixed what model gets requested once a gateway is obtained.
    """
    try:
        from core.llm_provider_registry import get_enabled_models
        return any(m["family"] == family for m in get_enabled_models())
    except Exception:
        return False


def _resolve_tier_model(env_value: str, family: str, tag: str) -> str:
    """Fall back to a registry-configured model when a .env role-specific
    constant (CLAUDE_PRIMARY_MODEL, OPENAI_CODING_MODEL, etc.) is blank.

    install.sh's "LLM Providers" flow only ever writes the raw API key to
    .env — it never sets these per-role model overrides — so a deployment
    configured purely through the admin screen has every one of these
    constants blank, which silently broke "Auto (Routing)" and every
    built-in alias (claude/gpt/mini/...) even after core.llm_provider_registry
    made specific models selectable/dispatchable (see the LLM provider
    config design doc's post-launch fixes).

    No-op when env_value is already set — zero behavior change for any
    deployment that has .env role vars configured. Tries an enabled
    registry model of `family` tagged `tag` first (same tag vocabulary
    db/migrate.py's Part AC1 backfill uses), then any enabled model of that
    family, else "" (unchanged from today's blank-constant behavior).
    """
    if env_value:
        return env_value
    try:
        from core.llm_provider_registry import get_enabled_models
        candidates = [m for m in get_enabled_models() if m["family"] == family]
        tagged = [m for m in candidates if tag in (m["capabilities"].get("tier_tags") or [])]
        pick = tagged[0] if tagged else (candidates[0] if candidates else None)
        if not pick:
            logger.warning(
                "ModelRouter: _resolve_tier_model found no enabled '%s' model "
                "(tag=%r) in the provider registry — dispatch will receive an "
                "empty model id and the upstream API call will fail.",
                family, tag,
            )
            return ""
        return pick["model_id"]
    except Exception as e:
        logger.warning(
            "ModelRouter: _resolve_tier_model(family=%r, tag=%r) raised %s — "
            "returning empty model id.", family, tag, e,
        )
        return ""


# ============================================================
# TIER VOCABULARY BRIDGE  (Phase 1 — additive, nothing uses it yet)
# ============================================================
#
# Maps the eight approved application tiers (core.tiers.Tier) onto the legacy
# internal hint strings this router already understands, so Phase 1 can
# introduce the new vocabulary with PROVABLY zero behaviour change: a `tier=`
# call is rewritten to the equivalent `model_hint=` call before any routing
# logic runs, and every existing `model_hint=` call is untouched.
#
# ⚠ Tier.SIMPLE maps to "haiku", NOT to "simple".
#   The legacy string "simple" means LOCAL. The new Tier.SIMPLE means "cheap,
#   short-output". They are different requests and must stay different until
#   each call site is migrated individually in Phase 6 (plan.html §D.2).
#   This asymmetry is the most important thing in this block; it is locked
#   down by tests/models/test_tiers.py::test_simple_collision_guarded.
#
# This map lives here rather than in core/tiers.py on purpose: core.tiers is a
# leaf module and must not know the router's internal tier constants.
_TIER_TO_LEGACY_HINT: dict = {
    Tier.MINI:                  "mini",
    Tier.SIMPLE:                "haiku",
    Tier.MEDIUM:                "medium",
    Tier.COMPLEX:               "complex",
    Tier.INTENT_CLASSIFICATION: "haiku",
    Tier.IMAGE_INPUT:           "vision",
    # PHASE-1 STUBS. Image generation and Veo bypass this router entirely
    # today (routers/chat_router.py calls the gateways directly), so there is
    # no distinct dispatch to point at yet. Both are unreachable in Phase 1
    # because no production code passes tier= at all. Real dispatch arrives in
    # Phase 5 — until then these exist only so the map is total over Tier.
    Tier.IMAGE_OUTPUT:          "vision",
    Tier.VIDEO_GENERATION:      "vision",
}


# ============================================================
# LEGACY TIER → GOVERNED TIER  (Phase 5)
# ============================================================
#
# Which of the router's internal tiers are a CAPABILITY REQUEST, and therefore
# the platform's decision to govern, versus a USER'S PICK, which governance
# must not second-guess. Seven of the sixteen qualify. Per plan.html §E.
#
# The nine that are absent, and why each one is:
#
#   simple, local_mini   The §D.2 reclassification. The literal string
#                        "simple" means LOCAL here and means "cheap, short
#                        output" in the new vocabulary — they are different
#                        requests (R1). Which of the ~18 call sites becomes
#                        which tier is decided per call site in Phase 6, and
#                        that sign-off has not happened. Auto-mapping them now
#                        would guess.
#   gemini, opus-4-8,    Named SKUs a user picked from a dropdown or a CLI
#   opus-5, sonnet-5,    --model flag. §E deletes them as TIERS while keeping
#   tera, luna           every one of them selectable. Resolving them through
#                        a tier would substitute a different model for the one
#                        the user asked for, which is the specific failure
#                        this migration exists to remove — not to introduce.
#   registry             Already the user-explicit path.
#
# The three modality tiers (image-output, video-generation) and
# intent-classification have no legacy equivalent at all, so they are
# reachable only through a `tier=` call. Phase 5 is the first release in which
# such a call dispatches rather than resolving to a Phase 1 stub.

_LEGACY_TO_GOVERNED: dict = {
    TIER_MINI:     (Tier.MINI,        {}),
    # §E: "core/model_registry.py already documents simple as the
    # provider-neutral operator name for the cheap/fast tier (internally keyed
    # haiku) — this completes a rename the codebase had already started."
    TIER_HAIKU:    (Tier.SIMPLE,      {}),
    TIER_MEDIUM:   (Tier.MEDIUM,      {}),
    TIER_COMPLEX:  (Tier.COMPLEX,     {}),
    # §M.3a — a role, not an eleventh tier. require_role is a PREFERENCE in
    # the resolver, so a single-model deployment still runs the review stage
    # with author and reviewer coinciding, which the admin screen shows.
    TIER_SOLUTION: (Tier.COMPLEX,     {"require_role": _ROLE_REVIEW}),
    # §M.2 — "deep" existed to be the context-promotion target. The window is
    # a property of the model, so it arrives as min_context_window on the
    # constraints instead of as a different tier.
    TIER_DEEP:     (Tier.COMPLEX,     {}),
    TIER_VISION:   (Tier.IMAGE_INPUT, {}),
}


# ============================================================
# DISPATCH BY FAMILY  (Phase 5)
# ============================================================
#
# Until Phase 5 there were fifteen hand-written `_try_<provider>_<tier>`
# methods and fifteen streaming twins, each one an ordered list of attempts
# ("call GPT-5.4; if that fails call Sonnet; if that fails call the local
# model") expressed as nested if/try blocks. They all did the same five things
# in the same order — resolve a gateway, check a breaker, call, judge the
# result, set the label — so the only thing that actually differed between them
# was the LIST. That list is now data, and the control flow exists once.
#
# Two reasons this matters beyond tidiness:
#
#   1. The governed path (see route()) has no fixed list. It gets its ordered
#      candidates from tier_resolver.resolve_tier_candidates(), which is a
#      different SOURCE for the same SHAPE. One dispatcher serves both.
#   2. Every one of the fifteen methods had to be corrected independently
#      whenever the fallback rules changed, and they had drifted — the blocking
#      and streaming halves of the same tier disagree in several places (see
#      the per-attempt flags below).
#
# ⚠ THE TABLE BELOW IS A TRANSCRIPTION, NOT A DESIGN. Several entries encode
#   behaviour nobody would choose on purpose: TIER_MEDIUM's blocking primary
#   sends no `model` at all while its streaming twin does; TIER_SOLUTION
#   reports was_fallback=True even when every hop failed; TIER_OPUS_48 never
#   sets _last_actual_tier. Since Phase 8 only user SKU picks and Auto's local
#   tiers still reach this table; Rev 22 stage 8.3 deletes it with them.


@dataclass(frozen=True)
class _Attempt:
    """One hop in a fallback chain.

    `model` and `label` are callables rather than strings because both are
    resolved at CALL time in the code this replaces — an operator who changes
    CLAUDE_PRIMARY_MODEL, or a catalogue that re-picks a local model, must be
    reflected on the next request without a restart. They take the request
    context (`local_model` / `provider_model`) and, for labels, the gateway
    that served the call.
    """

    family: str                                  # local | openai | claude | gemini
    model: Optional[object] = None               # (ctx) -> id, or None to omit model=
    label: Optional[object] = None               # (ctx, gw) -> str, or None to leave it
    tier: Optional[str] = None                   # _last_actual_tier, or None to leave it
    forward_kwargs: bool = False                 # forward the caller's compliance kwargs
    check_error: bool = True                     # a leading-"Error" result means "try next"
    empty_is_error: bool = False                 # "" also means "try next" (local only)
    fallback: Optional[bool] = None              # override the returned was_fallback
    extra: Optional[dict] = None                 # constant kwargs, e.g. tier="simple"
    capture_thinking: bool = False               # copy the gateway's extended-thinking text
    # Governed path only. The legacy chains name a PROVIDER FAMILY and get one
    # of the four cached singletons; a resolved candidate names a REGISTRY ROW
    # and may need a gateway built from that row's own base_url and key (an
    # openai_compatible endpoint has no singleton at all). Likewise the breaker:
    # the legacy chains share one per family, a resolved candidate gets its own
    # provider:model key so a single bad model cannot fail-fast its siblings.
    gateway: Optional[object] = None             # (router) -> gateway | None
    breaker_key: Optional[str] = None            # get_breaker(key), not _breaker_for(family)


def _breaker_for(family: str):
    """Module globals read at CALL time — tests replace these singletons."""
    return {"local": _CB_LOCAL, "openai": _CB_OPENAI,
            "claude": _CB_CLAUDE, "gemini": _CB_GEMINI}[family]


# ── Label builders ──────────────────────────────────────────────────────────
# Written as globals-referencing lambdas so that an env override or a
# monkeypatched display constant is picked up on the next call, exactly as the
# inline f-strings they replace were.

def _lbl_local(suffix: str = "", from_ctx: bool = True):
    """The local label reads back the model the gateway ACTUALLY picked.

    `from_ctx=False` is _try_openai_coding's last-resort local hop, which
    ignores any local_model override because it never had one — it arrived
    there from a cloud tier.
    """
    def _f(ctx, gw):
        actual = (ctx.get("local_model") if from_ctx else None) \
            or getattr(gw, "_last_selected_model", None)
        return f"Local ({actual}){suffix}" if actual else f"{_tier_label(TIER_SIMPLE)}{suffix}"
    return _f


def _lbl(display_name: str, model_fn, suffix: str = ""):
    return lambda ctx, gw: f"{globals()[display_name]} ({model_fn(ctx)}){suffix}"


# ── Model resolvers ─────────────────────────────────────────────────────────
_M_OPENAI_SIMPLE = lambda ctx: _resolve_tier_model(OPENAI_SIMPLE_MODEL, "openai", "simple")     # noqa: E731
_M_OPENAI_CODING = lambda ctx: _resolve_tier_model(OPENAI_CODING_MODEL, "openai", "medium")     # noqa: E731
_M_OPENAI_DEEP   = lambda ctx: _resolve_tier_model(OPENAI_LATEST_MODEL, "openai", "deep")       # noqa: E731
_M_OPENAI_OSS    = lambda ctx: _resolve_tier_model(OPENAI_OSS_MODEL,    "openai", "oss")        # noqa: E731
_M_OPENAI_TERA   = lambda ctx: _resolve_tier_model(OPENAI_TERA_MODEL,   "openai", "gpt56-tera") # noqa: E731
_M_OPENAI_LUNA   = lambda ctx: _resolve_tier_model(OPENAI_LUNA_MODEL,   "openai", "gpt56-luna") # noqa: E731
_M_CLAUDE_MAIN   = lambda ctx: _resolve_tier_model(CLAUDE_PRIMARY_MODEL, "anthropic", "complex")# noqa: E731
_M_CLAUDE_HAIKU  = lambda ctx: _resolve_tier_model(CLAUDE_HAIKU,        "anthropic", "haiku")   # noqa: E731
_M_CLAUDE_OPUS48 = lambda ctx: _resolve_tier_model(CLAUDE_OPUS_48_MODEL, "anthropic", "opus-4-8")# noqa: E731
_M_CLAUDE_OPUS5  = lambda ctx: _resolve_tier_model(CLAUDE_OPUS_5_MODEL, "anthropic", "opus-5")  # noqa: E731
_M_CLAUDE_SON5   = lambda ctx: _resolve_tier_model(CLAUDE_SONNET_5_MODEL, "anthropic", "sonnet-5")# noqa: E731
_M_SOLUTION      = lambda ctx: SOLUTION_MODEL                                                   # noqa: E731
_M_CTX_LOCAL     = lambda ctx: ctx.get("local_model") or None                                   # noqa: E731
_M_CTX_PROVIDER  = lambda ctx: ctx.get("provider_model") or None                                # noqa: E731


# ── Reusable hops ───────────────────────────────────────────────────────────
# The Sonnet-then-GPT pair below is _try_claude_sonnet's whole body, and five
# other chains end by delegating to it. `fallback=` is pinned explicitly on
# those copies because the delegating methods return the INNER call's flag
# verbatim — so a Sonnet success reached via Opus 4.8 reports was_fallback
# FALSE today, despite plainly being a fallback. Transcribed, not fixed.
def _hop_claude_main(*, primary: bool, fallback=None, forward=False):
    return _Attempt(
        family="claude", model=_M_CLAUDE_MAIN, tier=TIER_COMPLEX,
        label=_lbl("CLAUDE_PRIMARY_DISPLAY", _M_CLAUDE_MAIN,
                   "" if primary else " [fallback]"),
        check_error=primary, forward_kwargs=forward, fallback=fallback,
    )


def _hop_openai_coding_fallback(fallback=None):
    return _Attempt(
        family="openai", model=None, tier=TIER_MEDIUM, forward_kwargs=True,
        label=_lbl("OPENAI_CODING_DISPLAY", _M_OPENAI_CODING, " [fallback]"),
        check_error=False, fallback=fallback,
    )


# _try_claude_sonnet's body, reused by the three SKU tiers and by solution.
def _chain_sonnet(*, fallback_primary=None, fallback_secondary=None):
    return [_hop_claude_main(primary=True, fallback=fallback_primary),
            _hop_openai_coding_fallback(fallback=fallback_secondary)]


_A_LOCAL_PRIMARY = _Attempt(
    family="local", model=_M_CTX_LOCAL, tier=TIER_SIMPLE,
    label=_lbl_local(), extra={"tier": "simple"}, empty_is_error=True,
)
_A_OPENAI_MINI_FALLBACK = _Attempt(
    family="openai", model=None, tier=TIER_MINI, forward_kwargs=True,
    label=_lbl("OPENAI_SIMPLE_DISPLAY", _M_OPENAI_SIMPLE, " [fallback]"),
)
_A_LOCAL_LAST_RESORT = _Attempt(
    family="local", model=None, tier=TIER_SIMPLE, extra={"tier": "simple"},
    label=_lbl_local(" [fallback]", from_ctx=False), empty_is_error=True,
)


# Streaming differs from blocking in three ways that are not worth hiding:
# the label is set BEFORE the call (there is no result to judge first), the
# circuit breaker is only CHECKED and never wrapped around the call, and a
# leading-"Error" token is passed through to the client rather than triggering
# the next hop. The one exception is the local hops, which count tokens and
# fall through when none arrived — `empty_is_error` marks those in both
# dispatchers.
def _hop_openai_coding_fallback_stream():
    """Streaming sends `model` here; the blocking twin does not. Not a typo —
    see _try_claude_sonnet vs _try_claude_sonnet_stream in git history."""
    return _Attempt(
        family="openai", model=_M_OPENAI_CODING, tier=TIER_MEDIUM, forward_kwargs=True,
        label=_lbl("OPENAI_CODING_DISPLAY", _M_OPENAI_CODING, " [fallback]"),
        check_error=False,
    )


def _chain_sonnet_stream():
    return [
        _Attempt(family="claude", model=_M_CLAUDE_MAIN, tier=TIER_COMPLEX,
                 label=_lbl("CLAUDE_PRIMARY_DISPLAY", _M_CLAUDE_MAIN),
                 check_error=False, capture_thinking=True),
        _hop_openai_coding_fallback_stream(),
    ]


def _a_openai(model_fn, display: str, tier: str, suffix: str = "", *, forward=True):
    return _Attempt(family="openai", model=model_fn, tier=tier, forward_kwargs=forward,
                    label=_lbl(display, model_fn, suffix))


def _a_claude(model_fn, display: str, tier: Optional[str], suffix: str = "", *,
              check_error=True, fallback=None):
    return _Attempt(family="claude", model=model_fn, tier=tier,
                    label=_lbl(display, model_fn, suffix),
                    check_error=check_error, fallback=fallback)


_A_GEMINI = _Attempt(
    # label is deliberately absent: route() has already set a model-specific
    # Gemini label and overwriting it here would collapse every Gemini variant
    # to one generic string.
    family="gemini", model=_M_CTX_PROVIDER, tier=TIER_VISION, forward_kwargs=True,
)
_A_LOCAL_STREAM = _Attempt(
    family="local", model=_M_CTX_LOCAL, tier=TIER_SIMPLE,
    label=_lbl_local(), extra={"tier": "simple"}, empty_is_error=True,
)
_A_LOCAL_LAST_RESORT_STREAM = _Attempt(
    family="local", model=None, tier=TIER_SIMPLE, extra={"tier": "simple"},
    label=_lbl_local(" [fallback]", from_ctx=False), empty_is_error=True,
)

_CHAIN_SIMPLE_SYNC = [_A_LOCAL_PRIMARY, _A_OPENAI_MINI_FALLBACK,
                      _hop_claude_main(primary=False)]
_CHAIN_SIMPLE_STREAM = [
    _A_LOCAL_STREAM,
    _Attempt(family="openai", model=_M_OPENAI_SIMPLE, tier=TIER_MINI, forward_kwargs=True,
             label=_lbl("OPENAI_SIMPLE_DISPLAY", _M_OPENAI_SIMPLE, " [fallback]"),
             check_error=False),
    _Attempt(family="claude", model=_M_CLAUDE_MAIN, tier=TIER_COMPLEX,
             label=_lbl("CLAUDE_PRIMARY_DISPLAY", _M_CLAUDE_MAIN, " [fallback]"),
             check_error=False),
]

_LEGACY_CHAIN: dict = {
    TIER_SIMPLE: {"sync": _CHAIN_SIMPLE_SYNC, "stream": _CHAIN_SIMPLE_STREAM},

    # local_mini is not its own chain: _dispatch routes it to the local gateway
    # with OPENAI_OSS_MODEL as the pinned model. _try_openai_oss below is the
    # in-house-OpenAI-endpoint variant and is reachable only by direct call.
    TIER_LOCAL_MINI: {"sync": _CHAIN_SIMPLE_SYNC, "stream": _CHAIN_SIMPLE_STREAM},

    TIER_MINI: {
        "sync": [_a_openai(_M_OPENAI_SIMPLE, "OPENAI_SIMPLE_DISPLAY", TIER_MINI),
                 _hop_claude_main(primary=False)],
        # No claude hop: the streaming twin walks CHAT_FALLBACK_CHAIN instead,
        # which is env-configured and may be empty. Handled by walk_chain below.
        "stream": [_a_openai(_M_OPENAI_SIMPLE, "OPENAI_SIMPLE_DISPLAY", TIER_MINI)],
        "walk_chain": True,
    },

    TIER_MEDIUM: {
        # model= is genuinely absent on the blocking primary: it relies on the
        # OpenAI gateway's own module default. The streaming twin sends it.
        "sync": [_Attempt(family="openai", model=None, tier=TIER_MEDIUM, forward_kwargs=True,
                          label=_lbl("OPENAI_CODING_DISPLAY", _M_OPENAI_CODING)),
                 _hop_claude_main(primary=False),
                 _A_LOCAL_LAST_RESORT],
        "stream": [_a_openai(_M_OPENAI_CODING, "OPENAI_CODING_DISPLAY", TIER_MEDIUM),
                   _Attempt(family="claude", model=_M_CLAUDE_MAIN, tier=TIER_COMPLEX,
                            label=_lbl("CLAUDE_PRIMARY_DISPLAY", _M_CLAUDE_MAIN, " [fallback]"),
                            check_error=False),
                   _A_LOCAL_LAST_RESORT_STREAM],
    },

    TIER_DEEP: {
        "sync": [_a_openai(_M_OPENAI_DEEP, "OPENAI_LATEST_DISPLAY", TIER_DEEP),
                 _hop_claude_main(primary=False)],
        "stream": [_a_openai(_M_OPENAI_DEEP, "OPENAI_LATEST_DISPLAY", TIER_DEEP),
                   _Attempt(family="claude", model=_M_CLAUDE_MAIN, tier=TIER_COMPLEX,
                            label=_lbl("CLAUDE_PRIMARY_DISPLAY", _M_CLAUDE_MAIN, " [fallback]"),
                            check_error=False)],
    },

    TIER_TERA: {
        "sync": [_a_openai(_M_OPENAI_TERA, "OPENAI_TERA_DISPLAY", TIER_TERA),
                 _hop_claude_main(primary=False)],
        "stream": [_a_openai(_M_OPENAI_TERA, "OPENAI_TERA_DISPLAY", TIER_TERA),
                   _Attempt(family="claude", model=_M_CLAUDE_MAIN, tier=TIER_COMPLEX,
                            label=_lbl("CLAUDE_PRIMARY_DISPLAY", _M_CLAUDE_MAIN, " [fallback]"),
                            check_error=False)],
    },

    TIER_LUNA: {
        "sync": [_a_openai(_M_OPENAI_LUNA, "OPENAI_LUNA_DISPLAY", TIER_LUNA),
                 _hop_claude_main(primary=False)],
        "stream": [_a_openai(_M_OPENAI_LUNA, "OPENAI_LUNA_DISPLAY", TIER_LUNA),
                   _Attempt(family="claude", model=_M_CLAUDE_MAIN, tier=TIER_COMPLEX,
                            label=_lbl("CLAUDE_PRIMARY_DISPLAY", _M_CLAUDE_MAIN, " [fallback]"),
                            check_error=False)],
    },

    TIER_COMPLEX: {"sync": _chain_sonnet(), "stream": _chain_sonnet_stream()},

    TIER_HAIKU: {
        "sync": [_a_claude(_M_CLAUDE_HAIKU, "CLAUDE_HAIKU_DISPLAY", TIER_HAIKU),
                 _hop_openai_coding_fallback()],
        "stream": [_Attempt(family="claude", model=_M_CLAUDE_HAIKU, tier=TIER_HAIKU,
                            label=_lbl("CLAUDE_HAIKU_DISPLAY", _M_CLAUDE_HAIKU),
                            check_error=False),
                   _hop_openai_coding_fallback_stream()],
    },

    TIER_VISION: {"sync": [_A_GEMINI, _hop_claude_main(primary=False)],
                  "stream": [_A_GEMINI,
                             _Attempt(family="claude", model=_M_CLAUDE_MAIN, tier=TIER_COMPLEX,
                                      label=_lbl("CLAUDE_PRIMARY_DISPLAY", _M_CLAUDE_MAIN,
                                                 " [fallback]"),
                                      check_error=False)]},

    TIER_SOLUTION: {
        # was_fallback is forced True on EVERY outcome, including total
        # failure, because the method delegates to _try_claude_sonnet and
        # overwrites the flag unconditionally. exhausted_fallback carries the
        # "even when nothing worked" half of that.
        "sync": [_Attempt(family="claude", model=_M_SOLUTION, tier=TIER_SOLUTION,
                          label=lambda ctx, gw: _tier_label(TIER_SOLUTION))]
                + _chain_sonnet(fallback_primary=True, fallback_secondary=True),
        "stream": [_Attempt(family="claude", model=_M_SOLUTION, tier=TIER_SOLUTION,
                            label=lambda ctx, gw: _tier_label(TIER_SOLUTION),
                            check_error=False)] + _chain_sonnet_stream(),
        "exhausted_fallback": True,
    },

    # The three SKU tiers delegate to _try_claude_sonnet and return ITS flag
    # verbatim, so a Sonnet success reached from here reports was_fallback
    # False. Pinned with fallback= rather than corrected.
    TIER_OPUS_48: {
        # tier=None: _try_claude_opus48 is the one method that never sets
        # _last_actual_tier on success. Token accounting therefore reads the
        # PREVIOUS request's tier for this one. Transcribed as-is.
        "sync": [_a_claude(_M_CLAUDE_OPUS48, "CLAUDE_OPUS_48_DISPLAY", None, fallback=False)]
                + _chain_sonnet(fallback_primary=False, fallback_secondary=True),
        "stream": [_Attempt(family="claude", model=_M_CLAUDE_OPUS48, tier=None,
                            label=_lbl("CLAUDE_OPUS_48_DISPLAY", _M_CLAUDE_OPUS48),
                            check_error=False)] + _chain_sonnet_stream(),
    },
    TIER_OPUS_5: {
        "sync": [_a_claude(_M_CLAUDE_OPUS5, "CLAUDE_OPUS_5_DISPLAY", TIER_OPUS_5, fallback=False)]
                + _chain_sonnet(fallback_primary=False, fallback_secondary=True),
        "stream": [_Attempt(family="claude", model=_M_CLAUDE_OPUS5, tier=TIER_OPUS_5,
                            label=_lbl("CLAUDE_OPUS_5_DISPLAY", _M_CLAUDE_OPUS5),
                            check_error=False)] + _chain_sonnet_stream(),
    },
    TIER_SONNET_5: {
        "sync": [_a_claude(_M_CLAUDE_SON5, "CLAUDE_SONNET_5_DISPLAY", TIER_SONNET_5, fallback=False)]
                + _chain_sonnet(fallback_primary=False, fallback_secondary=True),
        "stream": [_Attempt(family="claude", model=_M_CLAUDE_SON5, tier=TIER_SONNET_5,
                            label=_lbl("CLAUDE_SONNET_5_DISPLAY", _M_CLAUDE_SON5),
                            check_error=False)] + _chain_sonnet_stream(),
    },

    # Reachable only by direct call to _try_openai_oss(): _dispatch sends
    # TIER_LOCAL_MINI to the local gateway instead. Kept because callers
    # outside the router still use it.
    "_oss": {
        "sync": [_a_openai(_M_OPENAI_OSS, "OPENAI_OSS_DISPLAY", TIER_LOCAL_MINI),
                 _a_openai(_M_OPENAI_SIMPLE, "OPENAI_SIMPLE_DISPLAY", TIER_MINI, " [fallback]")],
        "stream": [_a_openai(_M_OPENAI_OSS, "OPENAI_OSS_DISPLAY", TIER_LOCAL_MINI),
                   _a_openai(_M_OPENAI_SIMPLE, "OPENAI_SIMPLE_DISPLAY", TIER_MINI, " [fallback]")],
    },
}


# ============================================================
# MODEL ROUTER
# ============================================================

class ModelRouter:
    """
    Routes every prompt to the approved LLM gateway.
    Gateways are lazily initialised — never crashes on import.
    """

    _tl = threading.local()   # thread-local storage — prevents cross-request label bleed

    def __init__(self):
        self._local   = None
        self._openai  = None
        self._claude  = None
        self._gemini  = None
        logger.info("ModelRouter initialised")

    # ── Tier → legacy hint coercion (Phase 1; legacy_hint added in Phase 6) ──
    @staticmethod
    def _coerce_tier(model_hint: Optional[str], tier: Optional[Tier],
                     legacy_hint: Optional[str] = None) -> Optional[str]:
        """Collapse `tier=`, `model_hint=` and `legacy_hint=` into one hint string.

        Called as the FIRST statement of every public entry point, so that the
        rest of the router keeps seeing exactly the legacy hint it always has.
        `tier=None` (the overwhelmingly common case during Phases 1-5) returns
        `model_hint` untouched — zero behaviour change by construction.

        ── legacy_hint (Phase 6, decision D15) ──────────────────────────────
        What the call site used to pass, kept alongside the tier it now asks
        for. Since Phase 8 a tier= call resolves or raises, so the coerced hint
        is no longer consumed for routing.

        This matters because _TIER_TO_LEGACY_HINT is not a faithful inverse of
        the migration: Tier.SIMPLE maps to "haiku" (cloud Claude Haiku) while
        the ~18 call sites becoming Tier.SIMPLE pass model_hint="simple" today,
        which means the LOCAL model. Without legacy_hint, Phase 6 would move
        every one of them local -> cloud on deployments that never opted in.
        Deleted in Phase 10 with the rest of the legacy chain.

        Raises ValueError when both `tier` and `model_hint` are supplied, when
        `legacy_hint` is supplied without `tier`, or when `legacy_hint` is not
        a key of _HINT_MAP. Note that generate() and friends document "never
        raises": that contract is about RUNTIME LLM failures, which they still
        convert to an error string. All three of these are programming errors
        — statically determinable, caught by tests/models/test_tiers.py and
        tests/models/test_legacy_hint_shim.py, and impossible to reach at
        runtime in a correct call site — so failing loudly is right.
        """
        if tier is None:
            if legacy_hint is not None:
                raise ValueError(
                    "ModelRouter: legacy_hint= requires tier= "
                    f"(got legacy_hint={legacy_hint!r} with no tier)"
                )
            return model_hint
        if model_hint is not None:
            raise ValueError(
                "ModelRouter: pass either tier= or model_hint=, not both "
                f"(got tier={tier!r}, model_hint={model_hint!r})"
            )
        if legacy_hint is not None:
            # Fail on a typo here rather than letting an unrecognised hint slide
            # through _HINT_MAP's lookup and silently become the medium default
            # — the whole point of legacy_hint is that it reproduces a SPECIFIC
            # prior behaviour, and one that quietly does not is worse than none.
            if legacy_hint not in _HINT_MAP:
                raise ValueError(
                    f"ModelRouter: legacy_hint={legacy_hint!r} is not a known "
                    "routing hint — it must name the hint this call site used "
                    "BEFORE it was migrated to tier=, so that governance-off "
                    "behaviour is unchanged."
                )
            return legacy_hint
        # Tier(tier) rejects a bare string that is not one of the eight, so a
        # legacy alias like "solution" or "local" can never sneak in this way.
        return _TIER_TO_LEGACY_HINT[Tier(tier)]

    # ── Thread-local per-request state ────────────────────────
    # These are properties backed by threading.local() so concurrent
    # requests can't overwrite each other's in-flight model label.
    @property
    def last_model_label(self) -> str:
        return getattr(self._tl, "last_model_label", "auto")
    @last_model_label.setter
    def last_model_label(self, v: str):
        self._tl.last_model_label = v

    @property
    def last_model_id(self) -> str:
        """Return the bare model ID from last_model_label.

        last_model_label is a display string like:
          "GPT-5.4 (gpt-5.4)"
          "Claude Sonnet 4.6 (claude-sonnet-4-6) [fallback]"
          "Local (local:Kimi-k2.5)"
          "auto"  ← default when no call has been made yet

        This property extracts the content of the last parenthesised group
        (before any trailing [fallback] / [solution] suffix) so callers that
        write to model_usages always store a clean, queryable model ID rather
        than a human-readable display string.

        Falls back to last_model_label as-is when no parenthesised group is
        found (e.g. "auto", "Unknown", plain IDs that were set directly).
        """
        import re as _re
        label = self.last_model_label
        # Find the last (...) group — strip trailing whitespace and [tag] suffixes.
        # [^)]* (not [^)]+) so an empty "()" — e.g. a display label built from an
        # unresolved/blank model id — still matches instead of falling through to
        # returning the entire raw label string as a bogus "model id".
        m = _re.search(r'\(([^)]*)\)\s*(?:\[[^\]]*\])?\s*$', label)
        if m:
            return m.group(1).strip() or "unknown"
        return label

    @property
    def last_tier(self) -> str:
        return getattr(self._tl, "last_tier", "auto")
    @last_tier.setter
    def last_tier(self, v: str):
        self._tl.last_tier = v

    @property
    def last_input_tokens(self) -> int:
        return getattr(self._tl, "last_input_tokens", 0)
    @last_input_tokens.setter
    def last_input_tokens(self, v: int):
        self._tl.last_input_tokens = v

    @property
    def last_output_tokens(self) -> int:
        return getattr(self._tl, "last_output_tokens", 0)
    @last_output_tokens.setter
    def last_output_tokens(self, v: int):
        self._tl.last_output_tokens = v

    @property
    def last_thinking_text(self) -> str:
        """Extended thinking content from the last Claude call, if any.
        Set by gateway_claude.py during streaming when thinking_delta events
        are observed. Read by gateway.py to emit a `thinking` field in the
        SSE __meta__ event so the UI can render a Reasoning panel."""
        return getattr(self._tl, "last_thinking_text", "") or ""
    @last_thinking_text.setter
    def last_thinking_text(self, v: str):
        self._tl.last_thinking_text = v or ""

    @property
    def last_cache_read_tokens(self) -> int:
        return getattr(self._tl, "last_cache_read_tokens", 0)
    @last_cache_read_tokens.setter
    def last_cache_read_tokens(self, v: int):
        self._tl.last_cache_read_tokens = v

    @property
    def last_cache_creation_tokens(self) -> int:
        return getattr(self._tl, "last_cache_creation_tokens", 0)
    @last_cache_creation_tokens.setter
    def last_cache_creation_tokens(self, v: int):
        self._tl.last_cache_creation_tokens = v

    @property
    def last_decision(self) -> "FallbackInfo":
        """FallbackInfo from the most recent generate() call on this thread.

        Returns the _FALLBACK_INFO_NOT_SET sentinel when generate() has not
        been called yet on the current thread (reason == "not_set").

        NOTE: NOT updated by stream() — see FallbackInfo docstring for details.
        """
        return getattr(self._tl, "last_decision", _FALLBACK_INFO_NOT_SET)
    @last_decision.setter
    def last_decision(self, v: "FallbackInfo"):
        self._tl.last_decision = v

    # _last_actual_tier: set by _try_* methods alongside last_model_label so that
    # generate() can build FallbackInfo with accurate from/to tier information.
    # ── §L.5 audit: why did this request use this model? ──────────────────
    # Written by _record_selection() right after route(), i.e. in the CALLER's
    # thread, which is what the six ainxt.metrics producers read. Deliberately
    # NULL on the legacy path: "the .env constants decided" is not one of the
    # three vocabulary values, and inventing a fourth would put a claim in the
    # audit trail that the column was created to avoid.
    @property
    def last_selection_mode(self) -> Optional[str]:
        return getattr(self._tl, "last_selection_mode", None)

    @last_selection_mode.setter
    def last_selection_mode(self, v: Optional[str]):
        self._tl.last_selection_mode = v

    @property
    def last_requested_tier(self) -> Optional[str]:
        return getattr(self._tl, "last_requested_tier", None)

    @last_requested_tier.setter
    def last_requested_tier(self, v: Optional[str]):
        self._tl.last_requested_tier = v

    def _record_selection(self, decision: "RoutingDecision") -> None:
        """Record the §L.5 provenance of this decision, and publish last_decision.

        last_decision (FallbackInfo) had a property and setter but no assignment
        anywhere, so every reader saw the not_set sentinel. Set here rather than
        in generate() because it reports the RESOLVER walking the ladder, which
        is known at route() time and so is equally valid while streaming.
        """
        top = decision.resolved[0] if decision.resolved else None
        _via = bool(getattr(decision, "fallback", False))
        _req = (top.requested_tier.value
                if top is not None and top.requested_tier is not None else "")
        _got = (top.tier.value
                if top is not None and top.tier is not None else "")
        self.last_decision = FallbackInfo(
            fallback_occurred=_via,
            from_tier=_req or str(decision.tier or ""),
            from_label=_req or str(decision.tier or ""),
            to_tier=_got or _req or str(decision.tier or ""),
            to_label=decision.model or "",
            reason="tier_fallback" if _via else "primary",
        )

        if decision.resolved:
            top = decision.resolved[0]
            self.last_selection_mode = top.selection_mode
            self.last_requested_tier = (
                top.requested_tier.value if top.requested_tier is not None else None)
        elif decision.tier == TIER_REGISTRY:
            # The user named a model. True with the flag off as well as on,
            # and worth recording either way: the rollout is measured by a
            # shift in the model distribution, and a shift caused by users
            # picking differently has to be separable from one caused by
            # governance.
            self.last_selection_mode = "explicit"
            self.last_requested_tier = None
        else:
            self.last_selection_mode = None
            self.last_requested_tier = None

    @property
    def _last_actual_tier(self) -> str:
        return getattr(self._tl, "_last_actual_tier", "")
    @_last_actual_tier.setter
    def _last_actual_tier(self, v: str):
        self._tl._last_actual_tier = v

    # --------------------------------------------------------
    # LAZY GATEWAY LOADERS
    # --------------------------------------------------------

    def _get_local(self) -> Optional[object]:
        """Return the LiteLLM gateway (in-house Local models)."""
        if self._local is None:
            try:
                from gateway_local_llm import get_local_gateway
                self._local = get_local_gateway()
            except Exception as e:
                logger.warning(f"ModelRouter: Local gateway unavailable → {e}")
        return self._local

    def _get_openai(self) -> Optional[object]:
        # If no model is configured for this provider, treat it as unavailable.
        # Model IDs come entirely from env — an empty value means the operator
        # has not configured OpenAI, so skip it rather than sending an empty
        # model ID to the API (which would return an error).
        if not OPENAI_SIMPLE_MODEL and not OPENAI_CODING_MODEL and not _registry_has_family("openai"):
            return None
        proxy = _llm_proxy_url()
        if self._openai is not None and isinstance(self._openai, _ProxyGateway) != bool(proxy):
            self._openai = None
        if self._openai is None:
            if proxy:
                self._openai = _ProxyGateway("openai")
            else:
                try:
                    from gateway_openai import OpenAIGateway
                    self._openai = OpenAIGateway()
                except Exception as e:
                    logger.warning(f"ModelRouter: OpenAI gateway unavailable → {e}")
        return self._openai

    def _get_claude(self) -> Optional[object]:
        # If no model is configured for this provider, treat it as unavailable.
        if not CLAUDE_PRIMARY_MODEL and not CLAUDE_HAIKU and not _registry_has_family("anthropic"):
            return None
        proxy = _llm_proxy_url()
        if self._claude is not None and isinstance(self._claude, _ProxyGateway) != bool(proxy):
            self._claude = None
        if self._claude is None:
            if proxy:
                self._claude = _ProxyGateway("claude")
            else:
                try:
                    from gateway_claude import ClaudeGateway
                    self._claude = ClaudeGateway()
                except Exception as e:
                    logger.warning(f"ModelRouter: Claude gateway unavailable → {e}")
        return self._claude

    def _get_gemini(self) -> Optional[object]:
        # If no model is configured for this provider, treat it as unavailable.
        if not GEMINI_TEXT_MODEL and not GEMINI_IMAGE_MODEL and not _registry_has_family("gemini"):
            return None
        proxy = _llm_proxy_url()
        if self._gemini is not None and isinstance(self._gemini, _ProxyGateway) != bool(proxy):
            self._gemini = None
        if self._gemini is None:
            if proxy:
                self._gemini = _ProxyGateway("gemini")
            else:
                try:
                    from gateway_gemini import GeminiGateway
                    self._gemini = GeminiGateway()
                except Exception as e:
                    logger.warning(f"ModelRouter: Gemini gateway unavailable → {e}")
        return self._gemini

    # --------------------------------------------------------
    # TIER_REGISTRY — admin-configured provider registry dispatch
    # (extra Anthropic/OpenAI models, any openai_compatible/OpenRouter model).
    # No cross-vendor fallback, unlike every _try_* method above: an
    # explicitly admin-picked model should error, not silently run a
    # different vendor's model.
    # --------------------------------------------------------

    def _resolve_registry_gateway(self, provider_model: Optional[str]):
        """Return (gateway_or_None, family_or_None) for a TIER_REGISTRY model.

        For anthropic/openai, prefers the existing cached singleton
        (self._get_claude()/_get_openai()) but falls back to constructing one
        directly when that returns None — which happens when
        CLAUDE_PRIMARY_MODEL/OPENAI_SIMPLE_MODEL is unset, i.e. a provider
        configured PURELY through the admin screen with no matching .env
        vars at all (gateway_claude.py/gateway_openai.py's __init__ still
        succeeds in that case via resolve_credential_for_family — see the
        LLM provider config design doc's Phase 9 notes — only the env-var
        presence *gate* in _get_claude()/_get_openai() would otherwise block it).
        """
        if not provider_model:
            return None, None
        try:
            from core.llm_provider_registry import get_model as _get_registry_model
            reg = _get_registry_model(provider_model)
        except Exception:
            reg = None
        if not reg:
            return None, None
        family = reg["family"]

        return self._gateway_for_registry_family(family, provider_model), family

    def _gateway_for_registry_family(self, family: str, provider_model: Optional[str]):
        """Gateway for a model identified by its REGISTRY ROW, any family.

        Distinct from _gateway_for_family(), which answers "is this provider
        configured in .env". This one can construct a gateway for a provider
        that has no .env vars at all — the case an admin-only setup always
        produces, since install.sh's LLM Providers flow writes the API key and
        nothing else.

        gemini and ollama were added in Phase 5. Before it, route() intercepted
        those two families and sent them down the TIER_GEMINI / TIER_SIMPLE
        chains, so _try_registry never saw them — which was fine while the only
        caller was the user-explicit path, and is not fine now that a tier
        assignment can name any model of any family. Without them, assigning an
        Ollama model to `medium` would resolve correctly and then fail to
        dispatch.
        """
        if family == "anthropic":
            gw = self._get_claude()
            if gw is None:
                try:
                    from gateway_claude import ClaudeGateway
                    gw = ClaudeGateway()
                except Exception as e:
                    logger.warning(f"ModelRouter: registry Claude gateway unavailable → {e}")
                    gw = None
            return gw

        if family == "openai":
            gw = self._get_openai()
            if gw is None:
                try:
                    from gateway_openai import OpenAIGateway
                    gw = OpenAIGateway()
                except Exception as e:
                    logger.warning(f"ModelRouter: registry OpenAI gateway unavailable → {e}")
                    gw = None
            return gw

        if family == "gemini":
            gw = self._get_gemini()
            if gw is None:
                try:
                    from gateway_gemini import GeminiGateway
                    gw = GeminiGateway()
                except Exception as e:
                    logger.warning(f"ModelRouter: registry Gemini gateway unavailable → {e}")
                    gw = None
            return gw

        if family == "ollama":
            # The in-house gateway serves every local model; the specific id
            # travels as the `model` kwarg, exactly as a "local:<id>" hint does.
            return self._get_local()

        if family == "openai_compatible":
            try:
                from core.llm_provider_registry import get_client_for
                client_info = get_client_for(provider_model)
                if not client_info or not client_info.get("base_url"):
                    return None
                from gateway_generic_openai import get_generic_gateway
                return get_generic_gateway(client_info["base_url"], client_info.get("api_key"))
            except Exception as e:
                logger.warning(f"ModelRouter: registry generic-openai gateway unavailable → {e}")
                return None

        logger.warning("ModelRouter: no gateway for provider family %r", family)
        return None

    @staticmethod
    def _filter_kwargs_for(func, kwargs: dict) -> dict:
        """Drop any kwarg `func` doesn't declare, unless it accepts **kwargs
        (in which case pass everything through unchanged).

        Every built-in gateway's generate() has a DIFFERENT hand-written
        parameter list — Claude's takes no precleared/precleared_findings at
        all (it never does a second compliance pass), OpenAI's and Gemini's
        both do, and the generic openai_compatible gateway accepts **kwargs
        and ignores extras. Blindly forwarding _dispatch's compliance kwargs
        to whichever gateway TIER_REGISTRY resolves to crashed with
        "generate() got an unexpected keyword argument 'precleared'" for
        Claude specifically. Filtering dynamically by introspecting the
        actual callable — rather than a hardcoded "family == anthropic"
        special case — means this stays correct for every current and future
        provider family without needing a matching update here whenever a
        gateway's signature changes.
        """
        try:
            sig = inspect.signature(func)
        except (TypeError, ValueError):
            return kwargs
        params = sig.parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return kwargs
        return {k: v for k, v in kwargs.items() if k in params}

    def _try_registry(self, prompt: str, provider_model: Optional[str] = None, **kwargs) -> tuple[str, bool]:
        gw, family = self._resolve_registry_gateway(provider_model)
        if gw is None:
            return f"Error: no gateway available for registry model {provider_model!r}", False
        self._tl._last_registry_gw = gw   # thread-local — read by _propagate_tokens; avoids cross-request bleed (see class-level _tl doc)
        call_kwargs = self._filter_kwargs_for(gw.generate, kwargs)
        try:
            result = self._collect(gw.generate(prompt, model=provider_model, **call_kwargs))
            self.last_model_label = f"{provider_model}"
            self._last_actual_tier = TIER_REGISTRY
            return result, False
        except Exception as e:
            logger.warning(f"ModelRouter: registry dispatch failed for {provider_model!r} ({family}) → {e}")
            return f"Error: {provider_model} call failed ({e})", False

    def _try_registry_stream(self, prompt: str, provider_model: Optional[str] = None, **kwargs):
        gw, family = self._resolve_registry_gateway(provider_model)
        if gw is None:
            yield f"Error: no gateway available for registry model {provider_model!r}"
            return
        self._tl._last_registry_gw = gw   # thread-local — read by _propagate_tokens; avoids cross-request bleed (see class-level _tl doc)
        call_kwargs = self._filter_kwargs_for(gw.generate, kwargs)
        try:
            yield from gw.generate(prompt, model=provider_model, **call_kwargs)
        except Exception as e:
            logger.warning(f"ModelRouter: registry stream dispatch failed for {provider_model!r} ({family}) → {e}")
            yield f"Error: {provider_model} call failed ({e})"

    # --------------------------------------------------------
    # SIGNAL DETECTION
    # --------------------------------------------------------

    @staticmethod
    def _detect_vision(prompt: str) -> bool:
        return bool(_VISION_RE.search(prompt))

    @staticmethod
    def _classify_complexity(prompt: str) -> str:
        try:
            from models.classifier import classify_query_complexity
            return classify_query_complexity(prompt)
        except Exception as e:
            logger.warning(f"ModelRouter: classifier failed → {e}")
            return TIER_MEDIUM

    # --------------------------------------------------------
    # GOVERNED RESOLUTION  (Phase 5)
    # --------------------------------------------------------

    @staticmethod
    def _attempts_from_resolved(resolved: list) -> list:
        """The resolver's ordered candidates as dispatcher attempts.

        This is the whole point of making the legacy chains data: a tier's
        candidate list and a hand-written fallback chain are the same shape,
        so §M.5's "candidate 1 fails, candidate 2 is tried" costs no new
        control flow. The label is the bare model id — `_estimate_cost` and
        `_resolve_web_search_pricing_key` both parse last_model_label, and a
        bare id is the one form neither has to unwrap.
        """
        def _attempt(rm):
            return _Attempt(
                family=rm.family,
                model=lambda ctx, _m=rm.model_id: _m,
                label=lambda ctx, gw, _m=rm.model_id: _m,
                tier=TIER_GOVERNED,
                forward_kwargs=True,
                gateway=lambda router, _rm=rm: router._gateway_for_registry_family(
                    _rm.family, _rm.model_id),
                # §M.4 — per provider/model, not per family, so one failing
                # model cannot fast-fail every other model of the same vendor.
                breaker_key=f"{rm.provider_slug}:{rm.model_id}",
            )
        return [_attempt(rm) for rm in resolved]

    @staticmethod
    def _apply_acl(governed_tier, candidates: list, acl_filter: AclFilter) -> list:
        """The permitted subset of `candidates`, in the admin's priority order.

        One call with the whole id list, not one per candidate: `acl_filter`
        wraps a database round-trip, and the ACL query is a single UNION ALL
        over both rule tables.

        The result is re-derived by membership rather than taken from the
        filter's return order, because the filter answers "may they use
        these", not "in what order" — and §M.5's priority ordering is the
        administrator's, not the ACL's.
        """
        ids = [rm.model_id for rm in candidates]
        if not ids:
            # Nothing to permit or refuse. resolve_tier_candidates raises
            # rather than returning [], so this is unreachable through
            # _resolve_governed — but "the deployment has nothing assigned"
            # and "the administrator denied you" are the two failures this
            # class exists to keep apart, and conflating them here would
            # report an unconfigured tier as an access denial.
            return []
        try:
            allowed = set(acl_filter(ids))
        except Exception as exc:  # noqa: BLE001
            # Fail OPEN, matching the pre-migration behaviour this replaces
            # (gateway.py's walk wrapped the whole block in "check error
            # (fail-open)"). A governance lookup that is down must not take
            # chat down with it; it is logged loudly instead.
            logger.warning(
                "ModelRouter: ACL filter for tier %r raised %s — allowing all "
                "%d candidate(s) for this request (fail-open).",
                getattr(governed_tier, "value", governed_tier), exc, len(ids))
            return candidates

        kept = [rm for rm in candidates if rm.model_id in allowed]
        if not kept:
            raise ModelsBlockedByPolicy(governed_tier, ids)
        if len(kept) != len(candidates):
            logger.info(
                "ModelRouter: ACL dropped %d of %d candidate(s) for tier %r "
                "(blocked: %s) — serving %s",
                len(candidates) - len(kept), len(candidates),
                getattr(governed_tier, "value", governed_tier),
                ", ".join(i for i in ids if i not in allowed),
                kept[0].model_id,
            )
        return kept

    def _resolve_governed(self, governed_tier, extra: dict, *, legacy_tier: str,
                          complexity: str, is_vision: bool, hint,
                          constraints_kw: dict, channel,
                          acl_filter: Optional[AclFilter] = None):
        """Resolve one tier through the admin's assignments.

        A tier with nothing eligible raises NoEligibleModel (D107): there is no
        env-constant fallback since Phase 8, and a confidential turn must never
        be answered by whatever model happens to be reachable (§M.1/§M.5).

        `acl_filter` (§N.1 step 9) narrows the resolved candidates to the ones
        this user is permitted to use. It runs HERE, on the resolved list,
        rather than in the caller, for three reasons:

          - the ids are the real ones. The Chat Auto path used to ACL-check
            hint_to_model_id(hint) — the pre-registry hint → .env map — which
            on this deployment answered "gpt-5.4" for a request that
            dispatched claude-sonnet-4-6. filter_allowed_models treats an
            absent rule as allowed, so the check passed for a model that was
            never called.
          - "candidate 1 is blocked, try candidate 2" is already what
            _attempts_from_resolved does with the list. Filtering the list is
            therefore the whole of the fallback behaviour that
            CHAT_FALLBACK_CHAIN was hand-rolling in 116 lines of gateway.py.
          - the admin's priority order survives. A caller that picked a
            permitted model itself and pinned it would flatten the ordering
            and report selection_mode='explicit' for a turn the user asked
            Auto for.

        None (the default) means no ACL, which is what every application-tier
        caller passes — §M.4: the department ACL governs the user's own turns,
        not the platform's internal classification and summarisation calls.
        """
        from core.tier_resolver import (
            Constraints, NoEligibleModel, resolve_tier_candidates,
        )
        c = Constraints(**{**constraints_kw, **extra})
        try:
            candidates = resolve_tier_candidates(governed_tier, c, channel=channel)
            if acl_filter is not None:
                candidates = self._apply_acl(governed_tier, candidates, acl_filter)
        except (ModelsBlockedByPolicy, NoEligibleModel):
            raise
        except Exception as exc:  # noqa: BLE001 — reported as the deployment gap it is
            logger.warning("ModelRouter: tier resolution for %r failed: %s",
                           legacy_tier or governed_tier.value, exc)
            raise NoEligibleModel(governed_tier, c, {"resolver": str(exc)}) from exc

        top = candidates[0]
        logger.info(
            "ModelRouter: tier governance — %s → %s (%s, priority %s, %s)",
            governed_tier.value, top.model_id, top.family, top.priority,
            top.selection_mode,
        )
        return RoutingDecision(
            tier=TIER_GOVERNED, model=top.model_id, complexity=complexity,
            is_vision=is_vision, hint=hint, fallback=top.via_fallback,
            provider_model_override=top.model_id,
            resolved=candidates, requested_tier=governed_tier,
        )

    # --------------------------------------------------------
    # ROUTING
    # --------------------------------------------------------

    def route(self, prompt, model_hint: Optional[str] = None,
              data_classification: Optional[str] = None,
              context_tokens: int = 0,
              *, tier: Optional[Tier] = None,
              legacy_hint: Optional[str] = None,
              no_cloud_egress: bool = False,
              distinct_from_family: Optional[str] = None,
              require_role: Optional[str] = None,
              budget_state: Optional[str] = None,
              needs_tools: bool = False,
              needs_streaming: bool = False,
              channel: Optional[str] = None,
              acl_filter: Optional[AclFilter] = None) -> RoutingDecision:
        """Return the RoutingDecision for this prompt.
        prompt: str OR list[dict] (multi-turn messages array).
        tier: OPTIONAL approved application tier (core.tiers.Tier). Keyword-only
            and enum-typed so it can never collide with the legacy `model_hint`
            string vocabulary — see _coerce_tier. Mutually exclusive with
            model_hint. Resolves through the admin's tier assignments
            directly, which is the only way to reach image-output,
            video-generation or intent-classification — they have no legacy hint.
        legacy_hint: OPTIONAL (Phase 6, D15) the hint this call site passed
            BEFORE it was migrated to tier=. No longer consulted for routing
            since Phase 8 — a tier= call resolves or raises. Requires tier=;
            must be a known hint. See _coerce_tier.
        data_classification: optional sensitivity tag (PUBLIC/INTERNAL/
            CONFIDENTIAL/RESTRICTED/PCI_SENSITIVE, per core/rag_acl.py). At or
            above CONFIDENTIAL this becomes the no_cloud_egress constraint.
        no_cloud_egress: OPTIONAL policy assertion from a caller that knows its
            content must stay in the estate whatever the turn is labelled
            (§M.1, and §D.2's memory rows). OR-ed with the classification-
            derived value; neither can switch the other off.
        context_tokens: optional estimated token footprint of the whole turn.
            Becomes min_context_window, filtering the requested tier's
            candidates (§M.2).

        distinct_from_family / budget_state / needs_tools / needs_streaming:
            §M constraints that only the CALLER can know. Plumbed through to
            the resolver and passed by nobody in Phase 5 — the SDLC
            cross-model-review site and the budget governors connect them in
            Phase 6. They exist now so that wiring is a one-line change at the
            call site rather than a signature change here.
            distinct_from_family is connected by §N.1 step 10: the SDLC
            manifest judge asks not to be the family that authored the plan
            it is judging (§M.3b).

        require_role: OPTIONAL (§N.1 step 10, §M.3a) a PREFERENCE for a
            candidate the administrator tagged with this role — in practice
            "review". Added because it was previously reachable only through
            the legacy `solution` hint, via _LEGACY_TO_GOVERNED's `extra`: a
            call that said `tier=Tier.COMPLEX` got `extra={}` and so could
            never express "this is a review gate". A preference and not a
            filter, so a single-model deployment still runs the gate with
            author and reviewer coinciding (see tier_resolver._survivors).

        acl_filter: OPTIONAL (§N.1 step 9) the user's access-control list, as
            a callable taking model ids and returning the permitted subset —
            the shape of filter_allowed_models. Applied to the tier's resolved
            candidates, so a blocked candidate 1 serves from candidate 2 in
            the administrator's priority order and only an entirely blocked
            tier fails. Raises ModelsBlockedByPolicy in that case, which does
            NOT degrade to the legacy chain. Passed by the user-facing chat
            entry points only; §M.4 keeps application tiers unfiltered.

        Raises NoEligibleModel when the requested tier has nothing eligible
        (D107), and ModelsBlockedByPolicy when the user's ACL denies every
        candidate.
        """
        requested_tier = Tier(tier) if tier is not None else None
        model_hint = self._coerce_tier(model_hint, tier, legacy_hint)
        prompt_str = _as_str(prompt)  # routing signals always derived from text

        # §M.1 — two independent ways to require it, one meaning. The
        # classification is the data talking; the keyword is a caller that
        # KNOWS its content must stay in the estate regardless of how the turn
        # happens to be labelled (§D.2's memory rows). Either alone is enough.
        _no_cloud = _privacy_requires_local(data_classification) or bool(no_cloud_egress)

        # §M.2 — the same arithmetic _promote_for_context does, expressed as a
        # filter on the requested tier instead of a switch to a different one.
        # Only from a CALLER-SUPPLIED count here; the prompt-length estimate
        # stays confined to the auto path at step 3c, exactly as today, so
        # governance does not widen where the estimate applies.
        _min_window = None
        if _CONTEXT_SIZE_ROUTING and context_tokens and context_tokens > 0:
            _min_window = int(context_tokens / max(0.1, _CONTEXT_FIT_FRACTION))

        _constraints_kw = {
            "no_cloud_egress": _no_cloud,
            "min_context_window": _min_window,
            "distinct_from_family": distinct_from_family,
            # §M.3a — a preference, resolved in tier_resolver._survivors. When
            # the caller came in on the legacy "solution" hint,
            # _LEGACY_TO_GOVERNED's `extra` carries the same key and WINS
            # (Constraints(**{**constraints_kw, **extra}) below), so the
            # flag-off shape of that hint is unchanged by this parameter.
            "require_role": require_role,
            "budget_state": budget_state,
            "needs_tools": needs_tools,
            "needs_streaming": needs_streaming,
        }

        def _govern(legacy_tier, *, complexity, is_vision, hint,
                    explicit: Optional[Tier] = None, min_window=None):
            """Resolve this tier through the assignments; None for a user's SKU pick."""
            if explicit is not None:
                pair = (explicit, {})
            else:
                pair = _LEGACY_TO_GOVERNED.get(legacy_tier)
                if pair is None:
                    return None            # a user's SKU pick, or an unmigrated tier
            gtier, extra = pair
            kw = dict(_constraints_kw)
            if min_window is not None:
                kw["min_context_window"] = min_window
            return self._resolve_governed(
                gtier, extra, legacy_tier=legacy_tier, complexity=complexity,
                is_vision=is_vision, hint=hint, constraints_kw=kw, channel=channel,
                acl_filter=acl_filter,
            )

        # 0. PRIVACY FLOOR (hard enterprise invariant). The tier is unchanged
        #    and the candidate set narrows to privacy_class == deployment_local
        #    (§M.1); with no local model the resolver fails loudly rather than
        #    walking a fallback ladder, so confidential data never egresses.
        if _no_cloud:
            logger.warning(
                "ModelRouter: PRIVACY FLOOR enforced — data_classification=%s → "
                "no_cloud_egress constraint; the requested tier is unchanged and "
                "only deployment-local candidates are eligible.",
                str(data_classification).strip().upper(),
            )

        # 0b. An explicit `tier=` under governance resolves directly. This is
        #     the only path to image-output, video-generation and
        #     intent-classification: _coerce_tier maps all three onto legacy
        #     hints that mean something else, which is fine while nothing
        #     dispatches on them and wrong the moment something does.
        if requested_tier is not None:
            _d = _govern(None, complexity=requested_tier.value,
                         is_vision=(requested_tier is Tier.IMAGE_INPUT),
                         hint=None, explicit=requested_tier)
            if _d is not None:
                return _d

        # 1. Caller hint
        if model_hint:
            key = model_hint.lower()
            # "local:<model-id>" pins a SPECIFIC in-house model on the simple/local
            # tier (e.g. DOC_INTENT_MODEL=local:gemma or local:kimi-k2.7). The id
            # after the colon is forwarded to the local gateway's generate(model=…).
            if key.startswith("local:"):
                _local_model = model_hint.split(":", 1)[1].strip()
                if _local_model:
                    logger.info(f"ModelRouter: hint={model_hint!r} → tier=simple model={_local_model!r}")
                    return RoutingDecision(
                        tier=TIER_SIMPLE, model=f"Local ({_local_model})",
                        complexity=TIER_SIMPLE, is_vision=False,
                        hint=model_hint, fallback=False,
                        provider_model_override=_local_model,
                    )
            # 1a. Admin-configured provider registry (core.llm_provider_registry)
            # — checked BEFORE _HINT_MAP below, and takes priority on a match.
            # Some providers' own real model ids happen to collide with
            # pre-existing hardcoded alias keys in _HINT_MAP — e.g. Anthropic's
            # actual "claude-sonnet-5"/"claude-opus-5"/"claude-opus-4-8" model
            # ids are ALSO alias keys mapped to TIER_SONNET_5/TIER_OPUS_5/
            # TIER_OPUS_48. If _HINT_MAP were checked first, picking that exact
            # model from the admin-config-driven chat dropdown would get
            # silently reinterpreted as a generic tier selection instead of
            # "run this exact model" — and since newly-synced registry models
            # don't have tier_tags set, the tier's fallback resolution could
            # dispatch a COMPLETELY DIFFERENT model than the one requested
            # (confirmed live: selecting "claude-sonnet-5" was dispatching
            # "claude-opus-5" instead). An exact registry model_id match is a
            # strictly more specific signal of intent than a coincidental
            # alias collision, so it must win. Gemini and Ollama/local reuse
            # the existing TIER_GEMINI/TIER_SIMPLE dispatch (unchanged from
            # before); everything else dispatches via the TIER_REGISTRY branch
            # in _dispatch()/_dispatch_stream()/async_stream(). No cross-vendor
            # fallback for any of these: an explicitly admin-picked model
            # should surface an error if it fails, not silently run a
            # different vendor's model.
            try:
                from core.llm_provider_registry import get_model as _get_registry_model
                _reg_model = _get_registry_model(key)
            except Exception:
                _reg_model = None
            if _reg_model and _reg_model["family"] == "gemini":
                logger.info(f"ModelRouter: hint={model_hint!r} → registry Gemini model")
                return RoutingDecision(
                    tier=TIER_GEMINI, model=_gemini_model_label(_reg_model["model_id"]),
                    complexity=TIER_GEMINI, is_vision=False,
                    hint=model_hint, fallback=False,
                    provider_model_override=_reg_model["model_id"],
                )
            if _reg_model and _reg_model["family"] == "ollama":
                logger.info(f"ModelRouter: hint={model_hint!r} → registry Ollama model")
                return RoutingDecision(
                    tier=TIER_SIMPLE, model=f"Local ({_reg_model['model_id']})",
                    complexity=TIER_SIMPLE, is_vision=False,
                    hint=model_hint, fallback=False,
                    provider_model_override=_reg_model["model_id"],
                )
            if _reg_model:
                logger.info(f"ModelRouter: hint={model_hint!r} → registry {_reg_model['family']} model")
                return RoutingDecision(
                    tier=TIER_REGISTRY, model=f"{_reg_model['display_name']} ({_reg_model['model_id']})",
                    complexity=TIER_REGISTRY, is_vision=False,
                    hint=model_hint, fallback=False,
                    provider_model_override=_reg_model["model_id"],
                )

            # 1b. Hardcoded hint/tier alias map — reached only when the hint
            # did NOT exactly match a registry model_id above (e.g. a generic
            # keyword like "claude"/"gpt"/"medium", not a specific model id).
            if key in _HINT_MAP:
                tier = _HINT_MAP[key]
                logger.info(f"ModelRouter: hint={model_hint!r} → tier={tier}")
                # When the hint resolves to a specific Gemini model ID, forward
                # it so the dispatcher hits THAT model instead of the gateway
                # default. Non-Gemini hints stay None and dispatch unchanged.
                _gemini_override = _GEMINI_SPECIFIC_HINTS.get(key)
                # Use the model-specific label when the user picked a specific
                # Gemini ID — otherwise the chat footer/meta would always show
                # the tier's default model (e.g. gemini-3.5-flash) regardless
                # of which Gemini model actually ran.
                _label = _gemini_model_label(_gemini_override) if _gemini_override else _tier_label(tier)
                # Governance: a hint that names a CAPABILITY resolves through
                # the assignments; one that names a SKU does not — see
                # _LEGACY_TO_GOVERNED for which is which and why. A specific
                # Gemini id is always the latter, so it short-circuits.
                if _gemini_override is None:
                    _d = _govern(tier, complexity=tier,
                                 is_vision=(tier == TIER_VISION), hint=model_hint)
                    if _d is not None:
                        return _d
                return RoutingDecision(
                    tier=tier, model=_label,
                    complexity=tier, is_vision=(tier == TIER_VISION),  # TIER_GEMINI is not vision
                    hint=model_hint, fallback=False,
                    provider_model_override=_gemini_override,
                )

        # 2. Vision detection — only fires when:
        #    a) the caller did NOT pin a non-vision model hint (e.g. kimi, local, glm), AND
        #    b) the prompt actually contains an image content block (not just vision-adjacent
        #       words like "render" or "canvas" that appear naturally in code conversations).
        _hint_lower = (model_hint or "").lower()
        _hint_is_non_vision = any(_hint_lower.startswith(p) or p in _hint_lower
                                  for p in _NON_VISION_HINT_PREFIXES)
        if not _hint_is_non_vision and _prompt_has_image(prompt) and self._detect_vision(prompt_str):
            logger.info("ModelRouter: vision keywords + image attachment → Gemini")
            _d = _govern(TIER_VISION, complexity="N/A", is_vision=True, hint=None)
            if _d is not None:
                return _d
            return RoutingDecision(
                tier=TIER_VISION, model=_tier_label(TIER_VISION),
                complexity="N/A", is_vision=True, hint=None, fallback=False,
            )

        # 3. Complexity classification — always use LLM-backed classifier.
        #    classify_with_confidence_llm() runs regex first; if regex confidence
        #    is below 0.7 it delegates to Claude Haiku for highest accuracy.
        #    No mechanical tier-bumping — the LLM result is the authoritative label.
        try:
            from models.classifier import classify_with_confidence_llm
            complexity, confidence = classify_with_confidence_llm(prompt_str)
        except Exception as _e:
            logger.warning(f"ModelRouter: classify_with_confidence_llm failed → {_e}")
            complexity, confidence = self._classify_complexity(prompt_str), 0.75

        tier = complexity
        logger.info(
            f"ModelRouter: classified complexity={complexity} confidence={confidence:.2f}"
            + (" (LLM)" if confidence >= 0.9 else " (regex)")
        )

        # 3b. Code-domain guard — upgrade code queries from simple to medium
        if tier == TIER_SIMPLE:
            try:
                from models.classifier import detect_query_domain
                if detect_query_domain(prompt_str) == "code":
                    logger.info("ModelRouter: code domain with simple complexity → upgrade to TIER_MEDIUM")
                    tier = TIER_MEDIUM
            except Exception as _e:
                logger.warning(f"ModelRouter: domain detection failed → {_e}")

        # 3c. CONTEXT-SIZE ROUTING (frontier pattern #5) — promote to a larger-
        #     window tier when the turn's token footprint won't fit. Runs last so
        #     it can lift any complexity-derived tier, but only for auto turns
        #     (explicit hints returned earlier) and never for privacy-pinned
        #     turns (returned earlier). Fail-safe: unchanged on any error.
        # When the caller did not pass an explicit count, estimate from the
        # prompt text (~4 chars/token) so context-size routing works even on the
        # existing call sites that only pass prompt+model_hint.
        if (not context_tokens or context_tokens <= 0) and _CONTEXT_SIZE_ROUTING:
            try:
                context_tokens = int(len(prompt_str) / 4)
            except Exception:  # noqa: BLE001
                context_tokens = 0

        # The tier does NOT change. The token count becomes min_context_window
        # and filters the requested tier's own candidates (§M.2): the window is
        # a property of the model, held in capabilities.context_window.
        _d = _govern(
            tier, complexity=complexity, is_vision=False, hint=None,
            min_window=(int(context_tokens / max(0.1, _CONTEXT_FIT_FRACTION))
                        if (_CONTEXT_SIZE_ROUTING and context_tokens > 0) else None),
        )
        if _d is not None:
            return _d

        logger.info(f"ModelRouter: final tier={tier} (complexity={complexity} confidence={confidence:.2f})")
        # Fix 2: for TIER_SIMPLE, pin the catalog pick NOW (once per request) so
        # route(), _try_local_simple_stream(), and generate() all see the same model
        # ID — even if the catalog refreshes between these three call sites.
        _simple_override: Optional[str] = None
        if tier == TIER_SIMPLE:
            try:
                from gateway_local_llm import _catalog as _lcat
                _simple_override = _lcat.pick("simple")
            except Exception:
                pass
        return RoutingDecision(
            tier=tier, model=_tier_label(tier),
            complexity=complexity, is_vision=False, hint=None, fallback=False,
            provider_model_override=_simple_override,
        )

    # --------------------------------------------------------
    # TOKEN PROPAGATION
    # --------------------------------------------------------

    def _propagate_tokens(self, tier: str) -> None:
        _gw_map = {
            TIER_SIMPLE:    self._local,
            TIER_MINI:      self._openai,
            TIER_LOCAL_MINI: self._openai,
            TIER_MEDIUM:    self._openai,
            TIER_DEEP:      self._openai,
            TIER_COMPLEX:   self._claude,
            TIER_HAIKU:     self._claude,
            TIER_VISION:    self._gemini,
            TIER_GEMINI:    self._gemini,
            TIER_SOLUTION:  self._claude,
            TIER_OPUS_48:   self._claude,
            TIER_OPUS_5:    self._claude,
            TIER_SONNET_5:  self._claude,
            TIER_TERA:      self._openai,
            TIER_LUNA:      self._openai,
            # TIER_REGISTRY has no single fixed gateway attribute (anthropic/
            # openai reuse the singletons above when available, but
            # openai_compatible constructs a fresh instance per call — see
            # _resolve_registry_gateway) — _try_registry/_try_registry_stream
            # stash whichever instance actually served the request here so
            # token/cost tracking isn't silently zero for these models.
            TIER_REGISTRY:  getattr(self._tl, "_last_registry_gw", None),
            # Same reason as TIER_REGISTRY: a governed hop's gateway depends on
            # which model the resolver picked, so the dispatcher stashes it.
            TIER_GOVERNED:  getattr(self._tl, "_last_registry_gw", None),
        }
        gw = _gw_map.get(tier)
        self.last_input_tokens          = getattr(gw, "_last_input_tokens",          0) or 0
        self.last_output_tokens         = getattr(gw, "_last_output_tokens",         0) or 0
        self.last_cache_read_tokens     = getattr(gw, "_last_cache_read_tokens",     0) or 0
        self.last_cache_creation_tokens = getattr(gw, "_last_cache_creation_tokens", 0) or 0

    # --------------------------------------------------------
    # DISPATCH  (blocking — collects full response)
    # --------------------------------------------------------

    def _dispatch(self, tier: str, prompt: str, provider_model: Optional[str] = None,
                  candidates: Optional[list] = None, **kwargs) -> tuple[str, bool]:
        # kwargs carries precleared / precleared_findings when the upstream
        # caller has already run compliance_engine.validate_input().
        # privacy_local_only is consumed ONLY by the local path (a privacy-pinned
        # turn is always TIER_SIMPLE); pop it so it never leaks into cloud
        # gateways' generate() signatures.
        _privacy_local_only = kwargs.pop("privacy_local_only", False)
        if tier == TIER_GOVERNED:
            # §M.5's within-tier fallback: the resolver handed over every
            # eligible candidate in the admin's priority order, so a candidate
            # whose call fails right now yields to the next one. No cross-tier
            # substitution happens here — the ladder was already walked, once,
            # inside resolve_tier_candidates().
            return self._dispatch_by_family(
                self._attempts_from_resolved(candidates or []), prompt, **kwargs)
        if tier == TIER_SIMPLE:
            return self._try_local_simple(prompt, local_model=provider_model,
                                          privacy_local_only=_privacy_local_only, **kwargs)
        if tier == TIER_MINI:
            return self._try_openai_mini(prompt, **kwargs)
        if tier == TIER_LOCAL_MINI:
            # local_mini routes to the in-house GPU server (LOCAL_LLM_BASE_URL)
            # via the same local gateway used by TIER_SIMPLE. OPENAI_OSS_MODEL
            # carries the specific model ID to request (e.g. kimi-k2.7-code).
            return self._try_local_simple(prompt, local_model=OPENAI_OSS_MODEL or None, **kwargs)
        if tier == TIER_MEDIUM:
            return self._try_openai_coding(prompt, **kwargs)
        if tier == TIER_DEEP:
            return self._try_openai_deep(prompt, **kwargs)
        if tier == TIER_COMPLEX:
            return self._try_claude_sonnet(prompt, **kwargs)
        if tier == TIER_HAIKU:
            return self._try_claude_haiku(prompt, **kwargs)
        if tier in (TIER_VISION, TIER_GEMINI):
            return self._try_gemini(prompt, model=provider_model, **kwargs)
        if tier == TIER_SOLUTION:
            return self._try_claude_solution(prompt, **kwargs)
        if tier == TIER_OPUS_48:
            return self._try_claude_opus48(prompt, **kwargs)
        if tier == TIER_OPUS_5:
            return self._try_claude_opus5(prompt, **kwargs)
        if tier == TIER_SONNET_5:
            return self._try_claude_sonnet5(prompt, **kwargs)
        if tier == TIER_TERA:
            return self._try_openai_tera(prompt, **kwargs)
        if tier == TIER_LUNA:
            return self._try_openai_luna(prompt, **kwargs)
        if tier == TIER_REGISTRY:
            return self._try_registry(prompt, provider_model=provider_model, **kwargs)
        # Fail SAFE rather than returning an error string as the model's answer.
        # An unrecognised tier reaching dispatch is a routing gap (e.g. a local
        # model id that never got a tier). Rather than surfacing "Error: unknown
        # routing tier" verbatim to the user, fall back to the local/simple tier
        # (forwarding the requested model as the local override) and log loudly so
        # the real gap is captured. Verified: local ids like glm-5.2-fp8 route
        # correctly through _try_local_simple.
        logger.warning(
            "ModelRouter: UNKNOWN TIER %r reached _dispatch (model=%r) — falling "
            "back to TIER_SIMPLE/local so the run does not fail with an error "
            "string. Add this tier/model to _HINT_MAP if this recurs.",
            tier, provider_model,
        )
        return self._try_local_simple(prompt, local_model=provider_model, **kwargs)

    # --------------------------------------------------------
    # FAMILY DISPATCH  (Phase 5 — the one control flow)
    # --------------------------------------------------------

    def _gateway_for_family(self, family: str):
        """Legacy-path gateway lookup: the four cached provider singletons.

        Distinct from _resolve_registry_gateway(), which resolves a gateway
        from a REGISTRY ROW and can construct one for a provider that has no
        .env vars at all. This one answers "is the openai/claude/gemini/local
        provider configured", which is the question the legacy chains ask.
        """
        return {
            "local":  self._get_local,
            "openai": self._get_openai,
            "claude": self._get_claude,
            "gemini": self._get_gemini,
        }[family]()

    @staticmethod
    def _attempt_kwargs(a: "_Attempt", ctx: dict, kwargs: dict) -> dict:
        call_kw = dict(a.extra or {})
        if a.model is not None:
            resolved = a.model(ctx)
            # None means "send no model at all" and let the gateway use its
            # own default — which several hops genuinely rely on.
            if resolved is not None:
                call_kw["model"] = resolved
        if a.forward_kwargs:
            call_kw.update(kwargs)
        return call_kw

    def _call_kwargs(self, a: "_Attempt", ctx: dict, kwargs: dict, gw) -> dict:
        """The legacy chains know their gateways; the governed path does not."""
        if a.gateway is None:
            return self._attempt_kwargs(a, ctx, kwargs)
        return self._filtered_attempt_kwargs(a, ctx, kwargs, gw)

    def _filtered_attempt_kwargs(self, a: "_Attempt", ctx: dict, kwargs: dict, gw) -> dict:
        """Attempt kwargs, minus anything this particular gateway cannot take.

        Only the governed path needs this. The legacy chains were written
        against four known gateways and hand-pick which hops receive the
        compliance kwargs; a resolved candidate can be served by any of five
        families, including a generic openai_compatible instance, so the
        filtering has to be dynamic. Same reasoning as _filter_kwargs_for,
        which _try_registry has used for the user-explicit path since Phase 2.
        """
        call_kw = self._attempt_kwargs(a, ctx, kwargs)
        model = call_kw.pop("model", None)
        filtered = self._filter_kwargs_for(gw.generate, call_kw)
        if model is not None:
            filtered["model"] = model
        return filtered

    @staticmethod
    def _attempt_breaker(a: "_Attempt"):
        if a.breaker_key:
            return get_breaker(a.breaker_key)
        return _breaker_for(a.family)

    def _usable_gateway(self, a: "_Attempt"):
        """The gateway for this hop, or None when the hop must be skipped."""
        gw = a.gateway(self) if a.gateway is not None else self._gateway_for_family(a.family)
        if gw is None:
            return None
        if a.family == "local" and not getattr(gw, "available", False):
            return None
        if self._attempt_breaker(a).is_open:
            return None
        return gw

    def _dispatch_by_family(self, attempts, prompt, ctx=None, *,
                            exhausted_fallback: bool = False,
                            exhausted_text: Optional[str] = None,
                            **kwargs) -> tuple[str, bool]:
        """Walk an ordered attempt list, blocking. First usable hop wins.

        Serves both the legacy chains in _LEGACY_CHAIN and the governed path's
        candidate list from tier_resolver — same shape, different source.
        """
        ctx = ctx or {}
        for i, a in enumerate(attempts):
            gw = self._usable_gateway(a)
            if gw is None:
                continue
            if a.gateway is not None:
                # Governed hops have no fixed gateway attribute on the router
                # (openai_compatible builds a fresh instance per call), so the
                # instance that actually served is stashed thread-locally for
                # _propagate_tokens — same mechanism _try_registry has used
                # since Phase 2.
                self._tl._last_registry_gw = gw
            try:
                result = self._collect(self._attempt_breaker(a).call(
                    gw.generate, prompt, **self._call_kwargs(a, ctx, kwargs, gw)))
            except Exception as e:
                logger.warning("ModelRouter: %s hop %d raised → %s", a.family, i, e)
                continue
            if a.check_error and ((a.empty_is_error and not result)
                                  or (result or "").startswith("Error")):
                logger.warning("ModelRouter: %s hop %d returned an error → next hop",
                               a.family, i)
                continue
            if a.label is not None:
                self.last_model_label = a.label(ctx, gw)
            if a.tier is not None:
                self._last_actual_tier = a.tier
            return result, (i > 0) if a.fallback is None else a.fallback
        return (exhausted_text or "Error: no gateway available"), exhausted_fallback

    def _dispatch_by_family_stream(self, attempts, prompt, ctx=None, *,
                                   walk_chain: bool = False,
                                   precleared: bool = False,
                                   precleared_findings: Optional[list] = None):
        """Streaming twin. Three deliberate differences from the blocking form:

        the label is set BEFORE the call (there is no result to judge first),
        the circuit breaker is only consulted and never wrapped around the
        call, and a leading-"Error" token is streamed to the client rather
        than advancing to the next hop. Local hops are the exception on that
        last point — they count tokens and fall through when none arrived,
        which is what `empty_is_error` selects.

        _last_actual_tier is deliberately NOT set here: no streaming method
        ever set it, and starting to would change what _propagate_tokens reads
        for every streaming caller.
        """
        ctx = ctx or {}
        kwargs = {"precleared": precleared, "precleared_findings": precleared_findings}
        for i, a in enumerate(attempts):
            gw = self._usable_gateway(a)
            if gw is None:
                continue
            if a.gateway is not None:
                self._tl._last_registry_gw = gw
            call_kw = self._call_kwargs(a, ctx, kwargs, gw)
            try:
                if a.empty_is_error:
                    token_yielded = False
                    for tok in gw.generate(prompt, **call_kw):
                        if tok and not str(tok).startswith("Error"):
                            token_yielded = True
                            yield tok
                    if token_yielded:
                        if a.label is not None:
                            self.last_model_label = a.label(ctx, gw)
                        return
                    logger.info("ModelRouter stream: %s hop %d empty/error → next hop",
                                a.family, i)
                    continue
                if a.label is not None:
                    self.last_model_label = a.label(ctx, gw)
                yield from gw.generate(prompt, **call_kw)
                if a.capture_thinking:
                    try:
                        self.last_thinking_text = getattr(gw, "_last_thinking_text", "") or ""
                    except Exception:  # noqa: BLE001 — thinking text is never load-bearing
                        pass
                return
            except Exception as e:
                logger.warning("ModelRouter stream: %s hop %d failed → %s", a.family, i, e)
                continue
        if walk_chain:
            # CHAT_FALLBACK_CHAIN, env-configured and empty by default. Only
            # TIER_MINI's streaming path uses it; its blocking twin falls back
            # to Claude Sonnet instead. That divergence predates Phase 5.
            _yielded = yield from self._walk_fallback_chain_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
            if _yielded:
                return
        yield "Error: no gateway available"

    def _try_local_simple(self, prompt: str, local_model: Optional[str] = None,
                          privacy_local_only: bool = False,
                          **kwargs) -> tuple[str, bool]:
        """Local first, then GPT-5-mini, then Claude Sonnet.

        privacy_local_only truncates the chain to its first hop: a
        CONFIDENTIAL+ turn that the local model cannot serve must FAIL rather
        than reach a cloud provider. That is the hard enterprise invariant, so
        it is enforced by never offering the cloud hops to the dispatcher at
        all, rather than by a flag the dispatcher has to remember to honour.
        """
        chain = _LEGACY_CHAIN[TIER_SIMPLE]["sync"]
        ctx = {"local_model": local_model}
        if privacy_local_only:
            out, fb = self._dispatch_by_family(
                chain[:1], prompt, ctx,
                exhausted_text=_PRIVACY_FAIL_CLOSED_TEXT, **kwargs)
            if out is _PRIVACY_FAIL_CLOSED_TEXT:
                logger.error(
                    "ModelRouter: PRIVACY FLOOR — local model unavailable for "
                    "restricted data; FAILING CLOSED (cloud fallback suppressed)."
                )
                self.last_model_label = _tier_label(TIER_SIMPLE)
                self._last_actual_tier = TIER_SIMPLE
            return out, fb
        return self._dispatch_by_family(chain, prompt, ctx, **kwargs)

    def _try_openai_mini(self, prompt: str, **kwargs) -> tuple[str, bool]:
        return self._dispatch_by_family(_LEGACY_CHAIN[TIER_MINI]["sync"], prompt, **kwargs)

    def _try_openai_oss(self, prompt: str, **kwargs) -> tuple[str, bool]:
        """In-house GPT-OSS-120B over the OpenAI-compatible endpoint, falling
        back to GPT-5-mini so intent classification still answers during a
        local outage. Not reached from _dispatch, which sends TIER_LOCAL_MINI
        to the local gateway instead; kept for direct callers."""
        return self._dispatch_by_family(_LEGACY_CHAIN["_oss"]["sync"], prompt, **kwargs)

    def _try_openai_coding(self, prompt: str, **kwargs) -> tuple[str, bool]:
        return self._dispatch_by_family(_LEGACY_CHAIN[TIER_MEDIUM]["sync"], prompt, **kwargs)

    def _try_openai_deep(self, prompt: str, **kwargs) -> tuple[str, bool]:
        return self._dispatch_by_family(_LEGACY_CHAIN[TIER_DEEP]["sync"], prompt, **kwargs)

    def _try_openai_tera(self, prompt: str, **kwargs) -> tuple[str, bool]:
        """GPT-5.6 Tera (high-capacity variant). Falls back to Claude Sonnet."""
        return self._dispatch_by_family(_LEGACY_CHAIN[TIER_TERA]["sync"], prompt, **kwargs)

    def _try_openai_luna(self, prompt: str, **kwargs) -> tuple[str, bool]:
        """GPT-5.6 Luna (efficient variant). Falls back to Claude Sonnet."""
        return self._dispatch_by_family(_LEGACY_CHAIN[TIER_LUNA]["sync"], prompt, **kwargs)

    def _try_claude_sonnet(self, prompt: str, **kwargs) -> tuple[str, bool]:
        return self._dispatch_by_family(_LEGACY_CHAIN[TIER_COMPLEX]["sync"], prompt, **kwargs)

    def _try_claude_haiku(self, prompt: str, **kwargs) -> tuple[str, bool]:
        return self._dispatch_by_family(_LEGACY_CHAIN[TIER_HAIKU]["sync"], prompt, **kwargs)

    def _try_claude_solution(self, prompt: str, **kwargs) -> tuple[str, bool]:
        """Opus if ENABLE_OPUS=true, otherwise Sonnet, then the Sonnet chain.

        was_fallback is True for every outcome except the Opus hop itself —
        including total failure. That is what the pre-Phase-5 method did (it
        overwrote the delegated flag unconditionally) and callers may be
        reading it, so exhausted_fallback carries it rather than correcting it.
        """
        return self._dispatch_by_family(
            _LEGACY_CHAIN[TIER_SOLUTION]["sync"], prompt,
            exhausted_fallback=_LEGACY_CHAIN[TIER_SOLUTION]["exhausted_fallback"],
            **kwargs)

    def _try_claude_opus48(self, prompt: str, **kwargs) -> tuple[str, bool]:
        """Explicit Claude Opus 4.8 selection (CLI/IDE only). Falls back to Sonnet."""
        return self._dispatch_by_family(_LEGACY_CHAIN[TIER_OPUS_48]["sync"], prompt, **kwargs)

    def _try_claude_opus5(self, prompt: str, **kwargs) -> tuple[str, bool]:
        """Explicit Claude Opus 5 selection (CLI/IDE opt-in). Falls back to Sonnet."""
        return self._dispatch_by_family(_LEGACY_CHAIN[TIER_OPUS_5]["sync"], prompt, **kwargs)

    def _try_claude_sonnet5(self, prompt: str, **kwargs) -> tuple[str, bool]:
        """Explicit Claude Sonnet 5 selection (all channels). Falls back to Sonnet 4.6."""
        return self._dispatch_by_family(_LEGACY_CHAIN[TIER_SONNET_5]["sync"], prompt, **kwargs)

    def _try_gemini(self, prompt: str, model: Optional[str] = None, **kwargs) -> tuple[str, bool]:
        return self._dispatch_by_family(
            _LEGACY_CHAIN[TIER_VISION]["sync"], prompt, {"provider_model": model}, **kwargs)

    def _dispatch_stream(
            self,
            tier: str,
            prompt: str,
            local_model: str = None,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
            provider_model: Optional[str] = None,
            candidates: Optional[list] = None,
    ):
        # precleared / precleared_findings are only meaningful for providers
        # that run a second-pass compliance gate inside their generate()
        # (OpenAI + Gemini, direct or via LLM proxy). Claude and Local LLM do
        # not re-validate, so they ignore the flag.
        if tier == TIER_GOVERNED:
            yield from self._dispatch_by_family_stream(
                self._attempts_from_resolved(candidates or []), prompt,
                precleared=precleared, precleared_findings=precleared_findings,
            )
            return
        if tier == TIER_SIMPLE:
            # Fix: honor an explicit local model override on the STREAMING path.
            # Previously provider_model (from a "local:<id>" hint) was dropped
            # here — only local_model= was forwarded — so forcing kimi-k2.7/
            # glm-5.2 on chat streaming silently fell back to the tier default.
            yield from self._try_local_simple_stream(
                prompt, local_model=(local_model or provider_model),
                precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_MINI:
            yield from self._try_openai_mini_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_LOCAL_MINI:
            # local_mini routes to the in-house GPU server (LOCAL_LLM_BASE_URL)
            # via the same local gateway used by TIER_SIMPLE. OPENAI_OSS_MODEL
            # carries the specific model ID to request (e.g. kimi-k2.7-code).
            yield from self._try_local_simple_stream(
                prompt,
                local_model=OPENAI_OSS_MODEL or None,
                precleared=precleared,
                precleared_findings=precleared_findings,
            )
        elif tier == TIER_MEDIUM:
            yield from self._try_openai_coding_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_DEEP:
            yield from self._try_openai_deep_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_COMPLEX:
            # Sonnet fallback can land on OpenAI — forward flag for that case.
            yield from self._try_claude_sonnet_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_HAIKU:
            yield from self._try_claude_haiku_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier in (TIER_VISION, TIER_GEMINI):
            yield from self._try_gemini_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
                model=provider_model,
            )
        elif tier == TIER_SOLUTION:
            yield from self._try_claude_solution_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_OPUS_48:
            yield from self._try_claude_opus48_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_OPUS_5:
            yield from self._try_claude_opus5_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_SONNET_5:
            yield from self._try_claude_sonnet5_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_TERA:
            yield from self._try_openai_tera_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_LUNA:
            yield from self._try_openai_luna_stream(
                prompt, precleared=precleared, precleared_findings=precleared_findings,
            )
        elif tier == TIER_REGISTRY:
            yield from self._try_registry_stream(
                prompt, provider_model=provider_model,
                precleared=precleared, precleared_findings=precleared_findings,
            )
        else:
            # Fail SAFE (see _dispatch): an unknown tier falls back to the local/
            # simple stream forwarding the requested model, rather than yielding
            # "Error: unknown routing tier" as the streamed answer.
            logger.warning(
                "ModelRouter: UNKNOWN TIER %r reached _dispatch_stream (model=%r) "
                "— falling back to TIER_SIMPLE/local. Add this tier/model to "
                "_HINT_MAP if this recurs.",
                tier, provider_model,
            )
            yield from self._try_local_simple_stream(
                prompt, local_model=(local_model or provider_model),
                precleared=precleared, precleared_findings=precleared_findings,
            )

    def _try_local_simple_stream(
            self,
            prompt: str,
            local_model: str = None,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        # Emitted at WARNING so it survives log-level filters and appears in
        # per-request exports even when INFO is suppressed. Cross-check: if
        # [LLM DISPATCH]/[LOCAL USAGE] are absent from an export but this line
        # IS present, the request was served from the Redis/semantic cache
        # before reaching generate() — check bypass metrics
        # (ainxt:bypass:{date}:redis / :semantic) to confirm.
        from core.logger import get_request_id as _gri
        logger.warning(
            "[LOCAL STREAM ENTRY] request_id=%s local_model=%r cb_open=%s",
            _gri() or "n/a", local_model, _CB_LOCAL.is_open,
        )
        yield from self._dispatch_by_family_stream(
            _LEGACY_CHAIN[TIER_SIMPLE]["stream"], prompt, {"local_model": local_model},
            precleared=precleared, precleared_findings=precleared_findings)

    def _try_openai_mini_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        """GPT-5-mini, then CHAT_FALLBACK_CHAIN — NOT the blocking twin's Claude hop."""
        yield from self._dispatch_by_family_stream(
            _LEGACY_CHAIN[TIER_MINI]["stream"], prompt,
            walk_chain=True, precleared=precleared,
            precleared_findings=precleared_findings)

    def _walk_fallback_chain_stream(
            self, prompt: str, *, precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        """Walk CHAT_FALLBACK_CHAIN, yielding from the first reachable hop.

        Returns True (via StopIteration value) if any hop produced tokens, else
        False so the caller can emit the terminal sentinel. Each hop respects its
        circuit breaker; both local hops share _CB_LOCAL (skipped once open).
        Never raises — a failing hop is logged and the walk continues.
        """
        for _hop in CHAT_FALLBACK_CHAIN:
            try:
                if _hop == "haiku":
                    if _CB_CLAUDE.is_open:
                        continue
                    self.last_model_label = f"{CLAUDE_HAIKU_DISPLAY} ({_resolve_tier_model(CLAUDE_HAIKU, 'anthropic', 'haiku')}) [fallback]"
                    _got = False
                    for _tok in self._try_claude_haiku_stream(
                            prompt, precleared=precleared,
                            precleared_findings=precleared_findings):
                        if isinstance(_tok, str) and _tok.startswith("Error:"):
                            break
                        _got = True
                        yield _tok
                    if _got:
                        return True
                elif _hop.startswith("local:"):
                    if _CB_LOCAL.is_open:
                        continue  # local breaker open → skip all local hops
                    _lid = _hop.split(":", 1)[1].strip()
                    _got = False
                    for _tok in self._try_local_simple_stream(
                            prompt, local_model=_lid,
                            precleared=precleared,
                            precleared_findings=precleared_findings):
                        if isinstance(_tok, str) and _tok.startswith("Error:"):
                            break
                        _got = True
                        yield _tok
                    if _got:
                        return True
                else:
                    logger.warning(f"ModelRouter: unknown fallback hop {_hop!r} — skipping")
            except Exception as e:  # noqa: BLE001 — a bad hop must not break the walk
                logger.warning(f"ModelRouter: fallback hop {_hop!r} failed → {e}")
        return False

    def _try_openai_oss_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        """In-house GPT-OSS over the OpenAI-compat endpoint, then GPT-5-mini."""
        yield from self._dispatch_by_family_stream(
            _LEGACY_CHAIN["_oss"]["stream"], prompt,
            precleared=precleared,
            precleared_findings=precleared_findings)

    def _try_openai_coding_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        yield from self._dispatch_by_family_stream(
            _LEGACY_CHAIN[TIER_MEDIUM]["stream"], prompt,
            precleared=precleared,
            precleared_findings=precleared_findings)

    def _try_openai_deep_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        yield from self._dispatch_by_family_stream(
            _LEGACY_CHAIN[TIER_DEEP]["stream"], prompt,
            precleared=precleared,
            precleared_findings=precleared_findings)

    def _try_openai_tera_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        """GPT-5.6 Tera. Falls back to Claude Sonnet."""
        yield from self._dispatch_by_family_stream(
            _LEGACY_CHAIN[TIER_TERA]["stream"], prompt,
            precleared=precleared,
            precleared_findings=precleared_findings)

    def _try_openai_luna_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        """GPT-5.6 Luna. Falls back to Claude Sonnet."""
        yield from self._dispatch_by_family_stream(
            _LEGACY_CHAIN[TIER_LUNA]["stream"], prompt,
            precleared=precleared,
            precleared_findings=precleared_findings)

    def _try_claude_sonnet_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        yield from self._dispatch_by_family_stream(
            _LEGACY_CHAIN[TIER_COMPLEX]["stream"], prompt,
            precleared=precleared,
            precleared_findings=precleared_findings)

    def _try_claude_haiku_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        yield from self._dispatch_by_family_stream(
            _LEGACY_CHAIN[TIER_HAIKU]["stream"], prompt,
            precleared=precleared,
            precleared_findings=precleared_findings)

    def _try_claude_solution_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        """Opus if ENABLE_OPUS=true, else Sonnet, then the Sonnet chain."""
        yield from self._dispatch_by_family_stream(
            _LEGACY_CHAIN[TIER_SOLUTION]["stream"], prompt,
            precleared=precleared,
            precleared_findings=precleared_findings)

    def _try_claude_opus5_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        """Explicit Claude Opus 5 streaming (CLI/IDE opt-in). Falls back to Sonnet."""
        yield from self._dispatch_by_family_stream(
            _LEGACY_CHAIN[TIER_OPUS_5]["stream"], prompt,
            precleared=precleared,
            precleared_findings=precleared_findings)

    def _try_claude_opus48_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        """Explicit Claude Opus 4.8 streaming (CLI-only). Falls back to Sonnet."""
        yield from self._dispatch_by_family_stream(
            _LEGACY_CHAIN[TIER_OPUS_48]["stream"], prompt,
            precleared=precleared,
            precleared_findings=precleared_findings)

    def _try_claude_sonnet5_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
    ):
        """Explicit Claude Sonnet 5 streaming (all channels). Falls back to Sonnet 4.6."""
        yield from self._dispatch_by_family_stream(
            _LEGACY_CHAIN[TIER_SONNET_5]["stream"], prompt,
            precleared=precleared,
            precleared_findings=precleared_findings)

    def _try_gemini_stream(
            self,
            prompt: str,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
            model: Optional[str] = None,
    ):
        yield from self._dispatch_by_family_stream(
            _LEGACY_CHAIN[TIER_VISION]["stream"], prompt, {"provider_model": model},
            precleared=precleared, precleared_findings=precleared_findings)

    @staticmethod
    def _collect(gen) -> str:
        if isinstance(gen, str):
            return gen
        try:
            # Only join real text tokens. The stream may also yield non-string
            # sentinels — __stream_meta__ dicts (token counts) and ReasoningMarker
            # / ToolMarker objects (Gap #2/Phase 5). These MUST be skipped, not
            # coerced, so a blocking generate() never picks up reasoning text or
            # raises inside join(). (Markers str() to "" anyway, but skipping is
            # explicit and safe.)
            return "".join(t for t in gen if isinstance(t, str) and t)
        except Exception as e:
            return f"Error collecting response: {e}"

    # --------------------------------------------------------
    # PUBLIC API
    # --------------------------------------------------------

    def generate_structured(self, blocks: list, model_hint: Optional[str] = None,
                            *, tier: Optional[Tier] = None,
                            legacy_hint: Optional[str] = None) -> str:
        """
        Claude-only call with structured content_blocks for block-level prompt caching.

        tier: OPTIONAL approved application tier — see generate().

        model_hint's default moved from the literal "solution" to None so that
        `tier=` is usable at all: _coerce_tier treats a non-None model_hint as
        a conflict, and a hard-coded default would have made every tier= call
        raise. "solution" is still substituted below whenever neither argument
        is supplied, so every existing caller behaves exactly as before.

        In production (LLM_PROXY_URL set), routes via _ProxyGateway which forwards
        the structured payload to services/llm_proxy/main.py on the LLM proxy server.

        In dev mode (no LLM_PROXY_URL) or when Claude is unavailable, flattens all
        blocks to a single string and delegates to generate() — no behavior change.

        Returns the model's text output (same shape as generate()).
        Never raises — falls back to flat generate() on any error.
        """
        model_hint = self._coerce_tier(model_hint, tier, legacy_hint)
        if model_hint is None:
            # Preserves the historical default for callers that pass neither.
            model_hint = "solution"
        claude = self._get_claude()

        # Dev mode or Claude unavailable: flatten to flat-string generate()
        if claude is None or not isinstance(claude, _ProxyGateway):
            flat = "\n\n".join(b.get("text", "") for b in blocks if b.get("text"))
            return self.generate(flat, model_hint=model_hint)

        # Resolve model for the given hint
        decision = self.route("placeholder",
                              model_hint=None if tier is not None else model_hint,
                              tier=tier, legacy_hint=legacy_hint)
        _model: Optional[str] = None
        if decision.tier == TIER_SOLUTION:
            _model = SOLUTION_MODEL
        elif decision.tier == TIER_OPUS_48:
            _model = CLAUDE_OPUS_48_MODEL
        elif decision.tier == TIER_OPUS_5:
            _model = CLAUDE_OPUS_5_MODEL
        elif decision.tier == TIER_SONNET_5:
            _model = CLAUDE_SONNET_5_MODEL
        elif decision.tier in (TIER_COMPLEX, TIER_HAIKU):
            _model = CLAUDE_PRIMARY_MODEL
        elif decision.tier == TIER_GOVERNED:
            # This method speaks only to the Claude proxy gateway (it sends
            # content_blocks for prompt caching), so a governed decision is
            # usable only when the resolver picked an Anthropic model. When it
            # picked something else, fall back to the Claude default rather
            # than sending an OpenAI id to ClaudeGateway, which 400s.
            _top = (decision.resolved or [None])[0]
            _model = (_top.model_id if _top is not None and _top.family == "anthropic"
                      else CLAUDE_PRIMARY_MODEL)
        else:
            _model = CLAUDE_PRIMARY_MODEL  # default Claude for any other tier

        self.last_tier                  = decision.tier
        self.last_model_label           = decision.model
        self.last_cache_read_tokens     = 0
        self.last_cache_creation_tokens = 0

        try:
            result = self._collect(
                _CB_CLAUDE.call(claude.generate, model=_model, content_blocks=blocks)
            )
            if not result or result.startswith("Error"):
                raise ValueError(f"generate_structured: bad result: {result!r}")
            self._propagate_tokens(decision.tier)
            logger.info(
                f"ModelRouter.generate_structured → {decision.model} "
                f"cache_read={self.last_cache_read_tokens} "
                f"cache_created={self.last_cache_creation_tokens}"
            )
            return result
        except Exception as e:
            _flat_len = sum(len(b.get("text", "")) for b in blocks)
            logger.warning(
                f"ModelRouter.generate_structured: Claude failed ({type(e).__name__}: {e}) — "
                f"flattening {len(blocks)} blocks (~{_flat_len} chars) to generate()"
            )
            flat = "\n\n".join(b.get("text", "") for b in blocks if b.get("text"))
            return self.generate(flat, model_hint=model_hint)

    @staticmethod
    def _hint_is_explicit_local(model_hint: Optional[str]) -> bool:
        """True when the caller explicitly asked for the in-house/local model.

        Distinct from the "simple" hint, which means "auto-route: try local, fall
        back to cloud" and must keep that behaviour. "local", "local:<model>" and
        "inhouse" are a deliberate choice of the model the API labels
        "Local (In-house) - In-house GPU, free, private", so falling back to a
        paid cloud provider would silently send that turn off the machine.
        """
        h = (model_hint or "").lower().strip()
        return h in ("local", "inhouse", "in-house") or h.startswith("local:")

    def generate(self, prompt, model_hint: Optional[str] = None, return_meta=False,
                 precleared: bool = False, precleared_findings: Optional[list] = None,
                 data_classification: Optional[str] = None,
                 *, tier: Optional[Tier] = None,
                 legacy_hint: Optional[str] = None,
                 no_cloud_egress: bool = False,
                 distinct_from_family: Optional[str] = None,
                 require_role: Optional[str] = None,
                 acl_filter: Optional[AclFilter] = None):
        """Route prompt to the correct gateway. Never raises — returns error str on failure.
        prompt: str OR list[dict] (multi-turn messages array).

        distinct_from_family / require_role:
            §M constraints, forwarded to route(). Accepted here as of §N.1
            step 10 — route() has taken distinct_from_family since Phase 5 but
            generate() never passed it on, so the only caller that needs it
            (the SDLC manifest judge) had no way to reach it without calling
            route() itself.

        tier:
            OPTIONAL approved application tier (core.tiers.Tier), mutually
            exclusive with model_hint. Keyword-only and enum-typed. Supplying
            both raises ValueError — a programming error, distinct from the
            runtime failures this method converts to an error string.

        precleared / precleared_findings:
            When True, downstream OpenAI/Gemini gateways skip their second-pass
            compliance_engine.validate_input() block decision and re-use the
            provided findings for redaction only.  Used by callers (e.g.
            _enhance_core) that have already run an upstream compliance gate on
            the raw user input.
        data_classification:
            Optional sensitivity tag. When at/above CONFIDENTIAL the PRIVACY
            FLOOR pins routing to the local model AND disables cloud fallback in
            _dispatch — restricted data must never egress, even if local is down.
        """
        model_hint = self._coerce_tier(model_hint, tier, legacy_hint)
        if not prompt:
            return ""
        try:
            # `tier` goes to route() as well as being coerced above: with
            # governance on, route() resolves the Tier directly, and the three
            # modality tiers have no legacy hint to be coerced into.
            decision = self.route(prompt, model_hint=None if tier is not None else model_hint,
                                  tier=tier, legacy_hint=legacy_hint,
                                  no_cloud_egress=no_cloud_egress,
                                  data_classification=data_classification,
                                  distinct_from_family=distinct_from_family,
                                  require_role=require_role,
                                  acl_filter=acl_filter)
        except Exception as exc:
            # Returned as a string because generate()'s contract is that it never raises.
            if type(exc).__name__ != "NoEligibleModel":
                raise
            return no_eligible_message(exc)
        self._record_selection(decision)
        logger.info(f"ModelRouter → {decision.model} (tier={decision.tier})")
        # PRIVACY FLOOR: when enforced, no-cloud-fallback is propagated into
        # _dispatch so a local outage fails closed instead of egressing.
        _privacy_local_only = _privacy_requires_local(data_classification)
        # An explicit local request is local-only for the same reason restricted
        # data is: the caller chose a model advertised as in-house and private.
        if self._hint_is_explicit_local(model_hint):
            if not _privacy_local_only:
                logger.info(
                    "ModelRouter: explicit local hint %r — cloud fallback disabled "
                    "for this turn", model_hint,
                )
            _privacy_local_only = True

        # Seed the label from the decision so the meta footer reflects the
        # routed model (incl. the specific Gemini variant), even when no _try_*
        # method overrides it. _try_* fallbacks (e.g. Claude on Gemini failure)
        # still overwrite this with their own "[fallback]" label.
        self.last_model_label = decision.model

        # Start latency timer BEFORE the LLM call so elapsed reflects real wall-clock time.
        import time
        _t0 = time.perf_counter()

        # Build compliance kwargs to forward through _dispatch → _try_* → gateway.generate()
        _compliance_kw = {}
        if precleared:
            _compliance_kw = {"precleared": True, "precleared_findings": precleared_findings or []}

        if _privacy_local_only:
            _compliance_kw["privacy_local_only"] = True

        output, was_fallback = self._dispatch(
            decision.tier, prompt, provider_model=decision.provider_model_override,
            candidates=decision.resolved,
            **_compliance_kw,
        )

        elapsed = time.perf_counter() - _t0

        if was_fallback:
            logger.warning(f"ModelRouter: fallback used for tier={decision.tier}")
        self.last_tier = decision.tier
        self._propagate_tokens(decision.tier)

        if not return_meta:
            return output

        # elapsed was already measured above (line _t0 → after _dispatch)
        # Do NOT reset the timer here — that would always give latency=0.000
        in_tok  = getattr(self, "last_input_tokens",  0) or 0
        out_tok = getattr(self, "last_output_tokens", 0) or 0

        from gateway import _estimate_cost
        meta = {
            "model":    self.last_model_label,
            "in_tok":   in_tok,
            "out_tok":  out_tok,
            "tokens":   in_tok + out_tok,
            "cost_usd": _estimate_cost(self.last_model_label, in_tok, out_tok),
            "latency":  round(elapsed, 3),
        }
        return {"text": output, "meta": meta}

    def stream(
            self,
            prompt,
            model_hint: Optional[str] = None,
            local_model: Optional[str] = None,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
            *,
            tier: Optional[Tier] = None,
            legacy_hint: Optional[str] = None,
            no_cloud_egress: bool = False,
            distinct_from_family: Optional[str] = None,
            require_role: Optional[str] = None,
            acl_filter: Optional[AclFilter] = None,
    ):
        """Route prompt and yield tokens directly (true token streaming).
        prompt: str OR list[dict] (multi-turn messages array).
        tier: OPTIONAL approved application tier — see generate().
        distinct_from_family / require_role: §M constraints — see generate().

        precleared / precleared_findings:
            Forwarded to the OpenAI / Gemini / proxy gateways so that callers
            (the FastAPI /ask handler) which have already run
            compliance_engine.validate_input() on the current user turn can
            instruct the downstream provider gateway to skip its second-pass
            block decision and re-use the gateway's findings for redaction.
            Default False preserves full second-pass validation for callers
            that have not run an upstream compliance gate.

        ── Final sentinel ───────────────────────────────────────────────
        After token streaming completes, this generator yields ONE extra
        dict of shape:
            {"__stream_meta__": {
                "in_tok":      int,
                "out_tok":     int,
                "model_label": str,
                "tier":        str,
                "thinking":    str,   # extended-thinking text if any
            }}

        Why a sentinel and not properties?
            Under uvicorn + FastAPI's StreamingResponse, the sync generator
            is driven via anyio.iterate_in_threadpool, which can resume each
            next() call on a DIFFERENT worker thread. The previous design
            stored token counts in `self._tl.last_input_tokens` (a
            threading.local) and the gateway read them after the loop —
            but if the final _propagate_tokens write landed on thread T2
            and the gateway's read happened on T3, T3's local was 0, the
            fallback word-count gave a tiny number (~3), and observed
            in_tok was wrong. Carrying the values *as data* through the
            yield protocol bypasses thread state entirely.

        Callers that need the meta should detect the sentinel:
            for tok in mr.stream(...):
                if isinstance(tok, dict) and "__stream_meta__" in tok:
                    meta = tok["__stream_meta__"]
                    continue
                ...handle string token...
        Older callers that don't check for it will harmlessly ignore the
        dict (assuming they typecheck or no-op on non-string tokens).
        """
        model_hint = self._coerce_tier(model_hint, tier, legacy_hint)
        if not prompt:
            return
        try:
            # needs_streaming: the call shape is known HERE and nowhere else,
            # so the resolver can drop a candidate whose capabilities say it
            # cannot stream rather than discovering it mid-response.
            decision = self.route(prompt, model_hint=None if tier is not None else model_hint,
                                  tier=tier, legacy_hint=legacy_hint,
                                  no_cloud_egress=no_cloud_egress, needs_streaming=True,
                                  distinct_from_family=distinct_from_family,
                                  require_role=require_role,
                                  acl_filter=acl_filter)
        except Exception as exc:
            if type(exc).__name__ != "NoEligibleModel":
                raise
            yield no_eligible_message(exc)
            return
        self._record_selection(decision)
        logger.info(f"ModelRouter.stream → {decision.model} (tier={decision.tier})"
                    + (f" [local_model={local_model}]" if local_model else ""))
        self.last_model_label   = decision.model
        self.last_tier          = decision.tier
        self.last_input_tokens  = 0
        self.last_output_tokens = 0
        yield from self._dispatch_stream(
            decision.tier, prompt,
            local_model=local_model,
            precleared=precleared,
            precleared_findings=precleared_findings,
            provider_model=decision.provider_model_override,
            candidates=decision.resolved,
        )
        self._propagate_tokens(decision.tier)
        # Sentinel — read the values RIGHT NOW (same thread frame as
        # _propagate_tokens) so the dict carries snapshot data, not thread
        # state. The caller captures this dict inside its for-loop.
        try:
            _thinking_text = ""
            try:
                _claude_gw = self._claude
                if _claude_gw is not None:
                    _thinking_text = getattr(_claude_gw, "_last_thinking_text", "") or ""
            except Exception:
                pass
            yield {
                "__stream_meta__": {
                    "in_tok":      int(self.last_input_tokens or 0),
                    "out_tok":     int(self.last_output_tokens or 0),
                    "model_label": str(self.last_model_label or ""),
                    "model_id":    str(self.last_model_id or ""),   # bare model ID (no display prefix)
                    "tier":        str(self.last_tier or ""),
                    "thinking":    _thinking_text,
                }
            }
        except Exception as _meta_err:
            logger.debug(f"stream() meta sentinel skipped: {_meta_err}")

    async def async_generate(self, prompt, model_hint: Optional[str] = None,
                             *, tier: Optional[Tier] = None,
                             legacy_hint: Optional[str] = None,
                             no_cloud_egress: bool = False,
                             acl_filter: Optional[AclFilter] = None) -> str:
        """Async route + generate. Uses persistent AsyncClient — no thread held during LLM I/O.
        Falls back to sync generate() when LLM_PROXY_URL is not set (local dev / direct gateway).
        prompt: str OR list[dict] (multi-turn messages array).
        tier: OPTIONAL approved application tier — see generate().
        """
        model_hint = self._coerce_tier(model_hint, tier, legacy_hint)
        if not prompt:
            return ""
        proxy = _llm_proxy_url()
        if not proxy:
            # Local dev: no proxy, fall back to sync (run in threadpool via caller)
            return self.generate(prompt,
                                 model_hint=None if tier is not None else model_hint,
                                 tier=tier, legacy_hint=legacy_hint,
                                 no_cloud_egress=no_cloud_egress)
        # tier= goes to route() as well as being coerced above, for the same
        # reason generate() does it: the coerced hint is only the FALLBACK, and
        # the three modality tiers have no legacy hint at all.
        decision = self.route(prompt,
                              model_hint=None if tier is not None else model_hint,
                              tier=tier, legacy_hint=legacy_hint,
                              no_cloud_egress=no_cloud_egress,
                              acl_filter=acl_filter)
        self._record_selection(decision)
        logger.info(f"ModelRouter.async_generate → {decision.model} (tier={decision.tier})")
        gw_map = {
            TIER_SIMPLE:     self._get_local(),    # local stays sync
            TIER_MINI:       self._get_openai(),
            TIER_LOCAL_MINI: self._get_openai(),
            TIER_MEDIUM:     self._get_openai(),
            TIER_DEEP:       self._get_openai(),
            TIER_COMPLEX:    self._get_claude(),
            TIER_HAIKU:      self._get_claude(),
            TIER_VISION:     self._get_gemini(),
            TIER_GEMINI:     self._get_gemini(),
            TIER_SOLUTION:   self._get_claude(),
            TIER_OPUS_48:    self._get_claude(),
            TIER_OPUS_5:     self._get_claude(),
            TIER_SONNET_5:   self._get_claude(),
            TIER_TERA:       self._get_openai(),
            TIER_LUNA:       self._get_openai(),
        }
        gw = gw_map.get(decision.tier)
        if gw is None:
            logger.warning(f"ModelRouter.async_generate: no gateway for tier={decision.tier}")
            return self.generate(prompt, model_hint=model_hint)
        if not hasattr(gw, "async_generate"):
            # Local LLM or direct gateway without async support — sync fallback
            return self.generate(prompt, model_hint=model_hint)
        self.last_tier        = decision.tier
        self.last_model_label = decision.model

        # Build the client to pass to async_generate().
        # Per-call (default): fresh client each call — loop-agnostic.
        # Shared (legacy):    singleton — only safe under a single event loop.
        _per_call = _use_per_call_async_client()
        _owned_client = _make_async_client() if _per_call else None
        try:
            kwargs: dict = {}
            if _per_call and _owned_client is not None:
                kwargs["_override_client"] = _owned_client
            result = await gw.async_generate(prompt, **kwargs)
            self._propagate_tokens(decision.tier)
            return result
        except Exception as e:
            if decision.tier in (TIER_COMPLEX, TIER_HAIKU, TIER_SOLUTION, TIER_OPUS_48, TIER_OPUS_5, TIER_SONNET_5):
                logger.warning(
                    f"ModelRouter.async_generate: Claude failed (tier={decision.tier}) "
                    f"→ GPT fallback: {e}"
                )
                openai_gw = self._get_openai()
                if openai_gw and hasattr(openai_gw, "async_generate") and not _CB_OPENAI.is_open:
                    _fb_client = _make_async_client() if _per_call else None
                    try:
                        self.last_model_label = (
                            f"{OPENAI_CODING_DISPLAY} ({_resolve_tier_model(OPENAI_CODING_MODEL, 'openai', 'medium')}) [fallback]"
                        )
                        fb_kwargs: dict = {}
                        if _per_call and _fb_client is not None:
                            fb_kwargs["_override_client"] = _fb_client
                        result = await openai_gw.async_generate(prompt, **fb_kwargs)
                        self._propagate_tokens(TIER_MEDIUM)
                        return result
                    except Exception as fe:
                        logger.warning(
                            f"ModelRouter.async_generate: GPT fallback also failed: {fe}"
                        )
                    finally:
                        if _fb_client is not None:
                            await _fb_client.aclose()
            raise
        finally:
            if _owned_client is not None:
                await _owned_client.aclose()

    async def async_stream(
            self,
            prompt,
            model_hint: Optional[str] = None,
            local_model: Optional[str] = None,
            precleared: bool = False,
            precleared_findings: Optional[list] = None,
            conv_id: Optional[str] = None,
            *,
            tier: Optional[Tier] = None,
            legacy_hint: Optional[str] = None,
            no_cloud_egress: bool = False,
            acl_filter: Optional[AclFilter] = None,
    ):
        """Async streaming generator — yields str tokens then a sentinel dict.

        tier: OPTIONAL approved application tier — see generate().

        Mirrors stream() but runs entirely on the event loop so FastAPI's async
        StreamingResponse can flush each token to the client the instant it
        arrives, without blocking a thread-pool worker.

        Yields the same sentinel as stream():
            {"__stream_meta__": {"in_tok": int, "out_tok": int,
                                  "model_label": str, "tier": str}}

        Falls back to running the sync stream() in a thread when the gateway
        does not support async_stream() (e.g. local LLM, direct gateway without
        proxy).
        """
        model_hint = self._coerce_tier(model_hint, tier, legacy_hint)
        if not prompt:
            return

        decision = self.route(prompt,
                              model_hint=None if tier is not None else model_hint,
                              tier=tier, legacy_hint=legacy_hint,
                              no_cloud_egress=no_cloud_egress, needs_streaming=True,
                              acl_filter=acl_filter)
        self._record_selection(decision)
        logger.info(
            f"ModelRouter.async_stream → {decision.model} (tier={decision.tier})"
            + (f" [local_model={local_model}]" if local_model else "")
        )
        self.last_model_label = decision.model
        self.last_tier        = decision.tier
        self.last_input_tokens  = 0
        self.last_output_tokens = 0

        gw = None
        if decision.tier == TIER_SIMPLE:
            gw = self._get_local()
        elif decision.tier in (TIER_MINI, TIER_LOCAL_MINI, TIER_MEDIUM, TIER_DEEP,
                               TIER_TERA, TIER_LUNA):
            gw = self._get_openai()
        elif decision.tier in (TIER_COMPLEX, TIER_HAIKU, TIER_SOLUTION,
                               TIER_OPUS_48, TIER_OPUS_5, TIER_SONNET_5):
            gw = self._get_claude()
        elif decision.tier in (TIER_VISION, TIER_GEMINI):
            gw = self._get_gemini()
        # TIER_GOVERNED is deliberately absent: the native-async branch below
        # calls ONE gateway and has no way to try the next candidate when that
        # call fails. Leaving gw None routes governed streaming through the
        # sync bridge, which goes via _dispatch_stream and therefore keeps
        # §M.5's within-tier fallback. A native async path for the governed
        # tier is worth having, but not at the cost of silently dropping the
        # fallback the whole tier model is built on.

        if gw is not None and hasattr(gw, "async_stream"):
            # Native async streaming path — no thread held.
            _model_override = decision.provider_model_override or None
            async for tok in gw.async_stream(
                    prompt,
                    model=_model_override,
                    precleared=precleared,
                    precleared_findings=precleared_findings,
            ):
                yield tok
            self._propagate_tokens(decision.tier)
        else:
            # Fallback: run the sync stream() in a thread so we don't block
            # the event loop. Tokens are collected and yielded one by one.
            import asyncio as _asyncio
            _loop = _asyncio.get_event_loop()
            _queue: "asyncio.Queue[object]" = _asyncio.Queue()
            _SENTINEL = object()
            _thread_meta: dict[str, str | int] = {}

            # Capture the caller's ContextVar snapshot so the sync generator
            # thread inherits request_id / user_id / chat_id / correlation_id.
            # Without this, plain threading.Thread starts with an empty context
            # and [LOCAL USAGE] / [LLM DISPATCH] logs show "-" for every field.
            import contextvars as _cv
            _ctx_snapshot = _cv.copy_context()

            def _run_sync():
                def _inner():
                    try:
                        for tok in self._dispatch_stream(
                                decision.tier, prompt,
                                local_model=local_model,
                                precleared=precleared,
                                precleared_findings=precleared_findings,
                                provider_model=decision.provider_model_override,
                                candidates=decision.resolved,
                        ):
                            _loop.call_soon_threadsafe(_queue.put_nowait, tok)
                        self._propagate_tokens(decision.tier)
                        _thread_meta.update({
                            "in_tok": int(self.last_input_tokens or 0),
                            "out_tok": int(self.last_output_tokens or 0),
                            "model_label": str(self.last_model_label or ""),
                            "tier": str(decision.tier),
                        })
                    except Exception as _e:
                        _loop.call_soon_threadsafe(_queue.put_nowait, _e)
                    finally:
                        _loop.call_soon_threadsafe(_queue.put_nowait, _SENTINEL)
                _ctx_snapshot.run(_inner)

            import threading as _threading
            _t = _threading.Thread(target=_run_sync, daemon=True)
            _t.start()
            while True:
                item = await _queue.get()
                if item is _SENTINEL:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
            self.last_input_tokens = int(_thread_meta.get("in_tok", 0))
            self.last_output_tokens = int(_thread_meta.get("out_tok", 0))
            self.last_model_label = str(_thread_meta.get("model_label", decision.model))
            self.last_tier = str(_thread_meta.get("tier", decision.tier))

        # Emit the same sentinel dict as stream() so callers can capture meta.
        # Fix 3: include model_id (bare ID, no display prefix) for parity with
        # stream() — callers that read meta["model_id"] no longer need to parse
        # the label string via _resolve_model_id().
        try:
            yield {
                "__stream_meta__": {
                    "in_tok":      getattr(self, "_tl_in",  0) or self.last_input_tokens  or 0,
                    "out_tok":     getattr(self, "_tl_out", 0) or self.last_output_tokens or 0,
                    "model_label": self.last_model_label or "",
                    "model_id":    str(self.last_model_id or ""),  # bare model ID (no display prefix)
                    "tier":        str(self.last_tier or ""),
                }
            }
        except Exception:
            pass


# ============================================================
# SINGLETON
# ============================================================

model_router = ModelRouter()


def get_router() -> ModelRouter:
    """The router singleton.

    Four call sites in three modules already did `from models.model_router
    import get_router` — agents/review_engine.py (x2),
    agents/advanced_reasoning.py and memory/postgres_memory.py — and it did
    not exist, so every one of them raised ImportError into a bare
    `except Exception` and silently returned its failure value. Adding the
    accessor they expect repairs all four rather than rewriting each.
    """
    return model_router


def resolve_media_model(tier: Tier, *, channel: Optional[str] = None):
    """Resolve an output-modality tier to a concrete model. Never dispatches.

    Phase 6 §N.1 step 5. Image generation and video generation do not go
    through generate()/stream() at all — they call generate_imagen() and
    generate_veo_video() on a provider gateway, whose signatures and return
    types have nothing in common with a text completion. So the thing those
    call sites need from governance is not a dispatcher, it is an ANSWER:
    which model, from which family, with which capabilities.

    Returns a core.tier_resolver.ResolvedModel and raises NoEligibleModel
    when the tier has no eligible assignment. It does NOT fall back to the
    .env constants: answering "generate a video of a tiger" with a
    paragraph about tigers is a confusing wrong answer; an error naming the
    unassigned tier is a fixable one.
    """
    from core.tier_resolver import resolve_tier
    rm = resolve_tier(tier, channel=channel)
    logger.info(
        "ModelRouter: %s → %s (%s, priority %d)",
        tier.value, rm.model_id, rm.provider_slug, rm.priority,
    )
    return rm


def last_selection_audit() -> dict:
    """The §L.5 fields for the ainxt.metrics event, for the CURRENT thread.

    A function rather than two attribute reads so the six producers in
    gateway.py / kb_ask_router.py stay one line each and cannot disagree about
    the key names the Kafka consumer expects.

    Thread affinity is the same as last_model_label's: the values are written
    where route() ran. Two consequences, both absorbed by the columns being
    nullable — a gap in the measurement, never a wrong answer in the billing
    trail:

      * a producer on a different thread from the router call reads None;
      * a turn served from the Redis or semantic cache never calls route() at
        all, so it reports whatever that thread last routed. Read the column
        as "provenance of the last routed turn on this thread", not as a
        per-row guarantee, when analysing the rollout.
    """
    return {
        "selection_mode": model_router.last_selection_mode,
        "requested_tier": model_router.last_requested_tier,
    }
