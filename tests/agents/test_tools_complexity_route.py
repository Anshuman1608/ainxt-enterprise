# SPDX-License-Identifier: MIT
"""§N.1 step 9 — the classifier verdict → tier mapping, shared by four paths.

``agents/tools.py:539`` was the site §N.1 step 8 widened the CI ratchet
specifically to see (D33): it built ``{"model_hint": _complexity}`` and
splatted it, which the keyword-based scan was blind to. This file covers the
mapping that replaced it, and one trap in particular.

**The trap.** ``_complexity`` becomes the EMPTY STRING on the default install:
``agents/tools.py``'s no-repo-context downgrade assigns
``os.getenv("DOWNGRADE_MODEL", "")``. An empty hint means "classify this
prompt yourself" — ``route()`` gates its hint branch on ``if model_hint:`` —
and there is no tier and no alias that says that. ``_coerce_tier``
RAISES on a ``legacy_hint`` it does not recognise and strips falsy ones, so
coercing the empty case would have raised on the first no-context turn. That
is the same assumption that bit step 6 on ``ENRICH_MODEL``, which is why it
gets a test rather than care.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from core.tiers import Tier
from core.tiers import LEGACY_INBOUND_ALIASES
from models.model_router import chat_complexity_route

ROOT = pathlib.Path(__file__).resolve().parents[2]


# ── the three real verdicts ───────────────────────────────────────────────


@pytest.mark.parametrize("verdict,tier", [
    ("simple",  Tier.SIMPLE),
    ("medium",  Tier.MEDIUM),
    ("complex", Tier.COMPLEX),
])
def test_each_verdict_asks_for_its_tier(verdict, tier):
    assert chat_complexity_route(verdict) == {"tier": tier, "legacy_hint": verdict}


@pytest.mark.parametrize("verdict", ["simple", "medium", "complex"])
def test_the_legacy_hint_is_the_verdicts_own_word(verdict):
    """D15. Not the tier's name — the word the call site passed BEFORE it was
    migrated, so governance-off routing is byte-identical. The two coincide
    for these three, and the test says so explicitly because the next
    vocabulary to be mapped here may not be so lucky (step 8's
    DOC_MODEL_PROVIDER rows are the counter-example)."""
    assert chat_complexity_route(verdict)["legacy_hint"] == verdict


@pytest.mark.parametrize("verdict", ["simple", "medium", "complex"])
def test_every_emitted_hint_is_a_real_hint_map_key(verdict):
    """The assertion that caught step 6's first wrong assumption: a
    legacy_hint that is not a known alias makes _coerce_tier raise, and it
    raises at DISPATCH time, on a live turn, not at import."""
    assert chat_complexity_route(verdict)["legacy_hint"] in LEGACY_INBOUND_ALIASES


def test_the_verdict_is_normalised():
    assert chat_complexity_route("  MEDIUM ") == chat_complexity_route("medium")


# ── the trap ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_empty_verdict_stays_an_auto_hint(empty):
    """Not a tier, and not an error. `{"model_hint": ""}` is how "you decide"
    is spelled, because route() skips its hint branch on a falsy value."""
    out = chat_complexity_route(empty)
    assert out == {"model_hint": ""}
    assert "tier" not in out
    assert "legacy_hint" not in out


def test_the_empty_verdict_would_have_raised_if_coerced():
    """Proves the trap is real rather than theoretical — if _coerce_tier ever
    starts accepting a falsy legacy_hint, this test should be reconsidered
    rather than silently kept."""
    from models.model_router import ModelRouter
    with pytest.raises(ValueError):
        ModelRouter()._coerce_tier(None, Tier.MEDIUM, "not-a-hint-map-key")


def test_the_downgrade_path_is_the_empty_case(monkeypatch):
    """Why it matters here specifically: DOWNGRADE_MODEL is unset by default,
    so the general-chat-without-a-repo branch produces "" every time."""
    src = (ROOT / "agents" / "tools.py").read_text(encoding="utf-8", errors="replace")
    assert '_complexity = os.getenv("DOWNGRADE_MODEL", "")' in src
    import os
    assert not os.getenv("DOWNGRADE_MODEL"), (
        "DOWNGRADE_MODEL is set in this environment, so the default path this "
        "test describes is not the one being exercised"
    )


def test_an_unknown_verdict_passes_through_untouched():
    """A conv_state cached before the vocabulary shrank can still say "deep".
    gateway.py collapses those via _CIL_RETIRED_COMPLEXITY before calling, and
    models/classifier.py's _VALID_TIERS cannot emit them at all — so this is
    defence in depth. Passing the word through preserves exactly today's
    behaviour for a value nobody planned for, which is the safe direction."""
    assert chat_complexity_route("deep") == {"model_hint": "deep"}
    assert chat_complexity_route("gpt-5.4") == {"model_hint": "gpt-5.4"}


# ── the vocabularies it bridges must stay in step ─────────────────────────


def test_the_map_covers_the_classifiers_whole_vocabulary():
    """models/classifier.py::_VALID_TIERS is what agents/tools.py and
    workers/chat_worker.py feed in. A verdict with no entry falls through to
    the hint passthrough, which is safe but ungoverned — so a widened
    classifier must widen this map too, and this is where that is noticed."""
    from models.classifier import _VALID_TIERS
    from models.model_router import _CHAT_COMPLEXITY_TIERS
    assert _VALID_TIERS == set(_CHAT_COMPLEXITY_TIERS)


def test_the_map_covers_the_cils_whole_vocabulary():
    """cil/intent.py::_VALID_COMPLEXITY drives gateway.py's Auto path. §F said
    step 9 would have to shrink this vocabulary; Phase 6 already did, which is
    correction 3 in the step 9 plan."""
    from cil.intent import _VALID_COMPLEXITY
    from models.model_router import _CHAT_COMPLEXITY_TIERS
    assert _VALID_COMPLEXITY == set(_CHAT_COMPLEXITY_TIERS)


def test_the_retired_verdicts_collapse_before_they_arrive():
    from cil.intent import _RETIRED_COMPLEXITY
    from models.model_router import _CHAT_COMPLEXITY_TIERS
    for old, new in _RETIRED_COMPLEXITY.items():
        assert old not in _CHAT_COMPLEXITY_TIERS, (
            f"{old!r} is retired; it must be collapsed by the caller, not "
            f"given a tier here"
        )
        assert new in _CHAT_COMPLEXITY_TIERS


# ── the call site ─────────────────────────────────────────────────────────


def test_tools_py_uses_the_shared_mapping():
    """Not a fourth copy of the table. The pre-migration code had the same
    three words mapped in gateway.py, kb_ask_router.py, chat_worker.py and
    here, which is how the platform ended up with two tier systems."""
    tree = ast.parse((ROOT / "agents" / "tools.py").read_text(
        encoding="utf-8", errors="replace"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "_chat_complexity_route"]
    assert calls, "agents/tools.py no longer calls the shared mapping"


def test_the_three_source_precedence_is_unchanged():
    """model_hint (the user) beats tier (the caller) beats _complexity (the
    guess). Step 9 changes only the third; the order is §G's requirement."""
    src = (ROOT / "agents" / "tools.py").read_text(encoding="utf-8", errors="replace")
    i_hint = src.index('if getattr(state, "model_hint", None):')
    i_tier = src.index('elif getattr(state, "tier", None) is not None:')
    i_cls = src.index("_route_kwargs = _chat_complexity_route(_complexity)")
    assert i_hint < i_tier < i_cls
