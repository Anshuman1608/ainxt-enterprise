# SPDX-License-Identifier: MIT
"""§N.1 step 10 (D45) — require_role and distinct_from_family reach the resolver.

plan.html calls SDLC "the only consumer of both the role='review' and
distinct_from_family constraints". Neither was reachable from a `tier=` call
before this step, for two different reasons:

  require_role           route() did not have the parameter AT ALL. The
                         constraint existed only as the legacy "solution"
                         hint's extra (now _ALIAS_EXTRAS), and route()'s
                         explicit-tier branch passes `extra={}` — so a call
                         saying tier=Tier.COMPLEX could not express "this is a
                         review gate" no matter what it did.

  distinct_from_family   route() has taken it since Phase 5, but generate()
                         and stream() never forwarded it, so the only way to
                         reach it was to call route() directly and dispatch
                         yourself — which is precisely what the SDLC manifest
                         validator was doing.

The legacy-hint path must be unmoved by either: a deployment with governance
off, or one still on the `solution` string, has to behave exactly as before.
"""

from __future__ import annotations

import inspect

import pytest

from core.tier_resolver import ROLE_REVIEW
from core.tiers import Tier
from models.model_router import _ALIAS_EXTRAS, ModelRouter, model_router


@pytest.fixture
def captured(monkeypatch):
    """Capture the Constraints handed to the resolver by one route() call."""
    # A LIST, so a test can assert the resolver was asked exactly once.
    seen = []

    def _resolve(tier, c=None, **kw):
        seen.append({"tier": tier, "constraints": c})
        raise RuntimeError("stop here — the constraints are what this asserts")

    monkeypatch.setattr("core.tier_resolver.resolve_tier_candidates", _resolve)
    return seen


def _route(**kw):
    try:
        model_router.route("hello", **kw)
    except Exception:
        pass


# ── The signatures ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", ["route", "generate", "stream"])
@pytest.mark.parametrize("param", ["require_role", "distinct_from_family"])
def test_the_entry_points_accept_both(method, param):
    sig = inspect.signature(getattr(ModelRouter, method))
    assert param in sig.parameters, f"{method}() cannot express {param}"
    assert sig.parameters[param].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize("method", ["generate", "stream"])
@pytest.mark.parametrize("param", ["require_role", "distinct_from_family"])
def test_the_entry_points_FORWARD_both(method, param):
    """Accepting a parameter and dropping it is worse than not having it: the
    call site reads as governed and is not. Asserted over the source because
    both methods are long and the forward is one keyword in a large call.

    Scoped to the ModelRouter CLASS body — `generate` is also the name of the
    provider-gateway protocol method several classes in this module define,
    and the first one ast.walk finds is not the one under test. (Caught by
    this test failing on a correct implementation.)"""
    import ast
    import pathlib

    src = pathlib.Path(inspect.getsourcefile(ModelRouter)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "ModelRouter")
    fn = next(n for n in cls.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == method)
    forwards = [
        kw for call in ast.walk(fn)
        if isinstance(call, ast.Call)
        and getattr(call.func, "attr", "") == "route"
        for kw in call.keywords
        if kw.arg == param
    ]
    assert forwards, f"{method}() accepts {param} but never passes it to route()"


# ── What reaches the resolver ───────────────────────────────────────────────


def test_require_role_reaches_the_constraints_from_an_explicit_tier(captured):
    _route(tier=Tier.COMPLEX, legacy_hint="complex", require_role=ROLE_REVIEW)
    assert captured[0]["constraints"].require_role == ROLE_REVIEW


def test_distinct_from_family_reaches_the_constraints(captured):
    _route(tier=Tier.COMPLEX, legacy_hint="deep", distinct_from_family="anthropic")
    assert captured[0]["constraints"].distinct_from_family == "anthropic"


def test_both_together(captured):
    _route(tier=Tier.COMPLEX, legacy_hint="deep",
           require_role=ROLE_REVIEW, distinct_from_family="openai")
    c = captured[0]["constraints"]
    assert (c.require_role, c.distinct_from_family) == (ROLE_REVIEW, "openai")


def test_absent_by_default(captured):
    _route(tier=Tier.COMPLEX, legacy_hint="complex")
    c = captured[0]["constraints"]
    assert c.require_role is None and c.distinct_from_family is None


# ── The legacy path is unmoved ──────────────────────────────────────────────


def test_the_solution_hint_still_carries_the_review_role(captured):
    """The pre-migration expression of the same thing. It must keep working:
    every unmigrated caller still says model_hint="solution", and §E holds the
    alias until Phase 9/10."""
    _route(model_hint="solution")
    assert captured[0]["constraints"].require_role == ROLE_REVIEW


def test_the_legacy_extra_wins_over_an_explicit_argument(captured):
    """Constraints(**{**constraints_kw, **extra}) — `extra` last. That
    ordering predates this step: an alias's own constraint is not overridden by a caller's.
    Pinned so a future refactor of the merge does not silently invert it."""
    _route(model_hint="solution", require_role=None)
    assert captured[0]["constraints"].require_role == ROLE_REVIEW
    assert _ALIAS_EXTRAS["solution"] == {"require_role": ROLE_REVIEW}


def test_a_plain_complex_tier_does_NOT_get_the_review_role(captured):
    """D43, stated as an assertion. This is the behaviour change: before step
    10 SDLC reached COMPLEX through the "solution" string and therefore always
    preferred the reviewer; asking for the tier directly no longer does."""
    _route(tier=Tier.COMPLEX, legacy_hint="solution")
    assert captured[0]["constraints"].require_role is None


def test_a_failed_tier_resolution_does_not_retry_through_the_legacy_hint(captured):
    """D107. Before Phase 8 a failed explicit-tier resolution dropped to the
    legacy-hint branch and resolved a second time; now it raises, so a stage
    that needs the review role must ask for it (SDLC_STAGE_TIERS does)."""
    _route(tier=Tier.COMPLEX, legacy_hint="solution")
    assert len(captured) == 1
    assert captured[0]["constraints"].require_role is None


# ── The resolver's own semantics, which the above depends on ────────────────


def test_require_role_is_a_preference_and_not_a_filter():
    """§M.3a. A deployment with one capable model must still be able to run a
    review gate — the admin sees author and reviewer coincide rather than the
    stage failing. If this ever became a filter, every single-model install
    would start failing SDLC at the code-review gate."""
    from core.tier_resolver import Constraints, _survivors

    only = [{"model_id": "m1", "role": None, "capabilities": {}, "family": "x"}]
    out = _survivors(list(only), Tier.COMPLEX,
                     Constraints(require_role=ROLE_REVIEW), {})
    assert [m["model_id"] for m in out] == ["m1"]


def test_require_role_promotes_a_tagged_candidate_over_the_head():
    from core.tier_resolver import Constraints, _survivors

    cands = [
        {"model_id": "author",   "role": None,        "capabilities": {}, "family": "x"},
        {"model_id": "reviewer", "role": ROLE_REVIEW, "capabilities": {}, "family": "x"},
    ]
    out = _survivors(cands, Tier.COMPLEX, Constraints(require_role=ROLE_REVIEW), {})
    assert [m["model_id"] for m in out] == ["reviewer"]


def test_distinct_from_family_IS_a_hard_filter():
    """The asymmetry that makes N10-c necessary. Unlike require_role, this one
    removes candidates — so on a single-family deployment the manifest judge
    gets NoEligibleModel, and the caller has to catch it and proceed rather
    than fail the gate."""
    from core.tier_resolver import Constraints, _reject_reason

    model = {"model_id": "m1", "family": "anthropic", "capabilities": {}, "role": None}
    reason = _reject_reason(model, Tier.COMPLEX,
                            Constraints(distinct_from_family="anthropic"))
    assert reason and "distinct" in reason
