# SPDX-License-Identifier: MIT
"""The SDLC CLI's "error_max_turns" mislabel, and the reason string it produced.

The `ainxt` v3 binary maps ANY non-EndTurn stopReason onto the subtype
``error_max_turns`` (see ``_parse_cli_envelope``), so that subtype cannot tell
"ran out of turns" apart from "was cancelled for some other reason". Two
things were built on top of that ambiguity and both were wrong:

  * ``_is_transient_failure`` treated the case as retryable only when
    ``num_turns <= 2``. That window came from run d2b05274, which was
    cancelled after ONE turn. Run 3f95f90c hit the identical envelope after
    EIGHT of sixty turns, so the heuristic stayed silent, PLAN's two
    configured ``transient_retries`` went unused, and the phase suspended.
  * the suspend reason was ``_reason_for_exit_code(exit_code)``. A v3
    cancellation exits 0, so the operator-facing message — the one written to
    ``sdlc_runs.error`` and shown in the UI — was **"CLI exited with code 0"**.
    True, and it names neither the cause nor anything to do about it.

The envelope in ``REAL_CANCELLED_ENVELOPE`` is copied from run 3f95f90c's
activity stream, so these tests fail if either regression returns.
"""

from __future__ import annotations

import json

import pytest

from agents.sdlc_cli_engine import (
    _is_transient_failure,
    _parse_cli_envelope,
    _reason_for_exit_code,
    _suspend_reason,
)

# Verbatim from /home/appuser/workspaces/cli_logs/3f95f90c…/plan-…ndjson,
# trimmed to the fields the parser reads.
REAL_CANCELLED_ENVELOPE = json.dumps({
    "text": "",
    "stopReason": "Cancelled",
    "num_turns": 8,
    "structuredOutputError": "model did not produce structured output",
    "usage": {"input_tokens": 47426, "output_tokens": 2804},
})

REAL_MAX_TURNS = 60


# ── The envelope is read correctly ──────────────────────────────────────────


def test_the_envelope_still_maps_onto_error_max_turns():
    """Not a defect in itself — downstream IMPLEMENT auto-continue keys on this
    subtype, so the mapping stays. It is the SOLE reliance on it that was
    wrong."""
    r = _parse_cli_envelope(REAL_CANCELLED_ENVELOPE, exit_code=0)
    assert r.subtype == "error_max_turns"
    assert r.is_error is True


def test_the_structured_output_error_is_carried_off_the_envelope():
    """The field that disambiguates. It was parsed by nobody before this."""
    r = _parse_cli_envelope(REAL_CANCELLED_ENVELOPE, exit_code=0)
    assert r.structured_output_error == "model did not produce structured output"
    assert r.num_turns == 8


def test_an_envelope_without_the_field_gets_an_empty_string():
    r = _parse_cli_envelope('{"text":"ok","stopReason":"EndTurn"}', exit_code=0)
    assert r.structured_output_error == ""


# ── The heuristic ───────────────────────────────────────────────────────────


def test_the_real_run_is_now_retryable():
    """The regression, stated as the exact inputs that failed."""
    r = _parse_cli_envelope(REAL_CANCELLED_ENVELOPE, exit_code=0)
    assert _is_transient_failure(
        0, r.subtype, "", num_turns=r.num_turns, max_turns=REAL_MAX_TURNS,
        structured_output_error=r.structured_output_error,
    ) is True


def test_the_old_two_turn_window_would_have_missed_it():
    """Pins WHY this changed rather than just that it did: under the previous
    rule the same envelope was not transient, so nothing retried."""
    r = _parse_cli_envelope(REAL_CANCELLED_ENVELOPE, exit_code=0)
    assert not (0 <= r.num_turns <= 2), (
        "8 turns — the old `num_turns <= 2` window could not see this")


@pytest.mark.parametrize("num_turns", [0, 1, 2, 8, 30, 59])
def test_any_cancellation_below_the_cap_is_retryable(num_turns):
    assert _is_transient_failure(
        0, "error_max_turns", "", num_turns=num_turns, max_turns=60,
        structured_output_error="model did not produce structured output",
    ) is True


def test_a_GENUINE_exhaustion_is_not_retryable():
    """The half that must not move. Reaching the cap is a budget problem, and
    re-spawning to burn the budget again is the wrong answer — IMPLEMENT
    continues the same session for this case instead."""
    assert _is_transient_failure(
        0, "error_max_turns", "", num_turns=60, max_turns=60,
        structured_output_error="model did not produce structured output",
    ) is False


def test_exhaustion_without_a_structured_output_error_is_not_retryable():
    assert _is_transient_failure(
        0, "error_max_turns", "", num_turns=60, max_turns=60,
    ) is False


def test_the_low_turn_fallback_survives_for_envelopes_lacking_the_field():
    """Older/other CLI flavours emit no structuredOutputError at all. A 60-turn
    budget still cannot be exhausted in one turn, so that rule is kept rather
    than replaced."""
    assert _is_transient_failure(
        0, "error_max_turns", "", num_turns=1, max_turns=60,
    ) is True


def test_an_unknown_turn_count_is_not_assumed_retryable():
    """num_turns == -1 means the envelope did not say. Guessing "transient"
    there would re-spawn a genuinely exhausted run."""
    assert _is_transient_failure(
        0, "error_max_turns", "", num_turns=-1, max_turns=60,
        structured_output_error="model did not produce structured output",
    ) is False


def test_a_tiny_max_turns_budget_is_left_alone():
    """The >= 10 guard: with a cap of 2 or 3, "used fewer than the cap" stops
    being evidence of anything."""
    assert _is_transient_failure(
        0, "error_max_turns", "", num_turns=1, max_turns=3,
        structured_output_error="model did not produce structured output",
    ) is False


@pytest.mark.parametrize("exit_code", [2, 3, 5])
def test_deterministic_failures_are_still_never_retried(exit_code):
    """Bad usage / auth / tool failure. Widening the max-turns branch must not
    have widened these — re-spawning an auth failure just fails twice."""
    assert _is_transient_failure(
        exit_code, "error_max_turns", "", num_turns=1, max_turns=60,
        structured_output_error="model did not produce structured output",
    ) is False


# ── The reason string ───────────────────────────────────────────────────────


def test_the_real_run_no_longer_reports_an_exit_code():
    """What the operator actually saw, and what they see now."""
    r = _parse_cli_envelope(REAL_CANCELLED_ENVELOPE, exit_code=0)
    reason = _suspend_reason(0, r, REAL_MAX_TURNS)
    assert "exited with code" not in reason
    assert "structured output" in reason
    assert "8 of 60 turns" in reason


def test_the_mislabelled_cancellation_says_so_when_there_is_no_schema_error():
    r = _parse_cli_envelope(
        '{"text":"","stopReason":"Cancelled","num_turns":8}', exit_code=0)
    reason = _suspend_reason(0, r, 60)
    assert "budget was not reached" in reason


def test_a_genuine_exhaustion_says_exhausted():
    r = _parse_cli_envelope(
        '{"text":"","stopReason":"Cancelled","num_turns":60}', exit_code=0)
    assert "exhausted its turn budget" in _suspend_reason(0, r, 60)


@pytest.mark.parametrize("subtype", ["empty_output", "unparseable_json"])
def test_a_missing_envelope_is_named(subtype):
    r = _parse_cli_envelope("" if subtype == "empty_output" else "not json at all",
                            exit_code=0)
    assert r.subtype == subtype
    assert "no usable result envelope" in _suspend_reason(0, r, 60)


def test_it_falls_back_to_the_exit_code_text_when_there_is_nothing_better():
    """No subtype, no schema error — a plain non-zero exit. The old string is
    still the best available answer and must not be lost."""
    r = _parse_cli_envelope('{"text":"x","stopReason":"EndTurn"}', exit_code=7)
    assert _suspend_reason(7, r, 60) == _reason_for_exit_code(7)


def test_the_reason_survives_an_unknown_turn_count():
    """max_turns=None happens on phases that do not cap turns; the reason must
    still render rather than interpolating None into the message."""
    r = _parse_cli_envelope(REAL_CANCELLED_ENVELOPE, exit_code=0)
    reason = _suspend_reason(0, r, None)
    assert "None" not in reason
    assert "structured output" in reason


def test_run_cli_actually_USES_the_new_reason_builder():
    """The gap the first version of this file left open.

    Every test above calls `_suspend_reason` directly, so reverting the CALL
    SITE back to `_reason_for_exit_code(exit_code)` left all of them green
    while the operator-facing message regressed to "CLI exited with code 0" —
    measured, by doing exactly that revert. `run_cli` cannot be invoked here
    (it spawns a binary), so the wiring is asserted over the AST instead.
    """
    import ast
    import inspect
    import pathlib

    import agents.sdlc_cli_engine as eng

    tree = ast.parse(pathlib.Path(inspect.getsourcefile(eng)).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "run_cli")

    assigns = [n for n in ast.walk(fn)
               if isinstance(n, ast.Assign)
               and any(getattr(t, "id", "") == "reason" for t in n.targets)
               and isinstance(n.value, ast.Call)]
    called = {getattr(a.value.func, "id", getattr(a.value.func, "attr", ""))
              for a in assigns}
    assert "_suspend_reason" in called, (
        "run_cli no longer builds its suspend reason with _suspend_reason — a "
        "suspended phase is back to reporting only its exit code")
