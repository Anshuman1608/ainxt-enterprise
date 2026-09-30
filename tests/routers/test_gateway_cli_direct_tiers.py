# SPDX-License-Identifier: MIT
"""Phase 6.6 — the CLI direct path in ``/ask``.

``gateway.py``'s ``_cli_direct_stream`` is the relay the ``ainxt`` CLI talks
to: no orchestrator, no RAG, just a model. §N.1 step 9 left it out by name
(D34) and §S recorded it under "Not yet migrated" without assigning it a step.

Two separate things are asserted here, and they are kept separate on purpose
because they landed as two commits and either can be reverted alone:

  1. **The sentinel guard.** ``model_router.stream()`` always yields exactly
     one trailing ``{"__stream_meta__": …}`` dict. This loop consumed it with
     ``_full += _tok`` — a ``TypeError`` raised after the last real token,
     swallowed by the surrounding handler, which appended an ``[Error: …]``
     chunk to every CLI turn and skipped the ``_cmeta`` block entirely, so no
     CLI turn ever recorded its model, tokens or cost. The other two
     ``stream()`` callers in the file have always guarded it.

  2. **The tier request.** The two PLATFORM defaults — "complex" when the user
     has not picked, "mini" for a trivial greeting — become tier requests. An
     explicit ``_model_hint`` is the USER's pick and stays a hint (§G).

Assertions are over the source. ``gateway.py`` is ~16k lines with ~80 routers
attached at module scope and does not import under pytest, which is the same
constraint — and the same weakness — step 9 recorded for
``test_chat_auto_tier_mapping.py``.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "gateway.py"


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


def _ask_ai(tree: ast.AST):
    return _fn(tree, "ask_ai")


# ── 1. The sentinel guard ──────────────────────────────────────────────────


def test_the_cli_stream_loop_guards_the_sentinel(tree):
    """The bug this catches is invisible in normal reading: the dict is
    TRUTHY, so `if _tok:` passes and the concatenation one line later is what
    raises."""
    fn = _fn(tree, "_cli_direct_stream")
    guards = [n for n in ast.walk(fn)
              if isinstance(n, ast.Call)
              and getattr(n.func, "id", "") == "isinstance"
              and len(n.args) == 2
              and getattr(n.args[1], "id", "") == "dict"]
    assert guards, (
        "_cli_direct_stream does not check isinstance(tok, dict) — "
        "stream()'s trailing __stream_meta__ sentinel will reach `_full += _tok`")


def test_every_router_stream_caller_in_the_file_guards_it(tree):
    """Scoped to the whole module rather than this one function: the defect
    was that ONE of three callers had been written without the guard, and a
    test on the one we fixed would not notice a fourth being added."""
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "stream"
                and "model_router" not in ast.dump(node.func)):
            continue
        # Only the ModelRouter instances — httpx.stream and the OpenAI
        # responses stream are different APIs with no sentinel.
        recv = getattr(node.func, "value", None)
        if not isinstance(recv, ast.Name) or not recv.id.startswith("_mr"):
            continue
        enclosing = None
        for cand in ast.walk(tree):
            if (isinstance(cand, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and cand.lineno <= node.lineno <= (cand.end_lineno or cand.lineno)):
                if enclosing is None or cand.lineno > enclosing.lineno:
                    enclosing = cand
        dumped = ast.dump(enclosing) if enclosing else ""
        if "__stream_meta__" not in dumped:
            offenders.append(f"{enclosing.name if enclosing else '?'}:{node.lineno}")
    assert offenders == [], (
        f"these model_router.stream() callers never read the sentinel: {offenders}")


def test_the_meta_block_prefers_the_sentinel_over_thread_locals(src):
    """Not cosmetic. The sentinel exists because uvicorn's threadpool can
    resume the generator on a different worker between the final
    _propagate_tokens write and this read — the race gateway.py:8102 already
    documents for the /ask fast path."""
    assert "_cli_meta.get(\"in_tok\")" in src
    assert "_cli_meta.get(\"out_tok\")" in src
    assert "_cli_meta.get(\"model_id\")" in src


# ── 2. The tier request ────────────────────────────────────────────────────


def test_the_cli_defaults_are_tier_requests(src):
    assert "_cli_tier: \"object | None\" = None if _model_hint else _Tier.COMPLEX" in src
    assert "_cli_tier = _Tier.MINI" in src


def test_the_hint_string_survives(src):
    """_hint_for_stream has readers that are not the dispatch — the log line
    and the vocabulary /model speaks — so it stays a str and the tier travels
    beside it. Step 9 made the same call for _fp_hint, for the same reason."""
    assert "_hint_for_stream = _model_hint or \"complex\"" in src
    assert "_hint_for_stream = \"mini\"" in src


def test_an_explicit_pick_is_not_given_a_tier(src):
    """§G. The user typed /model; governance does not second-guess it. The
    conditional has to read this way round — `if _cli_tier is not None` — so
    that a pick produces a plain hint and not tier_request(None, …)."""
    assert ("_cli_route = (_tier_request(_cli_tier, _hint_for_stream)\n"
            "                      if _cli_tier is not None\n"
            "                      else {\"model_hint\": _hint_for_stream})") in src


def test_the_dispatch_splats_the_route_and_passes_no_hint(tree):
    """A route computed and not passed is the failure mode here, and it would
    look exactly like working code — the old `model_hint=_hint_for_stream`
    keyword still resolves something."""
    fn = _fn(tree, "_cli_direct_stream")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "stream"]
    assert len(calls) == 1, f"expected one stream() call, found {len(calls)}"
    call = calls[0]
    assert any(kw.arg is None and getattr(kw.value, "id", "") == "_cli_route"
               for kw in call.keywords), "the route is not splatted into stream()"
    assert not any(kw.arg == "model_hint" for kw in call.keywords), \
        "the dispatch still passes model_hint= alongside the route"


def test_the_route_is_resolved_before_the_generator(src, tree):
    """It has to be: the governance pre-flight below reads the tier, and a
    value computed inside the generator is not available until the first
    token is pulled — by which time the response headers are sent and a 403
    is no longer possible."""
    ask = _ask_ai(tree)
    gen = _fn(tree, "_cli_direct_stream")
    route_lines = [n.lineno for n in ast.walk(ask)
                   if isinstance(n, ast.Assign)
                   and any(getattr(t, "id", "") == "_cli_route" for t in n.targets)]
    assert route_lines, "_cli_route is never assigned"
    assert min(route_lines) < gen.lineno, (
        "_cli_route is computed inside the generator, so the 403 pre-flight "
        "cannot see the tier")


# ── 3. The ACL ─────────────────────────────────────────────────────────────


def test_the_cli_path_now_carries_an_acl(tree):
    fn = _fn(tree, "_cli_direct_stream")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "stream"]
    assert any(kw.arg == "acl_filter" for kw in calls[0].keywords), (
        "the CLI relay dispatches without an ACL — a department rule does not "
        "apply to any CLI turn")


def test_the_acl_is_only_applied_to_an_auto_turn(src):
    """§G again, and the same shape gateway.py:7666 uses on the fast path: a
    user who named a model has made an explicit request, and the ACL on the
    tier's candidate list has nothing to say about it."""
    assert ("_cli_acl = None\n"
            "        if not _model_hint:") in src


def test_a_fully_blocked_tier_is_a_403_before_streaming_starts(src):
    """Not an exception mid-SSE. The client has already begun rendering by
    then, which is the reason fully_blocked_candidates exists at all."""
    assert "_fp_fully_blocked(_cli_tier, _cli_acl)" in src
    assert "GOVERNANCE_NO_MODEL_AVAILABLE" in src


def test_the_acl_fails_open(src):
    """Governance must never take chat down. A DB outage that turned every
    CLI turn into a 500 would be a worse failure than an unenforced rule."""
    assert "[governance/cli] ACL filter unavailable (fail-open)" in src


# ── 4. What must NOT have changed ──────────────────────────────────────────


def test_the_trivial_query_regex_is_untouched(src):
    """The downgrade's TRIGGER is out of scope — only its destination moved
    from a vendor SKU to a tier. Widening the regex here would change which
    turns get the cheap model, which is a different decision needing
    different evidence."""
    assert r"^(hi+|hello+|hey+|thanks?|thank\s+you|bye+|" in src


def test_the_image_branch_still_bypasses_the_text_route(src):
    """The CLI vision path goes through the proxy's /llm/generate-image, not
    through stream(), and _vision_handled is what keeps the two apart."""
    assert "if _vision_handled:" in src
    assert "generate_image via proxy failed" in src
