# SPDX-License-Identifier: MIT
# ============================================================
# Gap #5 — context-size as a first-class routing dimension
# ============================================================
#
# Frontier pattern #5 (docs/architecture/02 §2.5/§2.8): when a turn's token
# footprint won't fit, the router must not truncate.
#
# ── Phase 5 split this into two mechanisms ──────────────────────────────────
#
#   TIER_GOVERNANCE_ENABLED off — the TIER is replaced by a larger-window one,
#     from the fixed _CONTEXT_PROMOTION_LADDER. Both target tiers are
#     provider-shaped ("deep" is GPT, "gemini" is Gemini), which is why §M.2
#     replaces the mechanism. Asserted unchanged in the first half below.
#   TIER_GOVERNANCE_ENABLED on  — the tier is UNCHANGED and the requested
#     tier's own candidates are filtered by capabilities.context_window
#     (§M.2). The window is a property of the MODEL, and a per-tier constant
#     cannot be right for a tier that has two models of different sizes.
#
# The ladder survives Phase 5 rather than being deleted as §M.2 asks, because
# deleting it would change routing for every deployment running with the flag
# off — in the release whose safety argument is that turning the flag off
# changes nothing. It goes in Phase 10 with the rest of the legacy chain.
# ============================================================

import pytest

import core.llm_provider_registry as reg
import core.tier_resolver as tr
from core.tiers import Tier
from models.model_router import (
    _promote_for_context,
    _tier_window,
    ModelRouter,
    TIER_SIMPLE,
)


@pytest.fixture(autouse=True)
def _governance_off(monkeypatch):
    """Pin the flag: the promotion assertions describe the flag-OFF path, and
    a developer whose .env switches it on would otherwise see them fail."""
    monkeypatch.delenv("TIER_GOVERNANCE_ENABLED", raising=False)


def test_small_context_does_not_promote():
    assert _promote_for_context("medium", 5_000) == "medium"


def test_medium_overflow_promotes_to_deep():
    # 150K tokens / 0.8 headroom = 187.5K needed; medium=128K can't fit → deep=256K
    assert _promote_for_context("medium", 150_000) == "deep"


def test_deep_overflow_promotes_to_gemini():
    # 250K / 0.8 = 312.5K needed; deep=256K can't fit → gemini=1M
    assert _promote_for_context("medium", 250_000) == "gemini"
    assert _promote_for_context("complex", 250_000) == "gemini"


def test_already_large_window_unchanged():
    assert _promote_for_context("gemini", 500_000) == "gemini"


def test_beyond_all_windows_picks_largest():
    assert _promote_for_context("simple", 5_000_000) == "gemini"


def test_zero_or_negative_tokens_unchanged():
    assert _promote_for_context("medium", 0) == "medium"
    assert _promote_for_context("medium", -1) == "medium"


def test_tier_window_defaults_safely():
    assert _tier_window("unknown_tier") == 128_000


def test_privacy_floor_still_wins_over_context_size():
    # A restricted turn must stay local even if the context is huge — privacy
    # is enforced before context-size routing.
    r = ModelRouter()
    d = r.route("x" * 2_000_000, data_classification="RESTRICTED")
    assert d.tier == TIER_SIMPLE


# ── The same concern, expressed as a capability filter (Phase 5, §M.2) ──────


def _model(row_id, window):
    return {
        "id": row_id, "model_id": f"model-{row_id}", "display_name": row_id,
        "capabilities": {"modality": ["text"], "privacy_class": "external",
                         "context_window": window},
        "is_default": False, "sort_order": 0, "provider_id": "p",
        "provider_slug": "anthropic", "provider_name": "anthropic",
        "family": "anthropic", "base_url": None,
    }


@pytest.fixture
def governed(monkeypatch):
    monkeypatch.setenv("TIER_GOVERNANCE_ENABLED", "true")
    models: list = []
    assignments: list = []
    monkeypatch.setattr(reg, "get_enabled_models", lambda channel=None: list(models))
    monkeypatch.setattr(tr, "get_tier_assignments", lambda: list(assignments))
    monkeypatch.setattr(tr, "_breaker_is_open", lambda slug, mid: False)

    def assign(tier, row_id, priority=100):
        assignments.append({"tier": tier.value, "model_row_id": row_id,
                            "priority": priority, "role": None, "org_id": "default"})
    return models, assign


def test_a_large_context_never_changes_the_requested_tier(governed):
    models, assign = governed
    models.append(_model("narrow", 8_000))
    models.append(_model("wide", 1_000_000))
    assign(Tier.COMPLEX, "narrow", priority=1)
    assign(Tier.COMPLEX, "wide", priority=2)

    d = ModelRouter().route("q", model_hint="complex", context_tokens=400_000)
    assert d.requested_tier is Tier.COMPLEX
    assert d.provider_model_override == "model-wide"


def test_headroom_is_applied_the_same_way_the_ladder_applied_it(governed):
    """CONTEXT_FIT_FRACTION is retained as a tuning knob, so 0.8 headroom
    means a 100K-token turn needs a 125K window, not a 100K one."""
    models, assign = governed
    models.append(_model("exactly-100k", 100_000))
    models.append(_model("roomy", 200_000))
    assign(Tier.COMPLEX, "exactly-100k", priority=1)
    assign(Tier.COMPLEX, "roomy", priority=2)
    d = ModelRouter().route("q", model_hint="complex", context_tokens=100_000)
    assert d.provider_model_override == "model-roomy"


def test_a_model_with_no_declared_window_is_skipped_not_assumed(governed):
    """Fail safe: an unknown window cannot be ASSERTED to be big enough.

    The undeclared model holds priority 1, so a resolver that treated "we do
    not know" as "large enough" would pick it and the turn would be truncated
    at dispatch. It must be passed over for the model that actually declares
    a window it can meet.
    """
    models, assign = governed
    unknown = _model("unknown", None)
    unknown["capabilities"].pop("context_window")
    models.append(unknown)
    models.append(_model("declared", 1_000_000))
    assign(Tier.COMPLEX, "unknown", priority=1)
    assign(Tier.COMPLEX, "declared", priority=2)

    d = ModelRouter().route("q", model_hint="complex", context_tokens=400_000)
    assert d.provider_model_override == "model-declared"


def test_an_unsatisfiable_window_falls_back_rather_than_failing(governed):
    """Unlike no_cloud_egress, a context constraint that cannot be met is not
    a compliance failure — it degrades to the .env constants with a warning,
    which is what every text tier does when it resolves to nothing."""
    models, assign = governed
    models.append(_model("tiny", 4_000))
    assign(Tier.COMPLEX, "tiny")
    d = ModelRouter().route("q", model_hint="complex", context_tokens=400_000)
    assert d.resolved is None
