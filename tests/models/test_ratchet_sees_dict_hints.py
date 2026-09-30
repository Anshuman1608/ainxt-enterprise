# SPDX-License-Identifier: MIT
"""D33 — the tier-migration ratchet can see the idiom the migration uses.

scripts/ci/release_checks.py::check_tier_migration is what stops §N.1 steps
1-8 from quietly regressing. It inspected `model_hint=` KEYWORDS on router
entry points — and step 6 introduced a second idiom, because "resolve a tier"
and "use this hint" cannot both be expressed as one string:

    _route = {"model_hint": "complex"}
    model_router.generate(prompt, **_route)

A splatted local is not a keyword, so the check read ZERO hits on the whole
idiom. A ratchet blind to the shape the code is written in is not ratcheting
anything.

The dict form is flagged wherever the LITERAL appears rather than at the call,
because following the variable would need dataflow analysis and the pair
`{"model_hint": "<tier>"}` is indefensible anywhere in a module that has been
migrated.

CORRECTION (§N.1 step 9). Step 8 shipped this check and claimed it now
guarded `agents/tools.py:539`. It did not: that line was
`{"model_hint": _complexity}`, a NAME, and the sweep only reads Constants.
Reverting step 9 in exactly that form still passes the ratchet — measured, not
assumed. The variable form is guarded by source assertions in tests/agents/
instead, and the boundary is pinned by
test_the_dict_sweep_sees_LITERALS_only_and_that_is_the_limit below, along with
the reason it cannot simply be widened.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _release_checks():
    spec = importlib.util.spec_from_file_location(
        "_rc", ROOT / "scripts" / "ci" / "release_checks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def rc():
    return _release_checks()


def _run_on(rc, tmp_path, monkeypatch, source: str) -> list[str]:
    """Run check_tier_migration over one synthetic module."""
    mod = tmp_path / "fake_migrated.py"
    mod.write_text(source, encoding="utf-8")
    monkeypatch.setattr(rc, "ROOT", tmp_path)
    monkeypatch.setattr(rc, "_PHASE6_MIGRATED_MODULES", ("fake_migrated.py",))
    return rc.check_tier_migration(None)


def test_the_keyword_form_is_still_flagged(rc, tmp_path, monkeypatch):
    out = _run_on(rc, tmp_path, monkeypatch,
                  'model_router.generate(p, model_hint="complex")\n')
    assert len(out) == 1 and "'complex'" in out[0]


def test_the_splat_form_is_flagged(rc, tmp_path, monkeypatch):
    """The regression D33 exists for: a developer "migrates" a call site by
    moving the literal into a dict, and the check used to go quiet."""
    out = _run_on(rc, tmp_path, monkeypatch,
                  '_route = {"model_hint": "simple"}\n'
                  'model_router.generate(p, **_route)\n')
    assert len(out) == 1 and "'simple'" in out[0]


def test_the_inline_splat_form_is_flagged(rc, tmp_path, monkeypatch):
    out = _run_on(rc, tmp_path, monkeypatch,
                  'model_router.stream(p, **{"model_hint": "haiku"})\n')
    assert len(out) == 1 and "'haiku'" in out[0]


def test_a_real_model_id_in_a_dict_is_still_legal(rc, tmp_path, monkeypatch):
    """A raw model id is a user-explicit pick and stays legal forever — that
    distinction is the whole reason the check matches against a tier list
    rather than against the presence of the key."""
    assert _run_on(rc, tmp_path, monkeypatch,
                   '_route = {"model_hint": "claude-opus-5"}\n') == []


def test_a_variable_value_in_a_dict_is_not_flagged(rc, tmp_path, monkeypatch):
    """`{"model_hint": ENRICH_MODEL}` is how an operator pin is forwarded
    (workers/index_worker.py). Flagging it would report every migrated module
    for doing the right thing."""
    assert _run_on(rc, tmp_path, monkeypatch,
                   '_route = {"model_hint": ENRICH_MODEL}\n') == []


def test_a_quoted_hint_in_a_comment_or_docstring_is_not_flagged(rc, tmp_path, monkeypatch):
    """Migrated modules deliberately carry the old literals in prose — 'this
    used to be model_hint="complex"' is exactly what the next reader needs.
    This is why the check parses instead of grepping, and it is the trap that
    has now caught three tests in this migration."""
    assert _run_on(rc, tmp_path, monkeypatch,
                   '"""Was model_hint="complex" before §N.1 step 8."""\n'
                   '# and {"model_hint": "simple"} before that\n'
                   'x = 1\n') == []


def test_the_real_tree_is_clean(rc):
    """The ratchet must pass on the tree as it stands, or the widened check is
    reporting the migration rather than guarding it."""
    assert rc.check_tier_migration(None) == []


# ── The line this check was widened for ───────────────────────────────────
#
# These two asserted the pre-step-9 state: that agents/tools.py still carried
# the splat idiom and was NOT yet in the migrated list. Step 9 landed, so they
# assert the other side of the same fact. They stay here rather than moving to
# the step 9 test file because the point they make is about THIS check — the
# reason it was taught to read ast.Dict at all was that it could not see this
# one line, and that is worth keeping next to the check's own tests.


def test_the_chat_auto_classifier_branch_now_asks_for_the_tier():
    """§N.1 step 9. The splat idiom is gone from the branch the ratchet was
    widened to see, replaced by the shared complexity → tier mapping."""
    src = (ROOT / "agents" / "tools.py").read_text(encoding="utf-8")
    assert '_route_kwargs = _chat_complexity_route(_complexity)' in src
    assert '_route_kwargs = {"model_hint": _complexity}' not in src


def test_the_dict_sweep_sees_LITERALS_only_and_that_is_the_limit(
        rc, tmp_path, monkeypatch):
    """A correction to what §N.1 step 8 claimed for D33.

    Step 8's commit said the widened check "can now see
    {"model_hint": "<tier>"} dict literals" and that this guarded
    agents/tools.py:539. The first half is true; the second is not, and this
    test exists so nobody relies on the difference again.

    The check flags a dict VALUE only when it is an ast.Constant in
    _PHASE6_TIER_HINTS. agents/tools.py:539 was
    `{"model_hint": _complexity}` — a Name — so reverting step 9 in that exact
    form still passes the ratchet. Verified by reverting it: the ratchet said
    ok and five source assertions failed.

    It CANNOT be widened to "any dict with a model_hint key", because the
    legitimate user-pick passthroughs have exactly that shape
    (`{"model_hint": state.model_hint}` two lines above the migrated branch,
    workers/chat_worker.py::_kb_answer_route, routers/kb_ask_router.py::
    _kb_route_for, and workers/doc_worker.py's local: handling). Telling a
    classifier verdict apart from a user's pick is a judgement about meaning,
    not shape — so the variable form is guarded by the source assertions in
    tests/agents/, and this ratchet's remit stops at literals.
    """
    # A literal IS caught, in the splat form step 6 introduced…
    assert _run_on(rc, tmp_path, monkeypatch, 'x = {"model_hint": "complex"}\n')
    # …and a variable is NOT, in the same form.
    assert not _run_on(rc, tmp_path, monkeypatch, 'x = {"model_hint": _complexity}\n')
    # The legitimate shape that makes widening impossible.
    assert not _run_on(rc, tmp_path, monkeypatch,
                       'x = {"model_hint": state.model_hint}\n')


def test_agents_tools_is_now_in_the_migrated_list(rc):
    """The corollary, and the part that makes the widening load-bearing: with
    agents/tools.py listed, a revert to `{"model_hint": _complexity}` is a
    failing build rather than a silent regression. Before step 9 the file was
    deliberately absent from the list; leaving it absent now would mean the
    dict-literal sweep guards nothing in the file it was written for."""
    assert "agents/tools.py" in rc._PHASE6_MIGRATED_MODULES
