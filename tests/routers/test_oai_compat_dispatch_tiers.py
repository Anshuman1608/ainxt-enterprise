# SPDX-License-Identifier: MIT
"""Phase 6.6 — the OpenAI-compatible endpoint's plain-chat dispatcher.

``gateway.py``'s ``_gateway_stream`` was a SECOND dispatcher. It called
``route()``, then threw the answer away and re-dispatched by hand:

    if decision.tier == TIER_MEDIUM:  gw = _mr._get_openai(); yield from gw.generate(_prompt)
    if decision.tier == TIER_COMPLEX: gw = _mr._get_claude(); yield from gw.generate(_prompt)
    ...
    result = _mr.generate(_prompt, model_hint=_model_hint)   # fall-through

Three things were wrong with it, and only the third is the one §S described:

  * **Under governance every branch was dead.** A resolved tier returns
    ``RoutingDecision(tier=TIER_GOVERNED)`` — the string ``"governed"`` —
    which equals none of the six constants, so control ALWAYS reached the
    fall-through. Consequences nothing recorded: plain chat did not stream
    (one blocking call, yielded whole) and every turn routed twice.
  * **The fall-through passed ``_model_hint``, not ``_route_hint``**, so the
    #33 browser-agent pin was discarded on the one lane it was written for.
  * **The branches that DID match** (``gemini``, and the auto/local case)
    called ``gw.generate(_prompt)`` with no ``model=``, so they ran the
    gateway's ``.env`` default rather than the id just resolved —
    ``ClaudeGateway.generate``'s signature is ``model: str = CLAUDE_MODEL``.

This file asserts the dispatcher is gone rather than mended. Source
assertions, because ``gateway.py`` does not import under pytest — the
weakness step 9 named and this step inherits.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "gateway.py"

# The six the hand-rolled ladder switched on.
LADDER_TIERS = ("TIER_SIMPLE", "TIER_MEDIUM", "TIER_COMPLEX",
                "TIER_VISION", "TIER_GEMINI", "TIER_HAIKU")

# The private gateway accessors it reached for.
GATEWAY_ACCESSORS = ("_get_openai", "_get_claude", "_get_gemini", "_get_local")


@pytest.fixture(scope="module")
def src() -> str:
    return GATEWAY.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def tree(src: str) -> ast.AST:
    return ast.parse(src)


def _fn(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in gateway.py")


# ── The dispatcher is gone ─────────────────────────────────────────────────


def test_no_private_gateway_accessor_survives_in_the_dispatcher(tree):
    """Scoped to the function, not the file: gateway.py:9782 legitimately
    calls _get_gemini() for the CLI image branch, which is a capability the
    router has no entry point for. A blanket rule would report that as the
    defect."""
    fn = _fn(tree, "_gateway_stream")
    found = [f"{n.func.attr}:{n.lineno}" for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") in GATEWAY_ACCESSORS]
    assert found == [], f"_gateway_stream still hand-dispatches: {found}"


def test_the_tier_constants_are_gone_from_the_endpoint(tree):
    """gateway.py:12381 was the LAST non-test application import of
    TIER_VISION / TIER_GEMINI in the repository. While it stood, Phase 8
    could not delete those constants and Phase 10 could not check "exactly 8
    application-facing tiers exist"."""
    fn = _fn(tree, "openai_chat_completions")
    imported = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom) and node.module == "models.model_router":
            imported |= {a.name for a in node.names}
    leftover = imported & set(LADDER_TIERS)
    assert leftover == set(), f"the endpoint still imports {sorted(leftover)}"


def test_no_tier_constant_is_compared_anywhere_in_the_dispatcher(tree):
    """The import going away is not enough on its own — a module-level name
    would still resolve. This asserts the COMPARISONS are gone."""
    fn = _fn(tree, "_gateway_stream")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert not (names & set(LADDER_TIERS)), \
        f"the ladder still switches on {sorted(names & set(LADDER_TIERS))}"


def test_the_blocking_fall_through_is_gone(tree):
    """`_mr.generate(...)` inside a streaming generator is the shape that
    silently turned this endpoint non-streaming. It must not come back, in
    any branch."""
    fn = _fn(tree, "_gateway_stream")
    blocking = [n.lineno for n in ast.walk(fn)
                if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "generate"]
    assert blocking == [], (
        f"_gateway_stream still makes a blocking generate() call at {blocking} — "
        f"its output is yielded whole, which is not streaming")


# ── What replaced it ───────────────────────────────────────────────────────


def test_the_dispatcher_streams_through_the_router(tree):
    fn = _fn(tree, "_gateway_stream")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "stream"]
    assert len(calls) == 1, f"expected exactly one stream() call, found {len(calls)}"
    kwargs = {kw.arg for kw in calls[0].keywords}
    assert "acl_filter" in kwargs, "the dispatch carries no ACL"
    assert any(kw.arg is None and getattr(kw.value, "id", "") == "_oai_route"
               for kw in calls[0].keywords), "the route is not splatted in"


def test_the_dispatcher_strips_the_sentinel(tree):
    """Its caller does `full_answer += token` at the plain-chat branch of
    oai_stream, so a dict escaping here is a TypeError — the same defect the
    CLI relay carried one floor down."""
    fn = _fn(tree, "_gateway_stream")
    assert "__stream_meta__" in ast.dump(fn), \
        "the sentinel is not consumed; it will reach `full_answer += token`"
    assert any(isinstance(n, ast.Call)
               and getattr(n.func, "id", "") == "isinstance"
               and getattr(n.args[1], "id", "") == "dict"
               for n in ast.walk(fn) if isinstance(n, ast.Call) and len(n.args) == 2)


def test_the_browser_agent_pin_is_a_tier_not_a_vendor(src):
    """Fix #33 says "pin to the capable Claude tier". _HINT_MAP already
    agrees that is complex. Naming the vendor made the pin unreachable the
    moment the tier resolved to anything else."""
    assert '_oai_route = _tier_request(_Tier.COMPLEX, "claude")' in src


def test_the_second_hint_variable_is_gone(tree):
    """`_route_hint` held the pin while `_model_hint` was what the dispatch
    actually read. Two variables for one decision is not a style point here
    — it is the mechanism by which the pin was computed, logged and dropped.
    Re-introducing one is the regression."""
    fn = _fn(tree, "_gateway_stream")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "_route_hint" not in names


def test_the_turn_resolves_exactly_once(tree):
    """THE regression this file exists for, in its general form.

    The old code resolved twice: `route()` for the log and `_meta`, then
    `generate()` for the answer — and it passed a DIFFERENT hint to each, so
    the #33 pin was computed, logged and discarded. Two resolutions is not
    only a chance to disagree, it is a real cost: on an Auto turn `route()`
    runs the complexity classifier, so the turn paid for two.

    stream() resolves and records the selection itself, so an explicit
    route() call beside it is by definition the second one.
    """
    fn = _fn(tree, "_gateway_stream")
    resolvers = [n.func.attr for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") in ("route", "generate", "stream")]
    assert resolvers == ["stream"], (
        f"_gateway_stream resolves the turn {len(resolvers)} times ({resolvers}); "
        f"stream() already routes and records the selection")


def test_the_routing_decision_is_read_back_off_the_sentinel(src):
    """With route() gone, the log line and _meta["model"] have to come from
    somewhere. The sentinel is the honest source: it reports the model that
    actually answered, where the old pre-dispatch route() reported one the
    fall-through then re-resolved."""
    assert '_sm_ide.get("model_id") or _sm_ide.get("model_label")' in src


def test_an_explicit_pick_rides_as_itself_not_as_a_hint(src):
    """§G, corrected by D81.

    The old assertion here was `{"model_hint": _model_hint}` — the user's hint
    is passed through, only replaced on the no-hint browser-agent path. True,
    and it was certifying the defect: `_model_hint` is the output of a prefix
    match, so a concrete id never reached the router as an id. An exact enabled
    registry id now goes through untranslated.

    Behaviour is covered by tests/routers/test_oai_explicit_pick.py; this is
    the source-level guard that the dispatch point reads it.
    """
    assert '_oai_route: dict = {"model_hint": _explicit_id or _model_hint}' in src
    assert "_explicit_id = _oai_explicit_model_id(req.model)" in src


# ── The ACL ────────────────────────────────────────────────────────────────


def test_the_endpoint_resolves_a_department(src):
    """acl_filter_for(user, "") applies USER-level rules only and lets every
    department rule through — so resolving the user without the department
    would have looked wired up and enforced half the policy."""
    assert '_oai_user_dept = _payload.get("department", "") or ""' in src
    assert '_oai_user_dept = _kp.get("department", "") or ""' in src


def test_the_department_comes_from_the_profile_not_the_jwt(tree):
    """The DAST fix removed all PII from the JWT, so there IS no "department"
    claim — reading it returns "" on every request and the ACL silently
    enforces nothing. /ask calls enrich_user_context() for exactly this
    reason. Caught by a field check, not by reading: the first version of
    this endpoint's ACL read the claim, and a department rule denying every
    candidate failed to block the turn."""
    fn = _fn(tree, "openai_chat_completions")
    enriched = [n for n in ast.walk(fn)
                if isinstance(n, ast.ImportFrom)
                and n.module == "auth.dependencies"
                and any(a.name == "enrich_user_context" for a in n.names)]
    assert enriched, (
        "the department is read straight off the JWT payload, where the DAST "
        "fix guarantees it is absent — the ACL would enforce nothing")


def test_the_acl_is_auto_only_and_fails_open(src):
    assert ("_oai_acl = None\n"
            "        if not _model_hint:") in src
    assert "[governance/ide] ACL filter unavailable (fail-open)" in src


def test_a_blocked_policy_is_reported_not_swallowed(tree):
    """ModelsBlockedByPolicy must be caught BEFORE the bare Exception handler.
    Below it, a governance denial would be logged as "gateway stream failed"
    and answered with "Error generating response" — indistinguishable from an
    outage."""
    fn = _fn(tree, "_gateway_stream")
    handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    names = [ast.dump(h.type) if h.type else "bare" for h in handlers]
    idx_blocked = next((i for i, n in enumerate(names) if "ModelsBlockedByPolicy" in n), None)
    idx_generic = next((i for i, n in enumerate(names) if "'Exception'" in n), None)
    assert idx_blocked is not None, "a governance denial is not distinguished from a crash"
    assert idx_generic is None or idx_blocked < idx_generic, \
        "ModelsBlockedByPolicy is caught after the generic handler, so it never fires"
