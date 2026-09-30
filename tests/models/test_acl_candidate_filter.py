# SPDX-License-Identifier: MIT
"""§N.1 step 9 (D36/D38) — the department ACL as a filter on tier candidates.

Before step 9 the Chat Auto path ACL-checked ``hint_to_model_id(hint)``, the
pre-registry map of hint string → .env constant. On this deployment that
answers ``'gpt-5.4'`` for the Auto default while the turn dispatches
``claude-sonnet-4-6``, and ``filter_allowed_models`` treats a model with no
rule as ALLOWED — so the check passed for a model that was never called and
the model that was called was never checked.

The fix is to filter the tier's own resolved candidate list, which is also
where "candidate 1 is blocked, try candidate 2" already lives
(``_attempts_from_resolved``). These tests pin the four properties that makes
load-bearing:

  1. the filter sees the REAL model ids, once, as a list;
  2. a blocked head serves from the next candidate, in the admin's order;
  3. an entirely blocked tier raises ModelsBlockedByPolicy and does NOT
     degrade to the legacy .env chain — the governance-escape assertion;
  4. no ``acl_filter`` behaves exactly as before, so §M.4 holds for every
     application-tier caller.
"""

from __future__ import annotations

import pytest

from core.tier_resolver import Constraints, NoEligibleModel, ResolvedModel
from core.tiers import Tier
from models.model_router import ModelRouter, ModelsBlockedByPolicy


def _rm(model_id: str, priority: int, family: str = "anthropic") -> ResolvedModel:
    return ResolvedModel(
        model_id=model_id, row_id=f"row-{model_id}", provider_id="prov",
        provider_slug="slug", family=family, base_url=None, capabilities={},
        tier=Tier.MEDIUM, requested_tier=Tier.MEDIUM, priority=priority,
    )


# Priority order deliberately NOT alphabetical, so a test that accidentally
# asserts sorted order rather than the administrator's fails.
CANDIDATES = [_rm("zeta-1", 1), _rm("alpha-2", 2), _rm("mid-3", 3)]


# ── 1. what the filter is handed ──────────────────────────────────────────


def test_the_filter_receives_the_real_model_ids_once():
    """Not hints, not families, not one call per candidate.

    One call because acl_filter wraps a database round-trip; the whole list
    because the ACL query is a single UNION ALL over both rule tables. A
    per-candidate implementation would turn a two-candidate tier into two
    sequential queries on the hot path of every Auto turn.
    """
    seen: list[list[str]] = []

    def acl(ids):
        seen.append(list(ids))
        return list(ids)

    ModelRouter._apply_acl(Tier.MEDIUM, CANDIDATES, acl)
    assert seen == [["zeta-1", "alpha-2", "mid-3"]]


def test_the_ids_are_never_hint_strings_or_families():
    """The specific regression: 'medium' or 'anthropic' reaching the ACL.

    hint_to_model_id's answer for the Auto default was a hint-derived .env
    constant, and step 6's first draft made the sibling mistake of forwarding
    a registry FAMILY where a model id was wanted. Both would silently pass
    an ACL that defaults absent rules to allowed.
    """
    captured: list[str] = []
    ModelRouter._apply_acl(Tier.MEDIUM, CANDIDATES,
                           lambda ids: captured.extend(ids) or list(ids))
    assert "medium" not in captured
    assert "anthropic" not in captured
    assert captured == [c.model_id for c in CANDIDATES]


# ── 2. a blocked head falls to the next candidate ─────────────────────────


def test_a_blocked_head_serves_from_the_next_candidate():
    kept = ModelRouter._apply_acl(
        Tier.MEDIUM, CANDIDATES, lambda ids: [i for i in ids if i != "zeta-1"])
    assert [c.model_id for c in kept] == ["alpha-2", "mid-3"]


def test_the_order_is_the_administrators_not_the_acls():
    """§M.5: the priority list is the admin's ordering. A filter that returns
    its answer in a different order — a set round-trip is enough to do that —
    must not reorder the candidates, or an admin's priority 1 silently becomes
    priority 2."""
    kept = ModelRouter._apply_acl(
        Tier.MEDIUM, CANDIDATES, lambda ids: list(reversed(ids)))
    assert [c.model_id for c in kept] == ["zeta-1", "alpha-2", "mid-3"]


def test_an_unknown_id_in_the_filters_answer_is_ignored():
    """Membership, not trust. A filter that invents an id cannot inject a
    model into the candidate list."""
    kept = ModelRouter._apply_acl(
        Tier.MEDIUM, CANDIDATES, lambda ids: ["mid-3", "smuggled-in"])
    assert [c.model_id for c in kept] == ["mid-3"]


# ── 3. fully blocked: the governance-escape assertion ─────────────────────


def test_a_fully_blocked_tier_raises_models_blocked_by_policy():
    with pytest.raises(ModelsBlockedByPolicy) as exc:
        ModelRouter._apply_acl(Tier.MEDIUM, CANDIDATES, lambda ids: [])
    # The refused ids travel on the exception so the caller's log can name
    # what the rule actually hit, which is the operator-facing half.
    assert exc.value.blocked == ["zeta-1", "alpha-2", "mid-3"]
    assert exc.value.tier is Tier.MEDIUM


def test_models_blocked_by_policy_is_not_a_no_eligible_model():
    """The type distinction IS the behaviour.

    _resolve_governed returns None on NoEligibleModel, and None means "run the
    legacy .env chain". If ModelsBlockedByPolicy were a subclass — or were
    reported as NoEligibleModel — then blocking every model for a department
    would cause the request to be served by an UNGOVERNED cloud model read
    from the .env constants. That is the exact escape the pre-migration
    comment at gateway.py:7591 was written to prevent.
    """
    assert not issubclass(ModelsBlockedByPolicy, NoEligibleModel)


def test_a_fully_blocked_tier_never_reaches_the_legacy_chain(monkeypatch):
    """The property the test above only implies, asserted end to end.

    Structured as "the fallback was not entered" rather than "an exception was
    raised", because a re-raise that lands in some caller's own `except` would
    satisfy the weaker form while still egressing.
    """
    router = ModelRouter()
    entered: list[str] = []
    monkeypatch.setattr(
        "models.model_router._warn_env_fallback",
        lambda tier, reason: entered.append(str(tier)),
    )
    monkeypatch.setattr(
        "core.tier_resolver.resolve_tier_candidates",
        lambda tier, c=None, **kw: list(CANDIDATES),
    )

    with pytest.raises(ModelsBlockedByPolicy):
        router._resolve_governed(
            Tier.MEDIUM, {}, legacy_tier="medium", complexity="medium",
            is_vision=False, hint="medium", constraints_kw={}, channel=None,
            acl_filter=lambda ids: [],
        )
    assert entered == [], (
        "a governance block fell through to the .env constants — "
        "_resolve_governed must re-raise ModelsBlockedByPolicy before its "
        "NoEligibleModel and bare-Exception handlers"
    )


def test_the_bare_exception_handler_does_not_swallow_it(monkeypatch):
    """_resolve_governed's last handler is `except Exception`, added so that
    governance can never break routing. It is also the handler that would
    quietly turn a policy denial into a legacy-chain dispatch, so the
    ModelsBlockedByPolicy clause must come first."""
    import inspect
    src = inspect.getsource(ModelRouter._resolve_governed)
    i_policy = src.index("except ModelsBlockedByPolicy")
    i_noelig = src.index("except NoEligibleModel")
    i_bare = src.index("except Exception")
    assert i_policy < i_noelig < i_bare


# ── 4. no filter = today's behaviour (§M.4) ───────────────────────────────


def test_no_filter_resolves_exactly_as_before(monkeypatch):
    """§M.4 — application tiers are not ACL-filtered. The platform's own
    classification and summarisation calls must not be blockable by the
    department rules that govern a user's chat turns, so acl_filter defaults
    to None and None must be a true no-op.
    """
    router = ModelRouter()
    monkeypatch.setattr(
        "core.tier_resolver.resolve_tier_candidates",
        lambda tier, c=None, **kw: list(CANDIDATES),
    )
    common = dict(legacy_tier="medium", complexity="medium", is_vision=False,
                  hint="medium", constraints_kw={}, channel=None)

    without = router._resolve_governed(Tier.MEDIUM, {}, **common)
    explicit_none = router._resolve_governed(Tier.MEDIUM, {}, acl_filter=None, **common)

    assert without == explicit_none
    assert without.model == "zeta-1"
    assert [r.model_id for r in without.resolved] == [c.model_id for c in CANDIDATES]


def test_every_router_entry_point_accepts_the_filter():
    """A chat entry point that cannot pass it has no way to apply the ACL —
    which is how /kb/ask ended up with no check at all."""
    import inspect
    for name in ("route", "generate", "stream", "async_generate", "async_stream"):
        sig = inspect.signature(getattr(ModelRouter, name))
        p = sig.parameters.get("acl_filter")
        assert p is not None, f"{name}() cannot apply an ACL"
        assert p.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert p.default is None, f"{name}() must default to unfiltered (§M.4)"


# ── Failure mode: the ACL lookup itself is down ───────────────────────────


def test_a_broken_filter_fails_open_and_says_so(caplog):
    """The pre-migration block wrapped its whole governance walk in
    "check error (fail-open)" — a governance lookup that is down must not
    take chat down with it. Preserved, but logged at WARNING rather than
    swallowed, because silently unenforced access control is worth an alert.
    """
    import logging

    def boom(ids):
        raise RuntimeError("connection refused")

    with caplog.at_level(logging.WARNING):
        kept = ModelRouter._apply_acl(Tier.MEDIUM, CANDIDATES, boom)
    assert [c.model_id for c in kept] == [c.model_id for c in CANDIDATES]
    assert any("fail-open" in r.message or "fail-open" in r.getMessage()
               for r in caplog.records)


def test_an_empty_candidate_list_is_not_a_policy_block():
    """A tier with nothing assigned is a DEPLOYMENT gap — NoEligibleModel's
    territory, which degrades to the legacy chain on purpose. Reporting it as
    a policy block would turn "the admin has not configured this yet" into
    "you are not allowed", and would stop D15's flag-off path working."""
    assert ModelRouter._apply_acl(Tier.MEDIUM, [], lambda ids: []) == []
