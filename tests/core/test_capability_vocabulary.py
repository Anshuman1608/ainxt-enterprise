# SPDX-License-Identifier: MIT
# ============================================================
# Provider-neutral capability vocabulary — behaviour-preservation gate.
#
# models/model_router.py's tier names are vendor product names ("haiku",
# "sonnet-5", "opus-5", "gemini", "tera", "luna") or complexity labels whose
# vendor is implicit ("complex" is Anthropic, "medium" is OpenAI). A per-feature
# admin dropdown cannot be expressed in those, so six provider-neutral names
# were added: fast / balanced / expert / vision / long-context / local-only.
#
# The change is STRICTLY ADDITIVE, and this module is the gate that proves it.
# The original design for this vocabulary proposed folding legacy names into
# the new ones ("simple" -> "fast", "medium" AND "complex" -> "balanced"). That
# is not behaviour-preserving, which is the whole point of the snapshot below:
#
#   * "simple" dispatches to the IN-HOUSE GPU, not a cheap cloud model, so
#     folding it into "fast" would move 17 call sites off-prem.
#   * "medium" (OpenAI) and "complex" (Anthropic) are different VENDORS, not
#     different capability levels, so folding both into "balanced" would
#     silently switch vendor for 39 call sites.
#
# If _LEGACY_HINT_TIERS ever needs editing to make this file pass, the change
# under review is not alias-only and needs re-thinking, not a new snapshot.
# ============================================================

from __future__ import annotations

import pytest

from models import model_router as mr


# Every statically-declared _HINT_MAP key as it resolved BEFORE the capability
# names were introduced. Keys built from env vars (OPENAI_CODING_MODEL and
# friends) are excluded because they are blank in a bare test environment and
# are covered separately below.
_LEGACY_HINT_TIERS = {
    "simple":                 "simple",
    "local":                  "simple",
    "mini":                   "mini",
    "gpt-mini":               "mini",
    "gpt-5-mini":             "mini",
    "local_mini":             "local_mini",
    "gpt-oss":                "local_mini",
    "gpt-oss-120b":           "local_mini",
    "medium":                 "medium",
    "coding":                 "medium",
    "agents":                 "medium",
    "gpt":                    "medium",
    "gpt-5.4":                "medium",
    "complex":                "complex",
    "sonnet":                 "complex",
    "claude":                 "complex",
    "haiku":                  "haiku",
    "vision":                 "vision",
    "gemini":                 "gemini",
    "gemini-2.5-flash":       "gemini",
    "gemini-2.0-flash":       "gemini",
    "gemini-3.5-flash":       "gemini",
    "gemini-3.1-flash-lite":  "gemini",
    "gemini-3.1-flash-image": "vision",
    "solution":               "solution",
    "opus":                   "solution",
    "opus-4-8":               "opus-4-8",
    "claude-opus-4-8":        "opus-4-8",
    "opus-5":                 "opus-5",
    "claude-opus-5":          "opus-5",
    "sonnet-5":               "sonnet-5",
    "claude-sonnet-5":        "sonnet-5",
    "deep":                   "deep",
    "gpt-5-5":                "deep",
    "tera":                   "tera",
    "gpt-5.6-terra":          "tera",
    "luna":                   "luna",
    "gpt-5.6-luna":           "luna",
}

# The six provider-neutral names and the tier each resolves to.
_CAPABILITY_TIERS = {
    "fast":         "haiku",
    "balanced":     "complex",
    "expert":       "solution",
    "vision":       "vision",        # already a capability name; pre-existing
    "long-context": "tera",
    "local-only":   "simple",
}


# ── the snapshot ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hint, tier", sorted(_LEGACY_HINT_TIERS.items()))
def test_every_legacy_hint_resolves_to_the_same_tier_as_before(
    hint: str, tier: str,
) -> None:
    assert mr._HINT_MAP[hint] == tier


def test_no_legacy_hint_was_dropped() -> None:
    missing = sorted(set(_LEGACY_HINT_TIERS) - set(mr._HINT_MAP))
    assert not missing, f"capability work removed live hint keys: {missing}"


def test_the_only_new_keys_are_the_capability_names() -> None:
    # Guards against an unrelated hint sneaking in under this banner. Env-var
    # keys are blank (and so stripped) in a bare test env, hence the filter.
    static_keys = {k for k in mr._HINT_MAP if k}
    unexpected = static_keys - set(_LEGACY_HINT_TIERS) - set(_CAPABILITY_TIERS)
    assert not unexpected, f"unexpected new _HINT_MAP keys: {sorted(unexpected)}"


# ── the new vocabulary ───────────────────────────────────────────────────────

@pytest.mark.parametrize("capability, tier", sorted(_CAPABILITY_TIERS.items()))
def test_capability_names_resolve(capability: str, tier: str) -> None:
    assert mr._HINT_MAP[capability] == tier


def test_local_only_pins_to_the_in_house_tier_not_a_cheap_cloud_one() -> None:
    # The trap this vocabulary was nearly built on. TIER_SIMPLE is the in-house
    # GPU path (_dispatch -> _try_local_simple); TIER_HAIKU/TIER_MINI are cloud.
    assert mr._HINT_MAP["local-only"] == mr.TIER_SIMPLE
    assert mr._HINT_MAP["simple"] == mr.TIER_SIMPLE
    assert mr._HINT_MAP["fast"] != mr.TIER_SIMPLE


def test_medium_and_complex_remain_distinct_tiers() -> None:
    # They are different vendors (OpenAI vs Anthropic), so no single capability
    # name may collapse them.
    assert mr._HINT_MAP["medium"] != mr._HINT_MAP["complex"]
    assert mr._HINT_MAP["balanced"] == mr._HINT_MAP["complex"]


def test_deep_was_not_repointed_to_the_opus_tier() -> None:
    # "deep" was already live as TIER_DEEP (GPT-5-5). The high-capability
    # capability is therefore named "expert", not "deep".
    assert mr._HINT_MAP["deep"] == mr.TIER_DEEP
    assert mr._HINT_MAP["expert"] == mr.TIER_SOLUTION
    assert mr.TIER_DEEP != mr.TIER_SOLUTION


def test_retired_opus_46_is_not_aliased() -> None:
    # It looks like a missing alias, but claude-opus-4-6 is retired and sits in
    # BLOCKED_MODELS — an alias would point callers at a blocked model.
    from core.model_registry import BLOCKED_MODELS

    assert "opus-4-6" not in mr._HINT_MAP
    assert "claude-opus-4-6" in BLOCKED_MODELS


# ── the other two vocabularies converge on the same names ────────────────────

@pytest.mark.parametrize("capability", sorted(_CAPABILITY_TIERS))
def test_ainxt_tier_map_accepts_every_capability_name(capability: str) -> None:
    from core.config import AINXT_TIER_MAP

    # Values are env-driven and blank in a bare checkout, so assert membership
    # rather than a concrete model id — a missing KEY is the real defect (the
    # caller would fall back to a default model silently).
    assert capability in AINXT_TIER_MAP


@pytest.mark.parametrize("capability", sorted(_CAPABILITY_TIERS))
def test_sdlc_treats_every_capability_name_as_a_tier_not_a_model_id(
    capability: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.model_registry import _SDLC_STAGE_HINTS_ALLOWED, sdlc_stage_hint

    assert capability in _SDLC_STAGE_HINTS_ALLOWED

    # sdlc_stage_hint returns a non-tier env value VERBATIM as a concrete model
    # id. Membership above is what stops "balanced" being handed to the router
    # as a model to call.
    monkeypatch.setenv("SDLC_MODEL_CODER", capability)
    monkeypatch.setenv("ENABLE_OPUS", "true")
    assert sdlc_stage_hint("coder") == capability


def test_expert_honours_the_enable_opus_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.model_registry import sdlc_stage_hint

    # "expert" and "solution" resolve to the same Opus-backed tier, so the
    # kill-switch must downgrade both — otherwise it is bypassable by naming
    # the capability instead of the tier.
    monkeypatch.setenv("ENABLE_OPUS", "false")
    monkeypatch.setenv("SDLC_MODEL_CODER", "expert")
    assert sdlc_stage_hint("coder") == "complex"
    monkeypatch.setenv("SDLC_MODEL_CODER", "solution")
    assert sdlc_stage_hint("coder") == "complex"


@pytest.fixture
def offline_registry(monkeypatch: pytest.MonkeyPatch):
    """Keep cli_model_for_tier off the network.

    Every role constant it reads (CLAUDE_PRIMARY_MODEL, OPENAI_CODING_MODEL, ...)
    is blank in a bare checkout, which sends _role_model() to
    core.llm_provider_registry.get_enabled_models() — a Redis + Postgres hop
    whose connect timeouts made these cases take ~8s each. Stub it with one
    registry row per family so the resolution stays a pure unit test.
    """
    import core.llm_provider_registry as reg
    import core.model_registry as cmr

    rows = [
        {"model_id": f"stub-{fam}", "family": fam, "capabilities": {}}
        for fam in ("anthropic", "openai", "gemini")
    ]
    monkeypatch.setattr(reg, "get_enabled_models", lambda channel=None: rows)
    monkeypatch.setattr(cmr, "is_local_only", lambda: False)
    return cmr


@pytest.mark.parametrize("capability", sorted(_CAPABILITY_TIERS))
def test_cli_model_for_tier_never_returns_a_capability_name_as_a_model_id(
    capability: str, offline_registry,
) -> None:
    # Unknown hints are returned verbatim on the assumption they are concrete
    # model ids, so an unmapped capability name would be dispatched as a model
    # called "balanced".
    assert offline_registry.cli_model_for_tier(capability) != capability


def test_cli_model_for_tier_keeps_local_only_off_the_cloud(
    offline_registry, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(offline_registry, "LOCAL_LLM_MODEL_NAME", "glm-5.2-fp8")

    # "local-only" is a posture, not a role: it must resolve to the locally
    # served model even when the deployment is not in local-only mode.
    assert offline_registry.cli_model_for_tier("local-only") == "glm-5.2-fp8"


def test_fast_and_balanced_keep_their_shadowed_tiers_env_override(
    offline_registry, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each capability is aliased onto an existing role so it keeps honouring
    # that role's SDLC_TIER_<TIER>_MODEL override, rather than needing its own.
    # "fast" shadows the "haiku" role, whose operator-facing override is
    # SDLC_TIER_SIMPLE_MODEL (see cli_model_for_tier's comment).
    monkeypatch.setenv("SDLC_TIER_SIMPLE_MODEL", "my-own-cheap-model")
    monkeypatch.setenv("SDLC_TIER_COMPLEX_MODEL", "my-own-workhorse")

    assert offline_registry.cli_model_for_tier("fast") == "my-own-cheap-model"
    assert offline_registry.cli_model_for_tier("haiku") == "my-own-cheap-model"
    assert offline_registry.cli_model_for_tier("balanced") == "my-own-workhorse"
    assert offline_registry.cli_model_for_tier("complex") == "my-own-workhorse"
