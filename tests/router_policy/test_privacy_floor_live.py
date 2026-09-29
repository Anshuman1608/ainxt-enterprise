# SPDX-License-Identifier: MIT
# ============================================================
# Phase 4 — LIVE model-router privacy-floor enforcement
# ============================================================
#
# The pure decision function (profiles/routing.py) is covered by
# tests/profiles/test_routing.py. THIS suite covers the *live* enforcement point
# wired into models.model_router.ModelRouter.route()/generate() — the hard
# enterprise invariant that CONFIDENTIAL+ data never egresses to a cloud
# provider, even when the caller passed an explicit cloud model_hint.
#
# ── Phase 5: the same invariant, now stated twice ───────────────────────────
#
# The MECHANISM changed and the GUARANTEE did not, which is the only reason
# this file was rewritten rather than replaced:
#
#   TIER_GOVERNANCE_ENABLED off — the tier is rewritten to TIER_SIMPLE, so a
#     hard reasoning task on confidential data runs on the smallest local
#     model. Historical behaviour, asserted unchanged below.
#   TIER_GOVERNANCE_ENABLED on  — the tier is UNCHANGED and the candidate set
#     narrows to capabilities.privacy_class == deployment_local (§M.1).
#     Capability and policy stop being the same axis.
#
# Under governance the resolver additionally refuses to walk the fallback
# ladder, so a deployment with no local model FAILS rather than degrading onto
# a weaker tier that might be external. That half is asserted here and again,
# from the routing side, in tests/models/test_tier_switchover.py — a
# compliance invariant is worth stating in both places it can break.
# ============================================================

import pytest

import core.llm_provider_registry as reg
import core.tier_resolver as tr
from core.tier_resolver import NoEligibleModel
from core.tiers import Tier
from models.model_router import (
    ModelRouter,
    TIER_SIMPLE,
    _privacy_requires_local,
    classification_from_policy,
)


@pytest.fixture(autouse=True)
def _governance_off(monkeypatch):
    """Pin the flag for every test in this file that does not set it itself.

    Without this the historical assertions below would depend on the
    developer's .env: a machine with governance switched on would see the
    tier NOT rewritten to TIER_SIMPLE and the suite would fail for the right
    reason at the wrong time.
    """
    monkeypatch.delenv("TIER_GOVERNANCE_ENABLED", raising=False)


@pytest.mark.parametrize("cls,expected", [
    ("RESTRICTED", True),
    ("restricted", True),
    ("PCI_SENSITIVE", True),
    ("CONFIDENTIAL", True),
    ("  Restricted  ", True),
    ("INTERNAL", False),
    ("PUBLIC", False),
    (None, False),
    ("", False),
    ("nonsense", False),
])
def test_privacy_ladder(cls, expected):
    assert _privacy_requires_local(cls) is expected


def test_restricted_forces_local_even_with_cloud_hint():
    """THE invariant: restricted data pins to local, overriding an explicit hint."""
    r = ModelRouter()
    d = r.route("some restricted content", model_hint="opus",
                data_classification="RESTRICTED")
    assert d.tier == TIER_SIMPLE, "restricted data must be pinned to the local tier"


def test_confidential_forces_local():
    r = ModelRouter()
    d = r.route("confidential text", model_hint="claude",
                data_classification="CONFIDENTIAL")
    assert d.tier == TIER_SIMPLE


def test_public_is_not_forced_local():
    """Public/internal traffic keeps normal routing — floor must not over-restrict."""
    r = ModelRouter()
    d = r.route("just a general question", model_hint="opus",
                data_classification="PUBLIC")
    assert d.tier != TIER_SIMPLE


def test_no_classification_is_unchanged():
    r = ModelRouter()
    d_plain = r.route("hello", model_hint="opus")
    d_none = r.route("hello", model_hint="opus", data_classification=None)
    assert d_plain.tier == d_none.tier


class _FakeRouting:
    def __init__(self, floor):
        self.privacy_floor = floor


class _FakePolicy:
    def __init__(self, floor):
        self.routing = _FakeRouting(floor)


@pytest.mark.parametrize("floor,expected", [
    ("restricted", "RESTRICTED"),
    ("confidential", "CONFIDENTIAL"),
    ("internal", None),
    ("public", None),
    (None, None),
])
def test_classification_from_policy(floor, expected):
    assert classification_from_policy(_FakePolicy(floor)) == expected


def test_classification_from_policy_handles_none():
    assert classification_from_policy(None) is None


# ── The same guarantee, expressed as a constraint (Phase 5, §M.1) ───────────


def _model(row_id, *, family, privacy_class):
    return {
        "id": row_id, "model_id": f"model-{row_id}", "display_name": row_id,
        "capabilities": {"modality": ["text"], "privacy_class": privacy_class},
        "is_default": False, "sort_order": 0, "provider_id": f"p-{family}",
        "provider_slug": family, "provider_name": family, "family": family,
        "base_url": None,
    }


@pytest.fixture
def governed(monkeypatch):
    """Governance ON, over an in-memory registry and tier table."""
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


def test_restricted_selects_a_deployment_local_model_without_changing_the_tier(governed):
    models, assign = governed
    models.append(_model("cloud", family="anthropic", privacy_class="external"))
    models.append(_model("onprem", family="ollama", privacy_class="deployment_local"))
    assign(Tier.COMPLEX, "cloud", priority=1)
    assign(Tier.COMPLEX, "onprem", priority=2)

    d = ModelRouter().route("restricted content", model_hint="complex",
                            data_classification="RESTRICTED")
    assert d.requested_tier is Tier.COMPLEX, "the capability asked for must survive"
    assert d.provider_model_override == "model-onprem"


def test_a_model_with_no_privacy_metadata_is_not_treated_as_local(governed):
    """Fail safe. Absent metadata means "we cannot prove this is safe", which
    is not the same as "this is safe" — see the resolver's module docstring."""
    models, assign = governed
    models.append(_model("unknown", family="openai_compatible", privacy_class=None))
    assign(Tier.COMPLEX, "unknown")
    with pytest.raises(NoEligibleModel):
        ModelRouter().route("q", model_hint="complex", data_classification="RESTRICTED")


def test_a_cloud_only_deployment_fails_rather_than_egressing(governed):
    models, assign = governed
    models.append(_model("cloud", family="anthropic", privacy_class="external"))
    assign(Tier.COMPLEX, "cloud")
    with pytest.raises(NoEligibleModel):
        ModelRouter().route("q", model_hint="complex", data_classification="CONFIDENTIAL")


def test_the_ladder_is_never_walked_across_the_privacy_boundary(governed):
    """complex → medium is the ladder. A weaker tier's model is still a model
    that may be external, so degrading onto it would defeat the constraint."""
    models, assign = governed
    models.append(_model("onprem", family="ollama", privacy_class="deployment_local"))
    assign(Tier.MEDIUM, "onprem")     # nothing on complex at all
    with pytest.raises(NoEligibleModel):
        ModelRouter().route("q", model_hint="complex", data_classification="RESTRICTED")


def test_public_traffic_is_unconstrained_under_governance_too(governed):
    """The floor must not over-restrict: the highest-priority model wins."""
    models, assign = governed
    models.append(_model("cloud", family="anthropic", privacy_class="external"))
    models.append(_model("onprem", family="ollama", privacy_class="deployment_local"))
    assign(Tier.COMPLEX, "cloud", priority=1)
    assign(Tier.COMPLEX, "onprem", priority=2)
    d = ModelRouter().route("q", model_hint="complex", data_classification="PUBLIC")
    assert d.provider_model_override == "model-cloud"
