# SPDX-License-Identifier: MIT
# ============================================================
# Phase 3 → Phase 9 — router tier wiring, against the real source
# ============================================================
#
# WHAT THIS FILE USED TO BE, AND WHY IT WAS WRONG
# -----------------------------------------------
# It carried a hand-written *mirror* of gateway.py's `_fp_hint` computation —
# a second copy of the logic, maintained by hand, tested in place of the real
# thing. Phase 5 changed the original and the copy was not updated, so by the
# time Phase 9 measured it the file asserted the OPPOSITE of production and
# was green about it:
#
#     its   _TIER_HINTS = {"simple","medium","complex","deep","solution"}   (5)
#     live  _PV2_TIER_HINTS = {"simple","medium","complex"}                  (3)
#     live  _CIL_RETIRED_COMPLEXITY = {"deep":"complex","solution":"complex"}
#           — the mirror had no equivalent at all
#
# So `test_on_auto_follows_cil_complexity` asserted that a cached CIL verdict
# of "deep" routes to "deep". The shipped gateway COLLAPSES deep → complex
# before the membership test and routes to "complex". Two assertions in this
# file therefore flip in Phase 9. That is not weakened coverage — it is the
# coverage starting to describe the platform.
#
# Every test name is kept (Phase 9's exit criterion is "no test deleted");
# one case is added for the collapse itself, which is the behaviour the old
# file specifically denied.
#
# WHY WE exec() THE SOURCE INSTEAD OF `import gateway`
# ----------------------------------------------------
# Same reason as tests/test_browser_agent_prompt.py:22 — gateway.py:31 calls
# core.ckms.load_at_boot() at import time, and importing the module also
# mounts every router and opens its connections. CI Tier 2 runs pytest on a
# bare runner (.github/workflows/ci.yml, `pip install -r requirements.txt`),
# not in the application image.
#
# The difference from a mirror is the one that matters: this loads the SHIPPED
# statements. It cannot drift. If gateway.py is refactored, the extraction
# asserts its own preconditions and fails with a message naming what moved,
# rather than silently continuing to test a copy.
#
# tests/routers/test_chat_auto_tier_mapping.py:95,355-362 asserts the same
# block TEXTUALLY (that the source contains the collapse and the membership
# test). This file is the behavioural half of that guarantee.
# ============================================================

from __future__ import annotations

import ast
import pathlib

import pytest

from cil.state import ConversationState

_GATEWAY = pathlib.Path(__file__).resolve().parents[2] / "gateway.py"

# The four statements the gateway runs, in order, at gateway.py:7599-7636.
# Named here so a failure says which one went missing.
_WANTED = (
    "_fp_hint       — the flat default: voice → complex, else the pick or medium",
    "_fp_tier       — the tier travelling beside the hint (§N.1 step 9)",
    "if _is_voice…  — the voice/no-pick tier assignment",
    "if _PIPELINE_V2 … — the CIL-driven override, incl. the retirement collapse",
)


def _load_fp_hint_from_gateway_source():
    """Build a callable from gateway.py's real `_fp_hint` statements.

    Returns ``fp(is_voice, model_hint, flag_on, conv_state) -> (hint, tier)``.

    Two extractions, because the logic spans two scopes:

      1. the module-scope vocabulary (`_CIL_COMPLEXITY`,
         `_CIL_RETIRED_COMPLEXITY`, `_PV2_TIER_HINTS`) — taken as the real
         try/except blocks so their import-failure fallbacks are exercised
         exactly as they are in production;
      2. the four statements inside `ask_ai` that compute the hint and tier,
         re-hosted in a synthesized function whose parameters are the inputs
         they read as locals.

    Everything else the block touches is the genuine article: `_Tier` is
    core.tiers.Tier and `_chat_complexity_route` is
    models.model_router.chat_complexity_route. Only `_otel` is a stub, because
    recording a span is not what is under test.
    """
    tree = ast.parse(_GATEWAY.read_text(encoding="utf-8", errors="replace"))

    # ── 1. module-scope vocabulary ───────────────────────────────────────
    vocab: list[ast.stmt] = []
    for st in tree.body:
        if not isinstance(st, ast.Try):
            continue
        assigned = {
            t.id
            for n in ast.walk(st)
            for t in getattr(n, "targets", [])
            if isinstance(t, ast.Name)
        }
        if assigned & {"_CIL_COMPLEXITY", "_PV2_TIER_HINTS"}:
            vocab.append(st)
    assert len(vocab) == 2, (
        f"expected the two try/except blocks that define _CIL_COMPLEXITY / "
        f"_CIL_RETIRED_COMPLEXITY / _PV2_TIER_HINTS at gateway.py module scope, "
        f"found {len(vocab)} — the CIL routing vocabulary has moved"
    )

    # ── 2. the four statements inside ask_ai ─────────────────────────────
    block: list[ast.stmt] | None = None
    for node in ast.walk(tree):
        for _f, val in ast.iter_fields(node):
            if not (isinstance(val, list) and val and isinstance(val[0], ast.stmt)):
                continue
            for i, st in enumerate(val):
                if (isinstance(st, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "_fp_hint"
                                for t in st.targets)
                        and isinstance(st.value, ast.IfExp)):
                    block = val[i:i + 4]
    assert block is not None and len(block) == 4, (
        "could not locate gateway.py's `_fp_hint = 'complex' if ... else ...` "
        "statement and the three that follow it"
    )
    assert any(isinstance(n, ast.Name) and n.id == "_CIL_RETIRED_COMPLEXITY"
               for n in ast.walk(block[3])), (
        "the extracted block no longer applies _CIL_RETIRED_COMPLEXITY — a "
        "cached 'deep'/'solution' verdict would fall through to the flat "
        f"medium default. Expected statements: {_WANTED}"
    )

    fn = ast.FunctionDef(
        name="_fp",
        args=ast.arguments(
            posonlyargs=[], args=[
                ast.arg(arg=a) for a in (
                    "_is_voice_platform", "_model_hint", "_rc",
                    "_PIPELINE_V2", "_PIPELINE_V2_ROUTING",
                )
            ],
            kwonlyargs=[], kw_defaults=[], defaults=[],
        ),
        body=[*block, ast.Return(value=ast.Tuple(
            elts=[ast.Name(id="_fp_hint", ctx=ast.Load()),
                  ast.Name(id="_fp_tier", ctx=ast.Load())],
            ctx=ast.Load()))],
        decorator_list=[],
    )
    mod = ast.fix_missing_locations(ast.Module(body=[*vocab, fn], type_ignores=[]))

    from core.tiers import Tier as _Tier
    from models.model_router import chat_complexity_route as _ccr

    class _Otel:
        def record_event(self, *a, **k):  # noqa: D102 — a span is not under test
            pass

    ns: dict = {"_Tier": _Tier, "_chat_complexity_route": _ccr, "_otel": _Otel()}
    exec(compile(mod, str(_GATEWAY), "exec"), ns)   # noqa: S102 — our own source

    def fp(*, is_voice, model_hint, flag_on, conv_state):
        class _RC:
            pass
        rc = None
        if conv_state is not None:
            rc = _RC()
            rc.conv_state = conv_state
        return ns["_fp"](is_voice, model_hint, rc, flag_on, flag_on)

    fp.vocabulary = ns["_PV2_TIER_HINTS"]           # type: ignore[attr-defined]
    fp.retired = ns["_CIL_RETIRED_COMPLEXITY"]      # type: ignore[attr-defined]
    return fp


_FP = _load_fp_hint_from_gateway_source()


def _fp_hint(*, is_voice, model_hint, flag_on, conv_state):
    """The hint only — what every assertion below was already written against."""
    return _FP(is_voice=is_voice, model_hint=model_hint,
               flag_on=flag_on, conv_state=conv_state)[0]


def _cs(complexity):
    st = ConversationState()
    st.task_complexity = complexity
    return st


# ── flag OFF → today's behavior ─────────────────────────────────────────────

def test_off_auto_is_medium():
    assert _fp_hint(is_voice=False, model_hint=None, flag_on=False, conv_state=_cs("complex")) == "medium"


def test_off_forced_model_kept():
    assert _fp_hint(is_voice=False, model_hint="haiku", flag_on=False, conv_state=None) == "haiku"


def test_off_voice_is_complex():
    assert _fp_hint(is_voice=True, model_hint=None, flag_on=False, conv_state=None) == "complex"


# ── flag ON → real change, but only for auto turns ──────────────────────────

def test_on_auto_follows_cil_complexity():
    """`deep` CHANGED in Phase 9, and the change is the point of this file.

    The old mirror asserted `_cs("deep") -> "deep"`. Production has collapsed
    deep → complex since Phase 5 (gateway.py's _CIL_RETIRED_COMPLEXITY): the
    CIL vocabulary shrank to three labels, and a conv_state cached before the
    shrink can still carry the old one. Collapsing keeps those turns at the
    capability they asked for; dropping them would downgrade to medium.
    """
    assert _fp_hint(is_voice=False, model_hint=None, flag_on=True, conv_state=_cs("simple")) == "simple"
    assert _fp_hint(is_voice=False, model_hint=None, flag_on=True, conv_state=_cs("complex")) == "complex"
    assert _fp_hint(is_voice=False, model_hint=None, flag_on=True, conv_state=_cs("deep")) == "complex"


def test_on_forced_model_never_overridden():
    # user explicitly picked a model → CIL never overrides it
    assert _fp_hint(is_voice=False, model_hint="opus-4-6", flag_on=True, conv_state=_cs("simple")) == "opus-4-6"


def test_on_voice_unchanged():
    assert _fp_hint(is_voice=True, model_hint=None, flag_on=True, conv_state=_cs("simple")) == "complex"


def test_on_no_conv_state_is_medium():
    assert _fp_hint(is_voice=False, model_hint=None, flag_on=True, conv_state=None) == "medium"


def test_on_invalid_tier_falls_back_to_medium():
    assert _fp_hint(is_voice=False, model_hint=None, flag_on=True, conv_state=_cs("bogus")) == "medium"


# ── added in Phase 9 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("retired", ["deep", "solution"])
def test_retired_complexity_collapses_onto_complex(retired):
    """The behaviour the old mirror denied, asserted directly.

    Both retired labels meant "harder than medium". Falling through to the
    flat default would silently downgrade every turn whose cached verdict
    predates the vocabulary change — invisible, because the answer still
    arrives.
    """
    assert _FP.retired[retired] == "complex"
    assert _fp_hint(is_voice=False, model_hint=None,
                    flag_on=True, conv_state=_cs(retired)) == "complex"


def test_the_gate_vocabulary_is_the_three_live_labels():
    """The mirror's five-label set is what let `deep` pass through untouched.

    Guarded here as a value rather than a comment: if the CIL gains a label
    and _PV2_TIER_HINTS does not, those turns fall back to medium with nothing
    reporting it.
    """
    assert _FP.vocabulary == {"simple", "medium", "complex"}


def test_a_tier_travels_beside_the_hint():
    """§N.1 step 9: _fp_hint stays a str for its five non-dispatch readers,
    and the Tier rides alongside. A change that drops the tier would leave
    dispatch on the legacy hint with every assertion above still passing."""
    from core.tiers import Tier

    _, tier = _FP(is_voice=True, model_hint=None, flag_on=False, conv_state=None)
    assert tier is Tier.COMPLEX

    _, tier = _FP(is_voice=False, model_hint=None, flag_on=False, conv_state=None)
    assert tier is Tier.MEDIUM

    # An explicit pick is never governed into a tier (§G).
    _, tier = _FP(is_voice=False, model_hint="opus-4-6", flag_on=False, conv_state=None)
    assert tier is None
