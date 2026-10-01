# SPDX-License-Identifier: MIT
"""§N.1 step 9, risk N9-a — "is this turn local" must ask the model.

Two chat paths decided this from the hint string: ``_fp_hint in ("local",
"simple")``. That was true by construction while "simple" *meant* the in-house
model. Step 9 makes ``Tier.SIMPLE`` resolve to whatever the administrator
assigned — Claude Haiku on this deployment — at which point the old test calls
a paid cloud turn "local" and two things go wrong:

  * ``_LOCAL_KV_CACHE_HOIST`` (default ON) prepends a system message built for
    a local model's KV cache onto a Claude request; and
  * the budget chip is suppressed, so the user is billed and the budget bar
    silently stops moving.

The second is the one that loses money, and it is the reason the post-dispatch
decision asks ``is_deployment_local(<the model that ran>)`` rather than reusing
the pre-dispatch prediction.
"""

from __future__ import annotations

import pytest

from core.tiers import Tier
from models.model_router import is_deployment_local, is_local_route


# ── the predicate ─────────────────────────────────────────────────────────


def test_an_addressed_local_model_is_local():
    assert is_deployment_local("local:Kimi-k2.5") is True
    assert is_deployment_local("LOCAL:Kimi-k2.5") is True


@pytest.mark.parametrize("value", ["", None, "   ", "auto"])
def test_nothing_useful_is_not_local(value):
    """Fails CLOSED — "treat as billable". Showing a budget chip for a free
    turn is cosmetic; hiding one for a paid turn is not."""
    assert is_deployment_local(value) is False


def test_it_reads_privacy_class_from_the_registry(monkeypatch):
    monkeypatch.setattr(
        "core.llm_provider_registry.get_enabled_models",
        lambda **kw: [
            {"model_id": "inhouse-7b", "capabilities": {"privacy_class": "deployment_local"}},
            {"model_id": "vendor-5",   "capabilities": {"privacy_class": "external"}},
        ],
    )
    monkeypatch.setattr("gateway_local_llm.is_local_model", lambda m: False)
    assert is_deployment_local("inhouse-7b") is True
    assert is_deployment_local("vendor-5") is False


def test_absent_privacy_metadata_is_not_local(monkeypatch):
    """Same fail-safe direction core/tier_resolver.py:275 uses for
    no_cloud_egress: absent metadata is NOT deployment_local."""
    monkeypatch.setattr(
        "core.llm_provider_registry.get_enabled_models",
        lambda **kw: [{"model_id": "mystery", "capabilities": {}}],
    )
    monkeypatch.setattr("gateway_local_llm.is_local_model", lambda m: False)
    assert is_deployment_local("mystery") is False


def test_the_local_proxy_catalog_still_counts(monkeypatch):
    """A union, not a replacement. gateway_local_llm.is_local_model is what
    _estimate_cost already consults to bill in-house models at $0 — so a model
    the local proxy serves but the registry has no row for must still be
    "local", or this would disagree with the cost the same turn is charged.
    """
    monkeypatch.setattr("core.llm_provider_registry.get_enabled_models",
                        lambda **kw: [])
    monkeypatch.setattr("gateway_local_llm.is_local_model",
                        lambda m: m == "nemotron-mini")
    assert is_deployment_local("nemotron-mini") is True
    assert is_deployment_local("claude-sonnet-5") is False


def test_a_broken_registry_does_not_raise_into_a_turn(monkeypatch):
    def boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr("core.llm_provider_registry.get_enabled_models", boom)
    monkeypatch.setattr("gateway_local_llm.is_local_model", lambda m: False)
    assert is_deployment_local("anything") is False


# ── the pre-dispatch prediction ───────────────────────────────────────────


def test_an_explicit_local_model_wins_immediately():
    """The user pinned an in-house model; nothing else needs consulting."""
    assert is_local_route("Kimi-k2.5", {"tier": Tier.COMPLEX}) is True


def test_a_tier_is_resolved_rather_than_guessed(monkeypatch):
    """The point of the whole change: the answer comes from the assignment."""
    from core.tier_resolver import ResolvedModel

    def fake(tier, c=None, **kw):
        caps = ({"privacy_class": "deployment_local"} if tier is Tier.MINI
                else {"privacy_class": "external"})
        return ResolvedModel(
            model_id=f"model-for-{tier.value}", row_id="r", provider_id="p",
            provider_slug="s", family="f", base_url=None, capabilities=caps,
            tier=tier, requested_tier=tier,
        )

    monkeypatch.setattr("core.tier_resolver.resolve_tier", fake)
    monkeypatch.setattr("core.llm_provider_registry.get_enabled_models",
                        lambda **kw: [
                            {"model_id": "model-for-mini",
                             "capabilities": {"privacy_class": "deployment_local"}},
                            {"model_id": "model-for-simple",
                             "capabilities": {"privacy_class": "external"}},
                        ])
    monkeypatch.setattr("gateway_local_llm.is_local_model", lambda m: False)

    assert is_local_route(None, {"tier": Tier.MINI}) is True
    # The regression this test exists for: `simple` is NOT local any more.
    assert is_local_route(None, {"tier": Tier.SIMPLE}) is False


def test_an_unresolvable_tier_predicts_billable(monkeypatch):
    def boom(tier, c=None, **kw):
        raise RuntimeError("nothing assigned")
    monkeypatch.setattr("core.tier_resolver.resolve_tier", boom)
    assert is_local_route(None, {"tier": Tier.MEDIUM}) is False


def test_the_in_house_hint_names_are_local():
    """"local" carries no_cloud_egress; "simple" is whatever an admin assigns (Phase 8)."""
    assert is_local_route(None, {"model_hint": "local"}) is True
    assert is_local_route(None, {"model_hint": "local:foo"}) is True
    assert is_local_route(None, {"model_hint": "complex"}) is False
    assert is_local_route(None, {"model_hint": "claude"}) is False


def test_a_users_explicit_registry_pick_is_answered_from_the_registry(monkeypatch):
    """Something the string test could never do: a user who picks a concrete
    in-house model id from the dropdown gets the right answer."""
    monkeypatch.setattr(
        "core.llm_provider_registry.get_enabled_models",
        lambda **kw: [{"model_id": "inhouse-7b",
                       "capabilities": {"privacy_class": "deployment_local"}}],
    )
    monkeypatch.setattr("gateway_local_llm.is_local_model", lambda m: False)
    assert is_local_route(None, {"model_hint": "inhouse-7b"}) is True


def test_no_route_at_all_is_not_local():
    assert is_local_route(None, None) is False
    assert is_local_route(None, {}) is False
    assert is_local_route("", {}) is False
