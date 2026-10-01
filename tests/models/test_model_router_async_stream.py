# SPDX-License-Identifier: MIT
"""The async web-chat stream reports the model that ANSWERED, not the route.

Phase 8 moved this onto the governed path: the medium tier's head candidate's
provider is down and the next candidate answers (§M.5). What is asserted is
unchanged:

  * the expected model is taken from the gateway that actually served the
    call, instead of from ``models.model_router.CLAUDE_PRIMARY_MODEL``. That
    constant is one of the .env model constants Phase 8 deletes, and a test
    that imports it would have to be rewritten then. More to the point, the
    invariant here is a RELATIONSHIP — "the metadata names whoever answered" —
    and comparing against a constant only tests it by coincidence, on a
    deployment where that constant happens to be what Claude is configured as.

The failure this guards is silent: when OpenAI is unavailable the answer still
arrives, so nothing looks wrong, but every usage row, cost attribution and
"which model said this" trace would name the provider that was *asked* rather
than the one that *replied*.
"""

from collections.abc import AsyncIterator, Iterator

import anyio
import pytest

from core.tier_resolver import ResolvedModel
from core.tiers import Tier
from models.model_router import ModelRouter, tier_request


def _cand(model_id: str, family: str, priority: int) -> ResolvedModel:
    return ResolvedModel(model_id=model_id, row_id=model_id, provider_id="p",
                         provider_slug=family, family=family, base_url=None,
                         capabilities={}, tier=Tier.MEDIUM,
                         requested_tier=Tier.MEDIUM, priority=priority)


class _ClaudeGateway:
    """Records the model it was asked for, so the assertion has a source of
    truth that is not an environment constant."""

    def __init__(self) -> None:
        self.served_model: str | None = None

    def generate(self, prompt: str, *, model: str) -> Iterator[str]:
        del prompt
        self.served_model = model
        yield "ROUTING_OK"


async def _collect_stream(router: ModelRouter) -> list[str | dict[str, dict[str, str | int]]]:
    stream: AsyncIterator[str | dict[str, dict[str, str | int]]] = router.async_stream(
        "hello",
        **tier_request(Tier.MEDIUM, "medium"),
    )
    return [chunk async for chunk in stream]


def test_async_stream_reports_actual_claude_fallback_when_openai_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: Auto asked for the medium tier, but only Claude is available.
    router = ModelRouter()
    claude = _ClaudeGateway()
    monkeypatch.setattr(
        "core.tier_resolver.resolve_tier_candidates",
        lambda tier, c=None, **kw: [_cand("openai-head", "openai", 1),
                                    _cand("claude-next", "anthropic", 2)])
    monkeypatch.setattr(ModelRouter, "_gateway_for_registry_family",
                        lambda self, family, model_id: claude if family == "anthropic" else None)

    # When: the async web-chat stream falls back through its sync worker thread.
    chunks = anyio.run(_collect_stream, router)
    stream_meta = next(
        chunk["__stream_meta__"]
        for chunk in chunks
        if isinstance(chunk, dict) and "__stream_meta__" in chunk
    )

    # Then: metadata identifies the provider that produced the answer, not the failed route.
    assert "".join(chunk for chunk in chunks if isinstance(chunk, str)) == "ROUTING_OK"
    assert claude.served_model, "the Claude gateway was never asked for a model"
    assert stream_meta["model_id"] == claude.served_model
    # A later candidate in the same tier is not a ladder fallback (§M.5), so no suffix.
    assert stream_meta["model_label"] == "claude-next"
