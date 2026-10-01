# SPDX-License-Identifier: MIT
# ============================================================
# Gap #5 — context-size as a first-class routing dimension
# ============================================================
#
# Frontier pattern #5 (docs/architecture/02 §2.5/§2.8): when a turn's token
# footprint won't fit, the router must not truncate.
#
# The requested tier is UNCHANGED and its own candidates are filtered by
# capabilities.context_window (§M.2): the window is a property of the MODEL.
# The pre-Phase-5 tier-promotion ladder went with TIER_GOVERNANCE_ENABLED in
# Phase 8, and its eight tests with it.
# ============================================================

import pytest

import core.llm_provider_registry as reg
import core.tier_resolver as tr
from core.tiers import Tier
from models.model_router import ModelRouter


# ── Context size as a capability filter (§M.2) ──────────────────────────────


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


def test_an_unsatisfiable_window_is_reported(governed):
    """Not a compliance failure, but no longer routed around either (D107):
    the error names the model and the window it lacks."""
    models, assign = governed
    models.append(_model("tiny", 4_000))
    assign(Tier.COMPLEX, "tiny")
    with pytest.raises(tr.NoEligibleModel):
        ModelRouter().route("q", model_hint="complex", context_tokens=400_000)
