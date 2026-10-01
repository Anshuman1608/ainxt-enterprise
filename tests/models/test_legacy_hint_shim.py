# SPDX-License-Identifier: MIT
"""Phase 6, decision D15 — the ``legacy_hint=`` shim.

Phase 6 replaces ``model_hint="simple"`` with ``tier=Tier.SIMPLE`` at ~90 call
sites. That is only safe because of this shim, and this file is what says so.

The problem it solves: ``_TIER_TO_LEGACY_HINT`` is not the inverse of the
migration. ``Tier.SIMPLE`` coerces to ``"haiku"`` — cloud Claude Haiku — while
the call sites becoming ``Tier.SIMPLE`` pass ``model_hint="simple"`` today,
which means the LOCAL model. Without ``legacy_hint`` those sites would move
local → cloud on every deployment that has not set TIER_GOVERNANCE_ENABLED,
and the flag would stop being a rollback.

So: ``legacy_hint`` is what the call site used to pass. Since Phase 8 it is no
longer consumed for routing (a tier= call resolves or raises), but
``_coerce_tier`` still validates it; the flag-off cases here were retired.
"""

from __future__ import annotations

import pytest

import core.llm_provider_registry as reg
import core.tier_resolver as tr
from core.tiers import Tier
from models.model_router import (
    ModelRouter,
    TIER_GOVERNED,
)


def _model(row_id: str, model_id: str) -> dict:
    return {
        "id": row_id, "model_id": model_id, "display_name": row_id,
        "capabilities": {"modality": ["text"], "privacy_class": "external"},
        "is_default": False, "sort_order": 0, "provider_id": "p",
        "provider_slug": "anthropic", "provider_name": "anthropic",
        "family": "anthropic", "base_url": None,
    }


@pytest.fixture
def governed(monkeypatch: pytest.MonkeyPatch):
    """A registry and assignment table the test controls."""
    models: list = []
    assignments: list = []
    monkeypatch.setattr(reg, "get_enabled_models", lambda channel=None: list(models))
    monkeypatch.setattr(tr, "get_tier_assignments", lambda: list(assignments))
    monkeypatch.setattr(tr, "_breaker_is_open", lambda slug, mid: False)

    def assign(tier: Tier, row_id: str, priority: int = 1) -> None:
        assignments.append({"tier": tier.value, "model_row_id": row_id,
                            "priority": priority, "role": None, "org_id": "default"})
    return models, assign


# ── The three programming errors _coerce_tier refuses to let through ───────


def test_legacy_hint_without_tier_is_a_programming_error():
    """`legacy_hint` on its own says nothing — it only has meaning as "the hint
    this call site used before it asked for a tier". Supplied alone it is
    almost certainly a half-finished migration, so it fails loudly."""
    with pytest.raises(ValueError, match="requires tier="):
        ModelRouter().route("q", legacy_hint="simple")


def test_an_unknown_legacy_hint_is_rejected():
    """A typo must not slide through _HINT_MAP and become the medium default.

    The entire value of legacy_hint is that it reproduces a SPECIFIC prior
    behaviour. One that silently reproduces a DIFFERENT one is worse than
    having no shim at all, because it looks like it worked.
    """
    with pytest.raises(ValueError, match="not a known routing hint"):
        ModelRouter().route("q", tier=Tier.SIMPLE, legacy_hint="simpel")


def test_tier_and_model_hint_together_still_raise():
    with pytest.raises(ValueError, match="not both"):
        ModelRouter().route("q", tier=Tier.SIMPLE, model_hint="simple")


# ── What it actually does ──────────────────────────────────────────────────


def test_the_assignment_beats_the_legacy_hint(governed):
    """Opting in is what makes the tier win — not the code change."""
    models, assign = governed
    models.append(_model("row-a", "claude-sonnet-4-5"))
    assign(Tier.SIMPLE, "row-a")

    d = ModelRouter().route("q", tier=Tier.SIMPLE, legacy_hint="simple")
    assert d.tier == TIER_GOVERNED
    assert d.provider_model_override == "claude-sonnet-4-5"
    assert d.requested_tier is Tier.SIMPLE


def test_an_unassigned_tier_raises_rather_than_using_the_legacy_hint(governed):
    """D107: no partial rollout through the old chain; the error names the tier."""
    with pytest.raises(tr.NoEligibleModel):
        ModelRouter().route("q", tier=Tier.SIMPLE, legacy_hint="simple")


def test_intent_classification_reaches_its_own_tier_not_the_haiku_stub(governed):
    """intent-classification has no legacy equivalent — _TIER_TO_LEGACY_HINT
    maps it onto "haiku" only so the Phase 1 map is total. With governance on
    it must resolve through its OWN assignment, which is the entire reason
    cil/intent.py is being migrated."""
    models, assign = governed
    models.append(_model("row-ic", "claude-haiku-4-5-20251001"))
    assign(Tier.INTENT_CLASSIFICATION, "row-ic")

    d = ModelRouter().route("q", tier=Tier.INTENT_CLASSIFICATION,
                            legacy_hint="local_mini")
    assert d.provider_model_override == "claude-haiku-4-5-20251001"
    assert d.requested_tier is Tier.INTENT_CLASSIFICATION


# ── no_cloud_egress as a caller-supplied constraint (§M.1, §D.2 memory) ────


def test_no_cloud_egress_keyword_narrows_candidates(governed):
    """Flag on, §M.1's shape: the tier is UNCHANGED and only deployment-local
    candidates survive. A caller asserting the constraint must get the same
    guarantee as one that labelled the turn CONFIDENTIAL."""
    models, assign = governed
    cloud = _model("cloud", "claude-sonnet-4-5")
    local = _model("local", "llama3.2:1b")
    local["capabilities"]["privacy_class"] = "deployment_local"
    models.extend([cloud, local])
    assign(Tier.SIMPLE, "cloud", priority=1)
    assign(Tier.SIMPLE, "local", priority=2)

    d = ModelRouter().route("q", tier=Tier.SIMPLE, legacy_hint="simple",
                            no_cloud_egress=True)
    assert d.provider_model_override == "llama3.2:1b"
    assert d.requested_tier is Tier.SIMPLE


def test_no_cloud_egress_cannot_be_switched_off_by_the_classification(governed):
    """The two sources are OR-ed, never AND-ed. A PUBLIC label must not
    cancel a caller that knows its content may not leave the estate."""
    models, assign = governed
    models.append(_model("cloud", "claude-sonnet-4-5"))   # external only
    assign(Tier.SIMPLE, "cloud")

    from core.tier_resolver import NoEligibleModel
    with pytest.raises(NoEligibleModel):
        ModelRouter().route("q", tier=Tier.SIMPLE, legacy_hint="simple",
                            data_classification="PUBLIC", no_cloud_egress=True)
