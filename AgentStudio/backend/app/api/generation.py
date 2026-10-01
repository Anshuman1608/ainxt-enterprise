# SPDX-License-Identifier: MIT
"""AI generation endpoints: /llm/models, /generate-instructions, /generate-workflow."""
import json

import os
import re
from fastapi import APIRouter, Depends, HTTPException, Request
from app.models import (
    GenerateInstructionsRequest, GenerateInstructionsResponse,
    GenerateWorkflowRequest, GenerateWorkflowResponse,
    AuthenticatedUser,
)
from app.core.llm_handler import get_llm_client, Message
from app.core.config import (
    build_meta_llm_config,
    openai_compatible_base_url, openai_compatible_api_key,
    factory_model, factory_base_url, factory_api_key,
)
from app.core.factory_utils import clean_llm_text
from app.api.deps import require_access

router = APIRouter()
from core.logger import logger
# Pseudo-model id for the Chat router's "Auto" entry. Never subject to
# governance filtering — it's a routing hint, not a real catalogue entry.
_PSEUDO_MODEL_AUTO = "auto"

_LOCAL_PROVIDER_LABEL = "Local (In-house)"

# Stable display order for CLI-aligned model groups.
_PROVIDER_ORDER = ["Claude", "OpenAI", "Gemini"]
_PROVIDER_GROUP_LABELS = {
    "anthropic": "Claude",
    "openai":    "OpenAI",
    "google":    "Gemini",
    "inhouse":   _LOCAL_PROVIDER_LABEL,
}


def _cli_reference_models() -> list[dict]:
    """Curated catalogue shape matching messages_compat_router.list_models_compat.

    Primary source: core.llm_provider_registry (the same admin-managed "LLM
    Providers" data GET /v1/all-models reads exclusively) — a provider
    configured purely through that screen (DB row, no matching env var) used
    to be invisible here even though it showed up correctly on the web Chat
    picker. An unreadable registry gives an empty catalogue.
    """
    try:
        from core.llm_provider_registry import get_cli_style_models
        models = get_cli_style_models()
        # Preserve the generic "local" alias (routes to whichever LOCAL_LLM_BASE_URL
        # is configured) as its own selectable entry, matching the catalogue shape
        # every caller here was built against — _ensure_local_provider_group and
        # messages_compat_router.list_models_compat both expect it to be present.
        if not any(m.get("id") == "local" for m in models):
            models.append({
                "id": "local", "hint": "local", "provider": "inhouse",
                "label": "Local (In-house)",
                "tag": "In-house GPU · free · private",
            })
        return models
    except Exception as exc:
        # Unreadable registry = empty catalogue (Phase 8 removed the env fallback).
        logger.error(f"[AGENT] core.llm_provider_registry unavailable: {exc}")
        return []


def _matches_discovered_model(model_id: str, discovered_ids: set[str]) -> bool:
    if model_id in discovered_ids:
        return True
    return any(mid.endswith(f"/{model_id}") for mid in discovered_ids)


def _group_catalogue(models: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for model in models:
        label = _PROVIDER_GROUP_LABELS.get(model.get("provider", ""), "Other")
        grouped.setdefault(label, []).append(model)
    ordered_labels = [p for p in _PROVIDER_ORDER if p in grouped]
    ordered_labels += [p for p in (_LOCAL_PROVIDER_LABEL, "Other") if p in grouped and p not in ordered_labels]
    ordered_labels += [p for p in grouped if p not in ordered_labels]
    return [{"provider": label, "models": grouped[label]} for label in ordered_labels]


def _llm_proxy_config() -> tuple[str, dict]:
    """Return (base_url, headers) for the platform llm_proxy service.

    ``LLM_PROXY_URL`` points at the FastAPI llm_proxy service (the LLM proxy server in prod,
    localhost:8003 in dev) which fronts Claude / OpenAI / Gemini and exposes
    an OpenAI-compatible ``GET /v1/models`` discovery endpoint. The proxy
    enforces a pre-shared ``X-Internal-Token`` header for all non-public
    routes — we mirror the helper used in ``core.proxy_tool_use`` and
    ``app.core.llm_handler`` so authentication stays consistent.
    """
    from app.core.config import llm_proxy_root as _llm_proxy_root
    base = _llm_proxy_root()
    token = os.getenv("LLM_PROXY_TOKEN", "").strip()
    headers: dict = {"Accept": "application/json"}
    if token:
        headers["X-Internal-Token"] = token
    return base, headers


def _flatten_provider_ids(providers) -> list[str]:
    """Ordered, deduplicated list of model IDs across all provider groups."""
    seen: set = set()
    out: list[str] = []
    for group in providers:
        for m in group.get("models", []):
            mid = m.get("id")
            if mid and mid not in seen:
                seen.add(mid)
                out.append(mid)
    return out


async def _fetch_llm_proxy_models() -> tuple[list, str]:
    """Return the cloud (Claude / OpenAI / Gemini) catalogue, mirroring the CLI.

    The CLI's ``/v1/models`` (``routers/messages_compat_router.py::list_models_compat``)
    builds its catalogue STATICALLY from ``core/model_registry.py`` constants — it
    never calls the platform proxy for discovery. We do the same here so the
    ABStudio Agent Configuration dropdown shows the same Claude / OpenAI /
    Gemini groups the CLI shows even when the proxy build deployed in front of
    ABStudio doesn't expose ``GET /v1/models`` (some proxy revisions only serve
    ``POST /v1/chat/completions``).

    Behaviour:
      * ``LLM_PROXY_URL`` unset → return the full curated cloud catalogue with
        ``error="LLM_PROXY_URL not set"``. The runtime call paths still fall
        through to ``OPENAI_COMPATIBLE_BASE_URL`` / ``LOCAL_LLM_BASE_URL``, so
        cloud ids in the dropdown are cosmetic in pure-standalone dev — same
        situation the CLI exposes when run without the proxy.
      * Proxy set, ``GET /v1/models`` returns 2xx with a non-empty payload →
        intersect the curated catalogue with the discovered ids. This preserves
        the ability for an operator to centrally HIDE a model by removing it
        from the proxy's discovery payload.
      * Proxy set, ``GET /v1/models`` fails (404, timeout, etc.) → return the
        full curated cloud catalogue + a non-empty error string for telemetry
        and ``/llm/models/debug``. Do NOT empty the catalogue — the runtime
        ``POST /v1/chat/completions`` path is unaffected by discovery failures.
      * Proxy set, ``GET /v1/models`` returns 2xx with empty payload → treat as
        "discovery not implemented at this proxy build" and return the full
        curated cloud catalogue.

    Returns ``(providers, error)``. ``providers`` is the grouped list shape
    ``[{"provider": <label>, "models": [{"id","label","hint","tag"}, ...]}, ...]``.

    NOTE: llm_proxy intentionally excludes the in-house Local LLM — local is on
    the internal network and is reached directly by the gateway server. Local models are
    added separately by ``_fetch_local_models`` below.
    """
    curated_cloud_models = [
        model for model in _cli_reference_models()
        if model.get("provider") != "inhouse"
    ]
    curated_cloud_groups = _group_catalogue(curated_cloud_models)

    base, headers = _llm_proxy_config()
    if not base:
        return curated_cloud_groups, "LLM_PROXY_URL not set"

    # Short timeout — this is an interactive dropdown probe, not a blocking
    # dependency. A hung proxy must not stall Agent Configuration panel
    # open; the docstring's "best-effort" semantics rely on failing fast.
    discovery_url = f"{base}/v1/models"
    try:
        import httpx
        ssl_verify = os.getenv("SSL_VERIFY", "true").lower() not in ("false", "0", "no")
        async with httpx.AsyncClient(verify=ssl_verify) as client:
            response = await client.get(discovery_url, headers=headers, timeout=3)
            response.raise_for_status()
            payload = response.json()
    except Exception as e:
        return curated_cloud_groups, f"llm_proxy /v1/models failed: {e}"

    raw_items: list = []
    if isinstance(payload, dict):
        raw_items = payload.get("data") or payload.get("models") or []
    elif isinstance(payload, list):
        raw_items = payload

    discovered_ids = {
        item.get("id") or item.get("name")
        for item in raw_items
        if isinstance(item, dict) and (item.get("id") or item.get("name"))
    }
    if not discovered_ids:
        return curated_cloud_groups, ""

    filtered_cloud = [
        model for model in curated_cloud_models
        if _matches_discovered_model(model.get("id", ""), discovered_ids)
    ]
    return _group_catalogue(filtered_cloud), ""


def _fetch_local_models() -> tuple[list[str], str]:
    """Pull the in-house Local LLM model IDs directly.

    Returns ``(model_ids, error_message)``. ``error_message`` is an empty
    string on success. llm_proxy intentionally does not front the in-house
    LLM (internal-network-only), so the locals come via the same helper the
    root gateway uses — but with the failure reason surfaced instead of
    swallowed silently.
    """
    try:
        from gateway_local_llm import get_local_gateway as _get_local_gw  # type: ignore
    except Exception as e:
        return [], f"gateway_local_llm import failed: {e}"
    try:
        ids = list(_get_local_gw().list_models() or [])
        return ids, ""
    except Exception as e:
        return [], f"local list_models() failed: {e}"


def _ensure_local_provider_group(providers: list, local_ids: list[str]) -> list:
    """Append (or extend) the Local/In-house group using CLI-compatible IDs."""
    existing_ids: set = set()
    local_group = None
    for group in providers:
        if group.get("provider") == _LOCAL_PROVIDER_LABEL:
            local_group = group
        for m in group.get("models", []):
            mid = m.get("id")
            if mid:
                existing_ids.add(mid)

    local_models = [m for m in _cli_reference_models() if m.get("provider") == "inhouse"]
    local_models += [
        {
            "id": mid, "hint": mid, "provider": "inhouse",
            "label": mid, "tag": "In-house GPU · free · private",
        }
        for mid in local_ids
        if mid and mid not in existing_ids and mid != "local"
    ]
    new_entries = [m for m in local_models if m.get("id") not in existing_ids]
    if not new_entries:
        return providers

    if local_group is not None:
        local_group["models"] = list(local_group.get("models", [])) + new_entries
        return providers

    return list(providers) + [{
        "provider": _LOCAL_PROVIDER_LABEL,
        "models": new_entries,
    }]


@router.get("/llm/models")
async def list_llm_models(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Return the full model catalogue available to the user.

    Routing (mirrors the CLI ``/v1/models`` shape — see
    ``routers/messages_compat_router.py::list_models_compat``):
      * Cloud models (Claude / OpenAI / Gemini): served from the static
        CLI-aligned catalogue built by ``_cli_reference_models()``. A
        best-effort probe of ``GET {LLM_PROXY_URL}/v1/models`` is still
        attempted with the platform ``X-Internal-Token`` header — when
        the proxy returns a non-empty discovery payload the curated
        list is intersected with it so operators can centrally hide
        models, otherwise the full curated list is returned so the
        dropdown matches the CLI's ``/model`` picker even on proxy
        builds that don't expose ``/v1/models``.
      * Local in-house models: pulled directly from ``gateway_local_llm``
        because llm_proxy is on the public-facing network and does not
        front the internal LLM cluster.

    The merged catalogue is then filtered against the per-user governance
    allowlist (shared with ``GET /model-governance/my-models``). Runtime
    routing of any selected id still flows through
    ``openai_compatible_base_url()`` → ``{LLM_PROXY_URL}/v1`` in
    ``app/core/llm_handler.py::OpenAIClient`` — the same path the CLI uses
    for ``POST /v1/chat/completions``.
    """
    base_url = openai_compatible_base_url()
    response = await resolve_available_models(current_user)
    response["base_url_configured"] = bool(base_url)
    return response


def max_tokens_limit() -> int:
    """The `le` bound on LLMConfig.max_tokens: the platform limit, the same for every model."""
    from app.models import LLMConfig
    return next(m.le for m in LLMConfig.model_fields["max_tokens"].metadata if hasattr(m, "le"))


async def resolve_available_models(current_user) -> dict:
    """Resolve the full model catalogue available to ``current_user``.

    Reusable helper extracted from ``list_llm_models`` so other endpoints
    (e.g. the agent-factory patcher) can validate a model id against the
    exact same catalogue the ``/llm/models`` dropdown shows. Returned shape
    matches the ``/llm/models`` payload minus the top-level route-only fields
    added by ``list_llm_models`` itself.
    """
    import asyncio
    (providers, proxy_error), (local_ids, local_error) = await asyncio.gather(
        _fetch_llm_proxy_models(),
        asyncio.to_thread(_fetch_local_models),
    )
    if proxy_error:
        logger.info(f'[AGENT] llm_proxy /v1/models probe non-fatal: {proxy_error}')

    providers = _ensure_local_provider_group(providers, local_ids)
    if local_error:
        logger.info(f'[AGENT] Local LLM models not merged into /llm/models: {local_error}')

    flat_ids = _flatten_provider_ids(providers)
    allowed = _filter_user_models(
        flat_ids,
        getattr(current_user, "id", "") or "",
        getattr(current_user, "department", "") or "",
    )
    allowed.add(_PSEUDO_MODEL_AUTO)

    filtered_providers = []
    for group in providers:
        kept = [m for m in group.get("models", []) if m.get("id") in allowed]
        if kept:
            filtered_providers.append({"provider": group.get("provider"), "models": kept})

    filtered_flat = [mid for mid in flat_ids if mid in allowed]
    default_model = next((mid for mid in filtered_flat if mid != _PSEUDO_MODEL_AUTO), None)

    response = {
        "provider": "ainxt",
        "providers": filtered_providers,
        "models": filtered_flat,
        "default_model": default_model,
        "max_tokens_limit": max_tokens_limit(),
    }
    if proxy_error:
        response["llm_proxy_error"] = proxy_error
    if local_error:
        response["local_error"] = local_error
    return response


@router.get("/llm/models/debug")
async def list_llm_models_debug(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Diagnostic view of every source feeding /llm/models.

    SIT-only helper for figuring out *why* a particular provider group is
    missing — surfaces env-var visibility and the exact error string each
    source raised. No governance filter applied.
    """
    sources: dict = {}

    # 1. llm_proxy /v1/models (cloud catalogue source of truth)
    proxy_providers, proxy_error = await _fetch_llm_proxy_models()
    proxy_base, _ = _llm_proxy_config()
    sources["llm_proxy"] = {
        "ok": not proxy_error,
        "url": f"{proxy_base}/v1/models" if proxy_base else "",
        "providers": [
            {
                "provider": g.get("provider"),
                "model_count": len(g.get("models", [])),
                "model_ids": [m.get("id") for m in g.get("models", [])],
            }
            for g in proxy_providers
        ],
        "error": proxy_error or None,
    }

    # 2. Direct local-LLM probe (in-house cluster — not behind llm_proxy)
    local_ids, local_error = _fetch_local_models()
    sources["local_llm"] = {
        "ok": not local_error,
        "model_count": len(local_ids),
        "model_ids": local_ids,
        "error": local_error or None,
    }

    # 3. Orchestrator runtime target — what ``native_engine._run_agent`` will
    #    actually call when an agent has empty base_url / api_key (the common
    #    case, since the frontend saves them blank). Diagnoses SIT failures
    #    where /llm/models works but agent execution raises "LLM unreachable".
    sources["orchestrator_runtime"] = {
        "openai_compatible_base_url": openai_compatible_base_url(),
        "openai_compatible_api_key_set": bool(openai_compatible_api_key())
            and openai_compatible_api_key() != "not-needed",
        "factory_base_url": factory_base_url(),
        "factory_api_key_set": bool(factory_api_key())
            and factory_api_key() != "not-needed",
        "x_internal_token_will_be_sent": bool(os.getenv("LLM_PROXY_TOKEN", "")),
    }

    # 4. Env-var visibility — presence flags so admins can confirm SIT config
    #    without leaking secrets. Non-secret URLs are exposed in plaintext.
    env_keys = (
        "LLM_PROXY_URL", "LLM_PROXY_TOKEN",
        "LOCAL_LLM_BASE_URL", "LITELLM_BASE_URL",
        "LOCAL_LLM_API_KEY", "LITELLM_API_KEY",
        "LOCAL_MODEL_REFRESH_SECS", "LOCAL_HIDDEN_MODELS",
        "OPENAI_COMPATIBLE_BASE_URL", "OPENAI_COMPATIBLE_API_KEY",
        "FACTORY_BASE_URL", "FACTORY_API_KEY",
        "SSL_VERIFY", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
    )
    sources["env"] = {k: ("set" if os.getenv(k) else "unset") for k in env_keys}
    sources["env"]["LLM_PROXY_URL_value"] = os.getenv("LLM_PROXY_URL", "")
    sources["env"]["LOCAL_LLM_BASE_URL_value"] = os.getenv("LOCAL_LLM_BASE_URL", "")
    sources["env"]["OPENAI_COMPATIBLE_BASE_URL_value"] = os.getenv("OPENAI_COMPATIBLE_BASE_URL", "")

    return sources


def _filter_user_models(model_ids, user_id: str, department: str) -> set:
    """Thin in-process wrapper around the governance router's shared helper.

    Returns a set for O(1) membership in the caller. Fails open (returns all
    IDs) if the gateway process / DB isn't available — e.g. standalone dev.
    """
    try:
        from routers.model_governance_router import filter_allowed_models
        from db.database import SessionLocal
    except Exception:
        return set(model_ids)

    db = SessionLocal()
    try:
        return set(filter_allowed_models(model_ids, user_id, department, db))
    except Exception as e:
        logger.warning(f'[AGENT] Governance filter failed, returning unrestricted list: {e}')
        return set(model_ids)
    finally:
        try:
            db.close()
        except Exception:
            pass


async def _fallback_openai_compatible_discovery(base_url: str):
    """Legacy upstream /models discovery used only when gateway.get_all_models
    cannot be imported (e.g. ABStudio standalone dev server)."""
    openai_token = openai_compatible_api_key()
    discovery_url = base_url.rstrip("/") + "/models"
    try:
        import httpx
        headers = {"Authorization": f"Bearer {api_key}"}
        _ssl_verify = os.getenv("SSL_VERIFY", "true").lower() == "true"
        if not _ssl_verify:
            logger.warning(
                "SSL_VERIFY is disabled — TLS certificate verification is OFF. "
                "This httpx.AsyncClient is used for document generation."
            )
        _ssl_ca = os.getenv("SSL_CA_BUNDLE")
        _httpx_verify: bool | str = _ssl_ca if _ssl_ca else _ssl_verify
        async with httpx.AsyncClient(verify=_httpx_verify) as client:
            response = await client.get(discovery_url, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
        if isinstance(data, dict):
            items = data.get("data") or data.get("models") or []
        else:
            items = data
        raw_ids = [
            (m.get("id") or m.get("name"))
            for m in items
            if isinstance(m, dict) and (m.get("id") or m.get("name"))
        ]

        def _bare(name: str) -> str:
            return name.split("/", 1)[1] if "/" in name else name

        canonical: dict[str, str] = {}
        for name in raw_ids:
            key = _bare(name).lower()
            existing = canonical.get(key)
            if existing is None or ("/" in existing and "/" not in name):
                canonical[key] = name
        models = sorted(canonical.values())
        return {
            "provider": "custom",
            "base_url_configured": bool(base_url),
            "discovery_url": discovery_url,
            "default_model": None,
            "providers": [{"provider": "Custom", "models": [
                {"id": m, "label": m, "hint": m} for m in models
            ]}] if models else [],
            "models": models,
        }
    except Exception as e:
        return {
            "provider": "custom",
            "base_url_configured": bool(base_url),
            "discovery_url": discovery_url,
            "default_model": None,
            "providers": [],
            "models": [],
            "error": str(e),
        }


# Short, directive prompt — long prompts increase prefill time and tempt
# reasoning models into longer think-blocks before they emit any output.
_INSTRUCTIONS_SYSTEM_PROMPT = (
    "You are a prompt engineer. Convert the user's purpose into a production-ready "
    "system prompt for an AI agent.\n"
    "\n"
    "STRUCTURE (no headings, plain prose paragraphs separated by blank lines):\n"
    "1. Role & identity — open with 'You are ...' and state the agent's role plainly.\n"
    "2. Responsibilities — what the agent does, in 2-4 concrete bullet-style sentences.\n"
    "3. Behaviour — tone, level of detail, how to handle ambiguity, when to ask "
    "clarifying questions, what to refuse.\n"
    "4. Output format — how responses should be structured (length, formatting, "
    "code blocks, citations, etc.).\n"
    "\n"
    "STYLE RULES:\n"
    "- 120-220 words total.\n"
    "- Second person throughout ('You ...').\n"
    "- Imperative, specific, testable — avoid vague words like 'helpful' or 'good'.\n"
    "- No meta-commentary, no 'Here is the prompt', no headings, no numbered list, "
    "no markdown code fences, no surrounding quotes, no <think> blocks.\n"
    "- Output only the prompt body itself."
)


def _build_instructions_llm_config(
    max_tokens: int, temperature: float, top_p: float = 1.0,
):
    """Build an LLMConfig for the "Generate Instructions" meta task, which must
    run against the dedicated factory model, not whatever model happens to be
    configured for runtime agent execution.

    Sourced via factory_model()/factory_base_url()/factory_api_key() — same
    resolution chain as build_meta_llm_config() elsewhere in this file: the
    admin's configured default in core.llm_provider_registry.
    """
    from app.models import LLMConfig, LLMProvider

    model = factory_model()
    base_url = factory_base_url()
    api_key = factory_api_key()
    if not model:
        raise HTTPException(
            status_code=503,
            detail="Generate Instructions is not configured. Add and enable at least "
                   "one model in Admin → LLM Providers.",
        )
    return LLMConfig(
        provider=LLMProvider.CUSTOM,
        api_key=api_key,
        model_name=model,
        temperature=temperature,
        max_tokens=max_tokens,
        top_p=top_p,
        base_url=base_url,
    )


@router.post("/generate-instructions", response_model=GenerateInstructionsResponse)
async def generate_instructions(
    request: GenerateInstructionsRequest,
    current_user: AuthenticatedUser = Depends(require_access),
):
    prompt = (request.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="Prompt is required.")
    try:
        # 500 tokens covers the 120-220 word target plus a small safety margin
        # for tokenisation slack. Temperature 0.35 + top_p 0.9 give the model
        # enough room to vary phrasing without rambling or hallucinating
        # process steps that weren't asked for.
        llm = get_llm_client(_build_instructions_llm_config(
            max_tokens=500, temperature=0.35, top_p=0.9,
        ))
        messages = [
            Message(role="system", content=_INSTRUCTIONS_SYSTEM_PROMPT),
            Message(
                role="user",
                content=(
                    "Write the system prompt for an agent whose purpose is:\n\n"
                    f"{prompt}"
                ),
            ),
        ]
        instructions = clean_llm_text(await llm.complete(messages))
        if not instructions:
            raise HTTPException(
                status_code=502,
                detail="LLM returned an empty response. Try again or rephrase the prompt.",
            )
        return GenerateInstructionsResponse(instructions=instructions)
    except HTTPException:
        raise
    except Exception as e:
        # Don't leak raw exception text to clients; log it instead.
        logger.exception('[AGENT] generate_instructions failed')
        raise HTTPException(status_code=500, detail="Failed to generate instructions.")


def _workflow_system_prompt() -> str:
    agent_provider = "custom"
    agent_model = factory_model()
    agent_base_url = openai_compatible_base_url()
    provider_note = f'Use provider="custom" with baseUrl="{agent_base_url}". Leave apiKey empty.'

    agent_node_example = (
        '{"id":"agent-1","type":"agent","position":{"x":350,"y":300},'
        '"data":{"name":"Role-Based Name",'
        '"instructions":"Sentence 1: role and input. Sentence 2: task. Sentence 3: output format.",'
        f'"provider":"{agent_provider}","apiKey":"",'
        f'"modelName":"{agent_model}","temperature":0.7,"maxTokens":2048,'
        f'"topP":1.0,"baseUrl":"{agent_base_url}",'
        '"skills":[],"tools":[]}}'
    )

    return f"""\
You are an expert multi-agent workflow designer.
Given a description, output a workflow as a strict JSON object - no markdown, no explanation.

OUTPUT FORMAT:
{{
  "name": "Workflow Name",
  "nodes": [...],
  "edges": [...]
}}

NODE TYPES (only these 3):
1. Start node (exactly 1): {{"id":"start-1","type":"start","position":{{"x":100,"y":300}},"data":{{"label":"Start"}}}}
2. Agent node: {agent_node_example}
   {provider_note}
3. End node (exactly 1): {{"id":"end-1","type":"end","position":{{"x":CALCULATED,"y":300}},"data":{{"label":"End"}}}}

LAYOUT: All nodes y=300, x: start=100, each agent +300px, end=100+(node_count*300)
EDGE FORMAT: {{"id":"edge-1","source":"start-1","target":"agent-1","type":"default","style":{{"stroke":"#6366f1","strokeWidth":2}}}}
RULES: Exactly 1 start, exactly 1 end. Role-based names. Every agent has skills[] and tools[]. Output ONLY valid JSON.
"""


@router.post("/generate-workflow", response_model=GenerateWorkflowResponse)
async def generate_workflow(
    request: GenerateWorkflowRequest,
    current_user: AuthenticatedUser = Depends(require_access),
):
    try:
        llm = get_llm_client(build_meta_llm_config(max_tokens=4096, temperature=0.3))
        messages = [
            Message(role="system", content=_workflow_system_prompt()),
            Message(role="user", content=f"Create a workflow for: {request.prompt}"),
        ]
        # Strip <think>…</think> reasoning blocks before parsing JSON so the
        # brace-grep below doesn't accidentally pull braces from inside the
        # chain-of-thought output of reasoning models.
        raw = clean_llm_text(await llm.complete(messages)).strip()
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
        if fence_match:
            raw = fence_match.group(1).strip()
        if not raw.startswith("{"):
            brace_match = re.search(r"\{[\s\S]*\}", raw)
            if brace_match:
                raw = brace_match.group(0)
        data = json.loads(raw)
        name = data.get("name", "Generated Workflow")
        nodes = data.get("nodes", [])
        edges = data.get("edges", [])
        node_types = [n.get("type") for n in nodes]
        if "start" not in node_types:
            raise ValueError("LLM did not include a start node")
        if "end" not in node_types:
            raise ValueError("LLM did not include an end node")
        return GenerateWorkflowResponse(name=name, graph_data={"nodes": nodes, "edges": edges})
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"LLM returned invalid JSON: {e}")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
