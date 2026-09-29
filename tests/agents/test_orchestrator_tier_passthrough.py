# SPDX-License-Identifier: MIT
"""Phase 6.5 item 1 — the orchestrator accepts a tier, and one caller uses it.

§N.1 step 4 migrated `workers/cowork_task_worker.py` except for a single line:
it called `agent.run(..., model_hint="complex")`, and `run()` had its own
`model_hint` parameter with no tier alongside it. There was no local fix — the
caller could not ask for a tier that the callee would not accept — so the CI
ratchet in scripts/ci/release_checks.py deliberately exempted `run()` and the
call site stayed on the legacy hint.

What makes this more than a rename is that `run()`'s `model_hint` carries TWO
unrelated meanings depending on who calls it:

    gateway.py:10081                 → the user's dropdown pick
    workers/cowork_task_worker.py    → the literal "complex", a tier request

Only the second is a routing decision this migration owns. So `tier` is added
ALONGSIDE `model_hint` rather than replacing it, and the precedence between
them has to be explicit: an explicit user pick outranks a tier, and a tier
outranks the classifier's guess.

`agents/orchestrator.py` cannot be imported in a bare test env (core.logger
pulls in structlog), which is why tests/agents/test_connector_first_routing.py
reads it as source. Same approach here: AST over the real files, so the
assertions cannot drift from a hand-copied mirror.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _tree(rel: str) -> ast.Module:
    return ast.parse((ROOT / rel).read_text(encoding="utf-8", errors="ignore"))


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found — did it move or get renamed?")


# ── run() accepts a tier, keyword-only, next to model_hint ────────────────


def test_run_accepts_tier_and_legacy_hint():
    run = _func(_tree("agents/orchestrator.py"), "run")
    kwonly = {a.arg for a in run.args.kwonlyargs}
    assert {"tier", "legacy_hint"} <= kwonly, (
        "OrchestratorAgent.run() must accept tier= and legacy_hint= as "
        "KEYWORD-ONLY arguments, for the same reason ModelRouter's six entry "
        f"points do — got keyword-only args {sorted(kwonly)}")


def test_model_hint_survives_alongside_the_tier():
    """The gateway passes the user's own model choice through this parameter.
    Replacing it with `tier` would have silently turned every explicit user
    pick into a governed tier request."""
    run = _func(_tree("agents/orchestrator.py"), "run")
    positional = {a.arg for a in run.args.args}
    assert "model_hint" in positional


def test_run_refuses_tier_and_model_hint_together():
    """They mean opposite things — "the user chose this" versus "the task needs
    this" — so preferring one by argument order would be a routing decision
    with nothing in the log to find later. Mirrors _coerce_tier, which raises
    for the same pair."""
    src = (ROOT / "agents" / "orchestrator.py").read_text(encoding="utf-8", errors="ignore")
    assert "pass either tier= or model_hint=, not " in src
    assert "legacy_hint= requires tier=" in src


def test_agent_state_carries_both():
    """A parameter run() accepts but does not store reaches nothing: the hint
    is consumed from AgentState by agents/tools.py, not from the signature."""
    fields = {
        t.target.id
        for node in ast.walk(_tree("agents/state.py"))
        if isinstance(node, ast.ClassDef) and node.name == "AgentState"
        for t in node.body
        if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)
    }
    assert {"model_hint", "tier", "legacy_hint"} <= fields, (
        f"AgentState is missing a routing field — got {sorted(fields)}")


def test_run_forwards_both_into_agent_state():
    src = (ROOT / "agents" / "orchestrator.py").read_text(encoding="utf-8", errors="ignore")
    for node in ast.walk(_tree("agents/orchestrator.py")):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "AgentState"):
            kwargs = {k.arg for k in node.keywords}
            if "model_hint" in kwargs:
                assert {"tier", "legacy_hint"} <= kwargs, (
                    f"agents/orchestrator.py:{node.lineno}: AgentState(...) is "
                    f"given model_hint but not tier/legacy_hint, so run()'s new "
                    f"arguments are accepted and then dropped")
                return
    raise AssertionError("no AgentState(model_hint=...) construction found")


# ── The generation path honours it, in the right order ────────────────────


def test_the_generation_call_routes_on_whichever_source_is_set():
    """agents/tools.py has exactly ONE model_router.stream() call on the answer
    path. It used to be `model_hint=_hint` where
    `_hint = state.model_hint or _complexity`; a tier had nowhere to go.
    """
    src = (ROOT / "agents" / "tools.py").read_text(encoding="utf-8", errors="ignore")
    assert 'model_router.stream(_stream_payload, **_route_kwargs)' in src, (
        "agents/tools.py no longer dispatches through a kwargs mapping — a "
        "tier passed to run() would be accepted and then ignored")
    assert '_route_kwargs = {"tier": state.tier,' in src


def test_the_precedence_is_user_pick_then_tier_then_classifier():
    """Order is the whole design: each source overrides the next because it
    carries more information about what the caller wants. Asserted by source
    position rather than by behaviour because tools.py is not importable
    either (it pulls in the retriever and the model router)."""
    src = (ROOT / "agents" / "tools.py").read_text(encoding="utf-8", errors="ignore")
    i_hint = src.index('if getattr(state, "model_hint", None):')
    i_tier = src.index('elif getattr(state, "tier", None) is not None:')
    i_cls = src.index('_route_kwargs = {"model_hint": _complexity}')
    assert i_hint < i_tier < i_cls


def test_the_classifier_branch_is_still_the_legacy_hint():
    """Migrating the complexity→hint mapping is §N.1 step 9, not this item.
    If this assertion starts failing because the branch now passes a tier,
    that is step 9 landing — and it needs its own before/after evidence on a
    fixed query set, not a quiet ride along with item 1."""
    src = (ROOT / "agents" / "tools.py").read_text(encoding="utf-8", errors="ignore")
    assert '_route_kwargs = {"model_hint": _complexity}' in src


# ── The call site that was left behind ────────────────────────────────────


def test_the_cowork_worker_now_asks_for_the_tier():
    tree = _tree("workers/cowork_task_worker.py")
    runs = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "run"
    ]
    assert runs, "no agent.run(...) call found — did the worker change shape?"

    migrated = []
    for node in runs:
        kwargs = {k.arg for k in node.keywords}
        assert "model_hint" not in kwargs, (
            f"workers/cowork_task_worker.py:{node.lineno}: still passes "
            f"model_hint= — this was the one call site §N.1 step 4 could not "
            f"finish, and run() now accepts a tier")
        if "tier" in kwargs:
            assert "legacy_hint" in kwargs, (
                f"workers/cowork_task_worker.py:{node.lineno}: tier= without "
                f"legacy_hint= — D15's flag-off guarantee needs both")
            migrated.append(node)
    assert migrated, "the worker's agent.run(...) does not pass a tier at all"


def test_the_ci_ratchet_now_covers_run():
    """The check exempted `run()` while it had no tier parameter, because
    flagging a caller for a gap in its callee gives the reader no local fix.
    That exemption is what let this call site sit unmigrated through Phase 6;
    closing it is what stops the next one doing the same."""
    src = (ROOT / "scripts" / "ci" / "release_checks.py").read_text(
        encoding="utf-8", errors="ignore")
    entry_points = next(
        node for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_ROUTER_ENTRY_POINTS"
                for t in node.targets)
    )
    names = {
        e.value for e in ast.walk(entry_points)
        if isinstance(e, ast.Constant) and isinstance(e.value, str)
    }
    assert "run" in names, (
        "_ROUTER_ENTRY_POINTS no longer includes 'run', so a module could go "
        "back to agent.run(model_hint=\"complex\") without CI noticing")


@pytest.mark.parametrize("path", [
    "workers/cowork_task_worker.py",
    "agents/orchestrator.py",
])
def test_the_files_still_parse(path):
    """Cheap guard: every assertion above reads these two as source, so a
    syntax error would show up as a confusing AssertionError elsewhere."""
    _tree(path)
