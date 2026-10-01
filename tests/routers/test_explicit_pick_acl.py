# SPDX-License-Identifier: MIT
"""§N.1 step 9 (D37) — the model picker's ACL asks about the model picked.

§F marks "Explicit model selection" as *Preserved*. It was not working.

``gateway.py`` resolved the access-control target with
``hint_to_model_id(hint)``, the pre-registry map of hint string → .env
constant, and then skipped the whole check on a falsy result:

    _gov_model = _gov_hint_to_model(_model_hint)
    if _gov_model:                      # <- '' and None both skip
        ...filter_allowed_models([_gov_model], ...)

Measured on this deployment, that made five of the eight tier heads
unblockable from Admin → Model Governance:

    claude-sonnet-5              ''    CLAUDE_SONNET_5_MODEL is unset
    claude-opus-5                ''    CLAUDE_OPUS_5_MODEL is unset
    claude-haiku-4-5-20251001    None  not an _HINT_MAP key
    claude-sonnet-5-5            None  not an _HINT_MAP key
    veo-3.1-generate-preview     None  not an _HINT_MAP key

``claude-sonnet-5`` is in ``llm_models`` and is the head of the ``complex``
tier — the registry knows it and the legacy map does not. That gap is the
two-model-systems problem this migration exists to close, in one line.
"""

from __future__ import annotations

import pytest

from models.model_router import hint_to_model_id, resolve_pick_to_model_id


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    """The eight tier heads as they stand on this deployment, plus one local."""
    models = [
        {"model_id": "claude-sonnet-5",           "family": "anthropic"},
        {"model_id": "claude-opus-5",             "family": "anthropic"},
        {"model_id": "claude-haiku-4-5-20251001", "family": "anthropic"},
        {"model_id": "claude-sonnet-5-5",         "family": "anthropic"},
        {"model_id": "claude-sonnet-4-6",         "family": "anthropic"},
        {"model_id": "gemini-3.1-flash-image",    "family": "gemini"},
        {"model_id": "veo-3.1-generate-preview",  "family": "gemini"},
        {"model_id": "llama3.2:1b",               "family": "ollama"},
    ]
    monkeypatch.setattr("core.llm_provider_registry.get_enabled_models",
                        lambda **kw: models)
    return models


# ── the regression, named ─────────────────────────────────────────────────


THE_FIVE = [
    "claude-sonnet-5",
    "claude-opus-5",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-5-5",
    "veo-3.1-generate-preview",
]


@pytest.mark.parametrize("model_id", THE_FIVE)
def test_the_models_that_could_not_be_blocked_now_resolve(model_id):
    """A table rather than a loop over the registry, so the regression is
    NAMED. If a future change makes one of these unresolvable again, the
    failure says which model an admin has just lost control of."""
    assert resolve_pick_to_model_id(model_id) == model_id


@pytest.mark.parametrize("model_id", THE_FIVE)
def test_the_old_helper_now_agrees(model_id):
    """hint_to_model_id used to answer from .env constants and returned nothing
    for these five. Since Phase 8 it reads the registry too, so both agree."""
    assert hint_to_model_id(model_id) == model_id


def test_a_registry_id_wins_over_the_alias_table():
    """Order matters: 'claude-sonnet-5' is BOTH a registry model id and an
    _HINT_MAP key whose tier's env constant is empty. Consulting the alias
    table first would keep returning ''."""
    assert resolve_pick_to_model_id("claude-sonnet-5") == "claude-sonnet-5"


# ── what still has to work ────────────────────────────────────────────────


def test_an_addressed_local_model_passes_through():
    """`local:<id>` is how the in-house proxy is addressed, and the ACL stores
    rows in exactly that form (see model_governance_router's AgentStudio
    note), so the prefix must survive — including its case."""
    assert resolve_pick_to_model_id("local:Kimi-k2.5") == "local:Kimi-k2.5"


@pytest.mark.parametrize("value", ["auto", "AUTO", "default", "none", "", "  "])
def test_auto_is_not_a_pick(value):
    """core.tiers.LEGACY_INBOUND_ALIASES maps "auto" to Tier.SIMPLE, which is
    reasonable for an inbound CLI hint and wrong as an answer to "which model
    did the user name". gateway.py normalises these to None before calling,
    but this is a public helper and the next caller may not."""
    assert resolve_pick_to_model_id(value) == ""


def test_an_unknown_model_id_resolves_to_nothing():
    """So the caller falls through to its q.local_model fallback rather than
    checking an ACL for a model that does not exist."""
    assert resolve_pick_to_model_id("not-a-real-model") == ""


def test_a_legacy_alias_resolves_to_the_tier_head(monkeypatch):
    """CLI and IDE clients send tier names and vendor words (§E). With
    governance on, route() resolves the tier they mean — so that is what the
    ACL must be asked about, not the tier's .env constant.

    The pre-migration answer for 'medium' was 'gpt-5.4': a model with no
    llm_models row and no provider here, for which no admin would ever write
    a rule, so the check passed unconditionally while the turn dispatched
    claude-sonnet-4-6.
    """
    from core.tier_resolver import ResolvedModel
    from core.tiers import Tier

    heads = {Tier.MEDIUM: "claude-sonnet-4-6", Tier.COMPLEX: "claude-sonnet-5"}

    def fake_resolve(tier, c=None, **kw):
        return ResolvedModel(
            model_id=heads[Tier(tier)], row_id="r", provider_id="p",
            provider_slug="s", family="anthropic", base_url=None,
            capabilities={}, tier=Tier(tier), requested_tier=Tier(tier),
        )

    monkeypatch.setattr("core.tier_resolver.resolve_tier", fake_resolve)

    assert resolve_pick_to_model_id("medium") == "claude-sonnet-4-6"
    assert resolve_pick_to_model_id("complex") == "claude-sonnet-5"


def test_a_legacy_alias_feeds_the_phase_10_removal_counter(monkeypatch):
    """note_legacy_alias is what gates the shim's removal: plan.html allows
    Phase 10 to delete the alias translation only once
    ainxt_legacy_model_alias_total has read zero for a release. §N.1 step 11
    wired the ainxt-api boundary into the same counter; this is the second
    consumer, so the migration keeps contributing to its own retirement
    instead of adding copies nobody counts."""
    noted = []
    monkeypatch.setattr("core.tiers.note_legacy_alias",
                        lambda v, surface: noted.append((v, surface)))

    from core.tier_resolver import ResolvedModel
    from core.tiers import Tier
    monkeypatch.setattr(
        "core.tier_resolver.resolve_tier",
        lambda tier, c=None, **kw: ResolvedModel(
            model_id="x", row_id="r", provider_id="p", provider_slug="s",
            family="f", base_url=None, capabilities={},
            tier=Tier(tier), requested_tier=Tier(tier)),
    )

    resolve_pick_to_model_id("claude")
    assert noted, "a legacy alias was translated without being counted"
    assert noted[0][0] == "claude"


def test_a_registry_id_is_not_counted_as_a_legacy_alias(monkeypatch):
    """The counter has to mean something. A user picking a real model id from
    the dropdown is the SUPPORTED path, not a deprecated one — counting it
    would keep the shim's removal gate above zero forever."""
    noted = []
    monkeypatch.setattr("core.tiers.note_legacy_alias",
                        lambda v, surface: noted.append(v))
    resolve_pick_to_model_id("claude-sonnet-5")
    assert noted == []


def test_a_broken_registry_does_not_block_the_turn(monkeypatch):
    """Fails open: an access-control lookup that cannot run returns no pick
    (the caller falls through) rather than becoming a 403."""
    def boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr("core.llm_provider_registry.get_enabled_models", boom)
    monkeypatch.setattr("core.llm_provider_registry.get_model", lambda mid: boom(), raising=True)
    assert not resolve_pick_to_model_id("claude-sonnet-4-6")


# ── the filter factory the four entry points share ────────────────────────


def test_no_identity_means_no_filter():
    """An anonymous or service-to-service turn behaves exactly as today: there
    is nothing to check, so route() is not handed a filter at all."""
    from routers.model_governance_router import acl_filter_for
    assert acl_filter_for(None, "Finance") is None
    assert acl_filter_for("", "Finance") is None


def test_the_filter_forwards_ids_verbatim(monkeypatch):
    seen = {}

    def fake_filter(model_ids, user_id, department, db):
        seen.update(ids=list(model_ids), user=user_id, dept=department)
        return [m for m in model_ids if m != "claude-sonnet-5"]

    monkeypatch.setattr(
        "routers.model_governance_router.filter_allowed_models", fake_filter)

    class _DB:
        def close(self): seen["closed"] = True
    monkeypatch.setattr("db.database.SessionLocal", lambda: _DB())

    from routers.model_governance_router import acl_filter_for
    f = acl_filter_for("u-1", "Finance")
    assert f(["claude-sonnet-5", "claude-opus-5"]) == ["claude-opus-5"]
    assert seen["ids"] == ["claude-sonnet-5", "claude-opus-5"]
    assert seen["user"] == "u-1"
    assert seen["dept"] == "Finance"
    assert seen.get("closed") is True, "the session must be closed per call"


def test_a_missing_department_is_an_empty_string(monkeypatch):
    """filter_allowed_models binds :dept into SQL; None would make the
    department clause match nothing rather than nothing-in-particular."""
    seen = {}
    monkeypatch.setattr(
        "routers.model_governance_router.filter_allowed_models",
        lambda ids, u, d, db: seen.update(dept=d) or list(ids))

    class _DB:
        def close(self): pass
    monkeypatch.setattr("db.database.SessionLocal", lambda: _DB())

    from routers.model_governance_router import acl_filter_for
    acl_filter_for("u-1", None)(["m"])
    assert seen["dept"] == ""
