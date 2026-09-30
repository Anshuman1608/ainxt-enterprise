# SPDX-License-Identifier: MIT
"""§N.1 step 10 (D41) — the ReAct engine, the last tier literals outside SDLC.

`routers/threads_router.py` has been on `_PHASE6_MIGRATED_MODULES` since step
4, and for six steps it went on passing `synthesis_hint="solution"` and
`iteration_hint="complex"` into `agents/react_engine.py`. The ratchet never
said a word, because it read `model_hint=` keywords and these are neither —
they arrive on a CONSTRUCTOR, under different names.

That is the same blind spot as D33's splat form, one keyword over, and it is
why this module is in an "SDLC" step: teaching the ratchet to read `hint=` so
it could guard step 10's own `_llm(prompt, hint=…)` convention necessarily
made these two visible. Leaving them would have meant a red build or a
narrower ratchet.

Discussion-thread synthesis is not SDLC and not a review gate, so both routes
ask for `complex` plain — §M.3a keeps the reviewer a role within that tier,
and neither of these calls is one.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from core.tiers import Tier

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_the_engine_takes_routes_not_hints():
    from agents.react_engine import ReactEngine

    params = inspect.signature(ReactEngine.__init__).parameters
    assert "synthesis_route" in params and "iteration_route" in params
    assert "synthesis_hint" not in params and "iteration_hint" not in params


def test_the_defaults_are_tier_requests_carrying_their_legacy_hints():
    """D15/D50. The default has to reproduce where the string sent the call,
    or turning governance off moves discussion synthesis without anyone
    asking for it."""
    from agents.react_engine import ReactEngine

    engine = ReactEngine(task="t", retrieve_fn=lambda q: [])
    assert engine.synthesis_route == {"tier": Tier.COMPLEX, "legacy_hint": "solution"}
    assert engine.iteration_route == {"tier": Tier.COMPLEX, "legacy_hint": "complex"}


def test_an_explicit_route_is_honoured():
    from agents.react_engine import ReactEngine

    engine = ReactEngine(task="t", retrieve_fn=lambda q: [],
                         synthesis_route={"model_hint": "gpt-5.5"})
    assert engine.synthesis_route == {"model_hint": "gpt-5.5"}
    # The other one still defaults rather than becoming None.
    assert engine.iteration_route["tier"] is Tier.COMPLEX


def test_the_engine_splats_its_routes_into_every_dispatch():
    """Three call sites — analysis, critique, synthesis. A route stored and
    not passed is the failure this asserts against, and it would look exactly
    like working code."""
    src = (ROOT / "agents" / "react_engine.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    dispatches, splatted = 0, 0
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "generate"):
            dispatches += 1
            if any(kw.arg is None
                   and "_route" in ast.dump(kw.value)
                   for kw in node.keywords):
                splatted += 1
    assert dispatches == 3, f"expected 3 generate() calls, found {dispatches}"
    assert splatted == 3, f"only {splatted} of {dispatches} pass a route"


def test_the_caller_asks_for_tiers():
    src = (ROOT / "routers" / "threads_router.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "ReactEngine"]
    assert len(calls) == 1, "expected exactly one ReactEngine construction"
    kwargs = {k.arg for k in calls[0].keywords}
    assert {"synthesis_route", "iteration_route"} <= kwargs
    assert not {"synthesis_hint", "iteration_hint"} & kwargs


def test_the_ratchet_now_sees_this_shape():
    """The point of the whole exercise. Reverting either keyword must be a
    failing build — before step 10 it was not, in a module the check already
    claimed to be guarding."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_rc10", ROOT / "scripts" / "ci" / "release_checks.py")
    rc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rc)

    assert "routers/threads_router.py" in rc._PHASE6_MIGRATED_MODULES
    assert "agents/react_engine.py" in rc._PHASE6_MIGRATED_MODULES
    assert "ReactEngine" in rc._ROUTER_ENTRY_POINTS
    assert {"synthesis_hint", "iteration_hint", "hint"} <= rc._ROUTING_KEYWORDS


def test_model_used_still_reports_something_when_the_router_is_silent():
    """`last_model_label` used to fall back to the hint STRING, which was at
    least a word. With the hint gone the fallback has to come from the route,
    or the thread's recorded model becomes None on any path where the router
    never set a label."""
    from models.model_router import route_label

    assert route_label({"tier": Tier.COMPLEX, "legacy_hint": "solution"}) == "complex"
    assert route_label({"model_hint": "gpt-5.5"}) == "gpt-5.5"
    assert route_label({}) == "auto"
