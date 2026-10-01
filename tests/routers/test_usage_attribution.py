# SPDX-License-Identifier: MIT
"""A model label must name the model that ran, or say it does not know.

Two sites defaulted to a SKU constant when the real label was unavailable:

* ``agents/sdlc_state_machine.py`` stamped every PR description footer with
  ``CLAUDE_PRIMARY_MODEL`` — a published claim about who wrote the code that
  was true only by coincidence.
* ``routers/projects_router.py`` defaulted the usage record to
  ``OPENAI_CODING_MODEL``, which then keyed ``MODEL_COST_PER_1M``, so an
  unidentified run was billed at that model's rate.

Both constants are deleted in Phase 8 and would then evaluate to ``""`` —
an empty author line, and ``.get("", (2.00, 8.00))`` billing every
unidentified run at the guessed default.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


# ── the SDLC run's model set ────────────────────────────────────────────────

@pytest.fixture
def sm():
    import agents.sdlc_state_machine as m
    m.reset_models_used()
    yield m
    m.reset_models_used()


def test_models_used_starts_empty_and_accumulates(sm):
    assert sm.models_used() == []
    sm._note_model_used("claude-haiku-4-5")
    sm._note_model_used("gemini-3-flash")
    sm._note_model_used("claude-haiku-4-5")      # de-duplicated
    assert sm.models_used() == ["claude-haiku-4-5", "gemini-3-flash"]


@pytest.mark.parametrize("junk", ["", "   ", "auto", "unknown", "UNKNOWN"])
def test_a_non_answer_is_not_recorded_as_a_model(sm, junk):
    """dispatched_model_id returns its fallback for these; recording them
    would put 'auto' in a PR description as though it were a model."""
    sm._note_model_used(junk)
    assert sm.models_used() == []


def test_reset_clears_between_runs(sm):
    sm._note_model_used("claude-haiku-4-5")
    sm.reset_models_used()
    assert sm.models_used() == []


def test_the_contextvar_does_not_leak_across_threads(sm):
    import threading

    sm._note_model_used("claude-haiku-4-5")
    seen = {}

    def _other():
        seen["models"] = sm.models_used()

    t = threading.Thread(target=_other)
    t.start()
    t.join()
    # A fresh thread starts from the ContextVar default, so one run's models
    # can never be attributed to another's.
    assert seen["models"] == []


# ── the PR description footer ───────────────────────────────────────────────

def _pr_description(monkeypatch, models):
    import agents.sdlc_state_machine as m

    monkeypatch.setattr(m, "get_run", lambda _rid: {"context": {}})
    m.reset_models_used()
    for mid in models:
        m._note_model_used(mid)

    sm = m.CodingStateMachine.__new__(m.CodingStateMachine)
    sm.run_id = "run-1"
    sm.jira_key = "AINXT-1"
    sm.design = {"solution_approach": "do the thing"}
    sm.code_output = {"summary": "did the thing", "files": []}
    sm.slt_output = {}
    sm._manifest_update_url = ""
    sm._sibling_mr_urls = []
    try:
        return sm._build_pr_description()
    finally:
        m.reset_models_used()


def test_the_footer_names_the_models_that_actually_ran(monkeypatch):
    body = _pr_description(monkeypatch, ["claude-haiku-4-5", "gemini-3-flash"])
    assert "AiNxt AI Coding Agent" in body
    assert "claude-haiku-4-5" in body and "gemini-3-flash" in body


def test_the_footer_claims_no_author_when_none_is_known(monkeypatch):
    """Better than naming a model that did not run, and better than the
    trailing '· ' that the deleted constant would have left behind."""
    body = _pr_description(monkeypatch, [])
    assert "AiNxt AI Coding Agent" in body
    assert "· _" not in body
    assert "**AiNxt AI Coding Agent**_" in body


# ── neither module reaches a model by constant ──────────────────────────────

@pytest.mark.parametrize("rel", [
    "agents/sdlc_state_machine.py",
    "routers/projects_router.py",
    "routers/threads_router.py",
])
def test_no_sku_constant_is_imported(rel):
    from tests.core.test_model_constant_importers import GUARDED

    src = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
    named = {
        a.name
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.ImportFrom)
        and (n.module or "").endswith("model_registry")
        for a in n.names
    } & GUARDED
    assert not named, f"{rel} reaches a model by constant again: {named}"


def test_an_unidentified_project_run_is_not_billed_at_a_guessed_rate():
    """The cost authority prices an unknown id at a conservative default, so
    an unidentified run must short-circuit to zero before it gets there."""
    from routers.projects_router import _project_ask_cost

    for label in ("", "unknown", "UNKNOWN", None):
        assert _project_ask_cost(label, 1_000_000, 1_000_000) == 0.0, label
    src = (ROOT / "routers" / "projects_router.py").read_text(
        encoding="utf-8", errors="replace")
    assert "_OPENAI_CODING" not in src
