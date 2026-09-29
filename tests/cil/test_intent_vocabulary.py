# SPDX-License-Identifier: MIT
"""Phase 6 §N.1 step 2 — the CIL classifier's routing vocabulary.

``task_complexity`` is the only CIL field that picks a model, and until now
it offered five values: simple | medium | complex | deep | solution. Two of
those were never capability requests. §E deletes both as tiers because
"deep" named a specific GPT SKU and "solution" named Opus — so the classifier
was being asked to pick a vendor, which is the confusion this whole migration
exists to remove.

Three things have to hold together for that shrink to be safe, and they live
in two different files, so they are asserted together here:

  1. the prompt cannot ask for a value the parser will not accept;
  2. the parser cannot emit a value the gateway's routing gate will not
     recognise — a mismatch there silently drops the turn to a flat default;
  3. a model that has not seen the new prompt (a cached response, a pinned
     override) must COLLAPSE to "complex", not fall through to "medium".

(3) is the one worth stating out loud: `_enum`'s default would have silently
DOWNGRADED every "deep" turn, which is the opposite of what both retired
labels meant.
"""

from __future__ import annotations

import pytest

from cil.intent import _RETIRED_COMPLEXITY, _VALID_COMPLEXITY


def test_the_vocabulary_is_exactly_three_values():
    assert _VALID_COMPLEXITY == {"simple", "medium", "complex"}


def test_the_retired_labels_collapse_upward_not_to_the_default():
    """Both meant "harder than medium", so "complex" is the only honest
    destination. Landing on "medium" would be a downgrade dressed up as a
    parse failure."""
    assert _RETIRED_COMPLEXITY == {"deep": "complex", "solution": "complex"}
    assert set(_RETIRED_COMPLEXITY).isdisjoint(_VALID_COMPLEXITY)
    assert set(_RETIRED_COMPLEXITY.values()) <= _VALID_COMPLEXITY


@pytest.mark.parametrize("retired", ["deep", "solution"])
def test_neither_prompt_string_still_offers_a_retired_label(retired):
    """The schema line and the guidance paragraph are separate strings and
    were edited separately; a model shown one of them still emits the label."""
    from cil.intent import _BASE_SYS, _GUIDANCE
    assert retired not in _BASE_SYS
    assert f"'{retired}'" not in _GUIDANCE


def test_the_schema_line_lists_the_vocabulary_the_parser_accepts():
    from cil.intent import _BASE_SYS
    assert '"task_complexity":"simple|medium|complex"' in _BASE_SYS


def test_the_gateways_routing_gate_is_derived_not_duplicated():
    """gateway._PV2_TIER_HINTS is the ONLY consumer of task_complexity for
    routing. A label the classifier can emit but the gate does not contain is
    a turn that silently falls back to the flat "medium" default — invisible
    in the logs, and indistinguishable from the classifier being wrong.

    The gate used to be a hand-written copy of the five-value set, which is
    exactly how the two drift. Asserted against the SOURCE rather than by
    importing gateway: gateway.py writes to /var/lib/ainxt at import time, so
    it is not importable under pytest (the same reason
    tests/agents/test_gateway_passthrough_logic.py reads it as text).
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[2] / "gateway.py").read_text(
        encoding="utf-8", errors="ignore")

    assert "_VALID_COMPLEXITY as _CIL_COMPLEXITY" in src, (
        "gateway no longer derives its routing gate from cil.intent — the two "
        "vocabularies can now drift apart silently")
    assert "_PV2_TIER_HINTS = set(_CIL_COMPLEXITY) & set(_MR_HINT_MAP)" in src
    # The retired labels must be collapsed at the gate too: a conv_state
    # cached before the shrink still carries them.
    assert "_CIL_RETIRED_COMPLEXITY.get(_cil_tier, _cil_tier)" in src
    # No hand-written five-value fallback may survive on either of the two
    # `except`/`if not` branches that used to carry one. Asserted as the exact
    # assignment rather than by searching for "deep": _allowed_hints elsewhere
    # in gateway.py legitimately accepts "deep"/"solution", because those are
    # still valid ROUTER hints an operator may set via ENHANCE_MODEL_HINT.
    # They just stopped being labels a CLASSIFIER may invent.
    assert '_PV2_TIER_HINTS = {"simple", "medium", "complex", "deep", "solution"}' not in src
