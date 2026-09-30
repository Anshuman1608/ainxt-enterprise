# SPDX-License-Identifier: MIT
"""D33 — the tier-migration ratchet can see the idiom the migration uses.

scripts/ci/release_checks.py::check_tier_migration is what stops §N.1 steps
1-8 from quietly regressing. It inspected `model_hint=` KEYWORDS on router
entry points — and step 6 introduced a second idiom, because "resolve a tier"
and "use this hint" cannot both be expressed as one string:

    _route = {"model_hint": "complex"}
    model_router.generate(prompt, **_route)

A splatted local is not a keyword, so the check read ZERO hits in
agents/tools.py — the single most important file §N.1 step 9 has to change,
and the one this migration has most carefully left alone. A ratchet blind to
the shape the code is written in is not ratcheting anything.

The dict form is flagged wherever the LITERAL appears rather than at the call,
because following the variable would need dataflow analysis and the pair
`{"model_hint": "<tier>"}` is indefensible anywhere in a module that has been
migrated.
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


# ── The line this migration must not touch ────────────────────────────────


def test_the_chat_auto_classifier_branch_is_still_the_legacy_hint():
    """agents/tools.py:539 is §N.1 step 9's, not step 8's. Asserted HERE as
    well as in tests/agents/test_orchestrator_tier_passthrough.py because
    teaching the ratchet to see the splat idiom is exactly the change that
    might tempt someone to "fix" this line while they are in the area — and
    step 9 needs its own before/after evidence on a fixed query set, not a
    quiet ride along with a CI improvement."""
    src = (ROOT / "agents" / "tools.py").read_text(encoding="utf-8")
    assert '_route_kwargs = {"model_hint": _complexity}' in src


def test_agents_tools_is_not_in_the_migrated_list_yet(rc):
    """The corollary: the ratchet can now see that line's shape, so adding
    agents/tools.py to _PHASE6_MIGRATED_MODULES before step 9 lands would
    make CI fail. It is not there."""
    assert "agents/tools.py" not in rc._PHASE6_MIGRATED_MODULES
