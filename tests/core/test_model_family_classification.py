# SPDX-License-Identifier: MIT
# ============================================================
# Provider-family classification and its two safety-critical consumers.
#
# These pin down the Phase 0 fixes from
# docs/llm/model_agnostic_architecture_audit.md. All three defects shared one
# root cause: provider/family was inferred by parsing the model's NAME, which
# stopped being sound the moment admins could register arbitrary models.
#
#   1. services.endpoint_model_catalog.family_of() — resolves `family` from the
#      DB provider registry, falling back to name heuristics only offline.
#   2. middleware.budget_middleware._is_inhouse_model() — was a seven-entry
#      cloud-prefix deny-list with "not one of these ⇒ free ⇒ skip budget",
#      which exempted every paid OpenRouter/Bedrock/Mistral/DeepSeek id from
#      budget enforcement entirely. Must now FAIL CLOSED.
#   3. endpoint_model_catalog.proxy_provider_for() — four call sites hardcoded
#      provider="claude" beside a dynamically resolved model, so repointing a
#      tier at another vendor sent a mismatched pair to /llm/generate.
#
# The registry is stubbed throughout: these are unit tests of the
# classification logic, not of Postgres.
# ============================================================

from __future__ import annotations

import pytest

from services import endpoint_model_catalog as emc


# ── family_of: registry is authoritative ─────────────────────────────────────

@pytest.mark.parametrize(
    "registry_family, expected",
    [
        ("anthropic",         "anthropic"),
        ("openai",            "openai"),
        ("gemini",            "gemini"),
        ("openai_compatible", "openai_compatible"),
        ("ollama",            "local"),   # callers act on "local", not "ollama"
    ],
)
def test_family_of_prefers_the_registry_row(
    registry_family: str, expected: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a model whose NAME says nothing about its vendor, but which has a
    # registry row. This is the shape every admin-registered model takes.
    import core.llm_provider_registry as reg
    monkeypatch.setattr(
        reg, "get_model",
        lambda mid: {"family": registry_family, "model_id": mid, "capabilities": {}},
    )

    # When/Then: the registry's family wins over any name parsing.
    assert emc.family_of("some-internal-deployment-name") == expected


def test_family_of_resolves_openrouter_id_the_prefix_heuristic_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the exact id that broke ABStudio — "anthropic/..." does not start
    # with "claude", so the old prefix chain fell through to "local" and sent it
    # to the in-house LiteLLM endpoint, which 404s.
    import core.llm_provider_registry as reg
    monkeypatch.setattr(
        reg, "get_model",
        lambda mid: {"family": "openai_compatible", "model_id": mid, "capabilities": {}},
    )

    assert emc.family_of("anthropic/claude-sonnet-4-6") == "openai_compatible"


def test_family_of_returns_unknown_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: no registry row, not in the local catalog, not a known cloud id.
    import core.llm_provider_registry as reg
    monkeypatch.setattr(reg, "get_model", lambda mid: None)
    monkeypatch.setattr(emc, "classify_model", lambda m: "unknown")
    monkeypatch.setattr(emc, "provider_of", lambda m: "unknown")

    # Then: "unknown", never a guessed vendor. Callers must handle it.
    assert emc.family_of("mystery-model-v3") == "unknown"


def test_family_of_handles_empty_input() -> None:
    assert emc.family_of("") == "unknown"
    assert emc.family_of(None) == "unknown"   # type: ignore[arg-type]


def test_family_of_survives_an_unreachable_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the registry raises (Redis/Postgres down).
    import core.llm_provider_registry as reg

    def _boom(mid):
        raise RuntimeError("registry down")

    monkeypatch.setattr(reg, "get_model", _boom)
    monkeypatch.setattr(emc, "classify_model", lambda m: "unknown")
    monkeypatch.setattr(emc, "provider_of", lambda m: "claude")

    # Then: it degrades to the name heuristics instead of propagating.
    assert emc.family_of("claude-sonnet-4-6") == "anthropic"


# ── proxy_provider_for: only the three built-in gateways ─────────────────────

@pytest.mark.parametrize(
    "family, expected",
    [
        ("anthropic",         "claude"),
        ("openai",            "openai"),
        ("gemini",            "gemini"),
        # An openai_compatible model needs its provider row's own base_url,
        # which /llm/generate's three-way provider switch cannot supply. A
        # local model is not served by the proxy at all.
        ("openai_compatible", None),
        ("local",             None),
        ("unknown",           None),
    ],
)
def test_proxy_provider_for_refuses_families_no_gateway_can_serve(
    family: str, expected, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(emc, "family_of", lambda m: family)
    assert emc.proxy_provider_for("any-model") == expected


# ── budget enforcement must fail closed ──────────────────────────────────────

# Paid cloud models that the old seven-prefix deny-list declared "in-house" and
# therefore exempted from budget enforcement completely. Each of these is a real
# id an admin can register through the openai_compatible family today.
_WRONGLY_EXEMPTED = [
    "mistralai/mixtral-8x7b-instruct",
    "meta-llama/llama-3.1-70b-instruct",
    "deepseek-chat",
    "o3-mini",
    "us.anthropic.claude-sonnet-4-6-v1:0",      # Bedrock
    "claude-3-5-sonnet@20240620",               # Vertex
    "grok-4",
    "complex",                                  # a router tier hint
    "haiku",
]


@pytest.mark.parametrize("model", _WRONGLY_EXEMPTED)
def test_paid_and_unknown_models_are_budget_checked(
    model: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from middleware import budget_middleware as bm

    # Given: the catalog cannot place the model (no registry row, not local).
    monkeypatch.setattr(emc, "classify_model", lambda m: "unknown")

    # Then: NOT in-house ⇒ the budget gate runs. This is the fix: the old
    # prefix deny-list returned True here and skipped enforcement entirely.
    assert bm._is_inhouse_model(model) is False


@pytest.mark.parametrize(
    "model", ["local", "local:glm-5.2", "kimi-k2.6", "gemma-4-31B-it"],
)
def test_genuinely_local_models_stay_exempt(
    model: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from middleware import budget_middleware as bm

    monkeypatch.setattr(emc, "classify_model", lambda m: "local")
    assert bm._is_inhouse_model(model) is True


def test_model_id_case_is_preserved_for_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from middleware import budget_middleware as bm

    # Real in-house ids carry capitals ("gemma-4-31B-it", "qwen-3.6-35B-A3B")
    # and both halves of classify_model match exactly against a catalogue.
    # Lower-casing the id first made those miss the local catalogue and fall
    # through to "unknown", newly enforcing a budget on a free in-house model.
    seen: list[str] = []
    monkeypatch.setattr(emc, "classify_model", lambda m: seen.append(m) or "local")

    assert bm._is_inhouse_model("  gemma-4-31B-it  ") is True
    assert seen == ["gemma-4-31B-it"]   # stripped, but NOT lower-cased


def test_local_model_request_reads_model_then_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json as _json

    from middleware import budget_middleware as bm

    seen: list[str] = []
    monkeypatch.setattr(emc, "classify_model", lambda m: seen.append(m) or "local")

    assert bm._is_local_model_request(_json.dumps({"model": "qwen-3.6-35B-A3B"}).encode()) is True
    assert bm._is_local_model_request(_json.dumps({"hint": "local:glm-5.2"}).encode()) is True
    assert seen == ["qwen-3.6-35B-A3B", "local:glm-5.2"]

    # A malformed body must not raise out of the middleware.
    assert bm._is_local_model_request(b"not json") is False


@pytest.mark.parametrize("hint", ["", "auto", "default", "  ", None])
def test_auto_hints_are_budget_checked(hint) -> None:
    from middleware import budget_middleware as bm

    # An unresolved hint may route to a paid model, so it must not be exempt.
    # Checked before the catalog is consulted, so no stubbing is needed.
    assert bm._is_inhouse_model(hint) is False


def test_budget_fails_closed_when_the_catalog_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from middleware import budget_middleware as bm

    def _boom(m):
        raise RuntimeError("registry down")

    monkeypatch.setattr(emc, "classify_model", _boom)

    # Then: treated as NOT in-house, so the budget is enforced. Failing open
    # here would hand out free cloud inference during an outage.
    assert bm._is_inhouse_model("mistralai/mixtral-8x7b-instruct") is False


# ── ndjson parsing ───────────────────────────────────────────────────────────

def test_ndjson_helper_concatenates_token_chunks() -> None:
    from core.proxy_tool_use import llm_proxy_ndjson_text

    # /llm/generate's real wire format: one JSON object per line. Calling
    # resp.json() on this raised JSONDecodeError, which is why
    # feedback_processor and hybrid_retriever's decomposition always returned
    # their empty fallback.
    body = (
        '{"t": "Be "}\n'
        '{"t": "more "}\n'
        '{"t": "concise."}\n'
        '{"m": {"in": 12, "out": 4, "model": "claude-haiku-4-5"}}\n'
    )
    assert llm_proxy_ndjson_text(body) == "Be more concise."


def test_ndjson_helper_skips_unparseable_and_empty_lines() -> None:
    from core.proxy_tool_use import llm_proxy_ndjson_text

    # A truncated final chunk must not discard text that already arrived.
    assert llm_proxy_ndjson_text('{"t": "kept"}\n\nnot json\n{"t": "!"}\n{"t":') == "kept!"
    assert llm_proxy_ndjson_text("") == ""
    assert llm_proxy_ndjson_text(None) == ""   # type: ignore[arg-type]
