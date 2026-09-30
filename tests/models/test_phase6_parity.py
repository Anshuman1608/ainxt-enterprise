# SPDX-License-Identifier: MIT
"""Phase 6 — every migrated call site is a no-op with governance off.

This is the load-bearing test for decision D15. Phase 6 rewrites ~90 call
sites from ``model_hint="x"`` to ``tier=Tier.Y, legacy_hint="x"``. The claim
that makes that safe to ship is:

    On a deployment that has not set TIER_GOVERNANCE_ENABLED, a migrated call
    site routes to EXACTLY the model it routed to before.

``tests/models/test_legacy_hint_shim.py`` tests the mechanism. This file tests
the claim, pair by pair, against the actual (tier, legacy_hint) combinations
the migration introduces — so adding a call site with a new combination means
adding a row here, and a row that does not hold fails the build.

Deleted in Phase 10 along with the shim itself.
"""

from __future__ import annotations

import dataclasses

import pytest

from core.tiers import Tier
from models.model_router import ModelRouter


@pytest.fixture(autouse=True)
def _governance_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The entire file describes the flag-OFF path. Pin it: a developer whose
    .env turns governance on would otherwise see these pass for the wrong
    reason (both sides resolving through the same assignment)."""
    monkeypatch.delenv("TIER_GOVERNANCE_ENABLED", raising=False)


# (tier now requested, hint previously passed, where)
#
# Every row is a call site group from plan.html §N.1 steps 1-5. The third
# element is documentation, not assertion input — it is what tells the next
# person which module to look at when a row starts failing.
MIGRATED: list[tuple[Tier, str, str]] = [
    # ── step 1: memory & summarisation (§D.2 "rolling summary", "memory merge")
    (Tier.SIMPLE, "simple",
     "memory/chat_summarizer.py, memory/postgres_memory.py, core/context_manager.py"),

    # ── step 2: classification paths (§D.2 — all four become one admin setting)
    (Tier.INTENT_CLASSIFICATION, "local_mini", "cil/intent.py"),
    (Tier.INTENT_CLASSIFICATION, "haiku",      "models/classifier.py, models/doc_intent.py"),
    (Tier.INTENT_CLASSIFICATION, "simple",
     "models/query_rewriter.py, models/router.py, agents/router_agent.py"),

    # ── step 3: structured-output agents (generation and verification split)
    (Tier.COMPLEX, "complex",
     "agents/orchestrator.py:289, agents/advanced_reasoning.py generation"),

    # ── step 4: leaf feature modules
    (Tier.COMPLEX, "claude",  "routers/broadcast_router.py:437 — vendor alias removed"),
    (Tier.MEDIUM,  "medium",  "routers/projects_router.py:356"),
    # R4 reclassification, not a rename: threads_router asks for a SMALLER
    # tier than it used to. The flag-off path must still produce the old
    # medium routing, which is exactly what makes the before/after comparison
    # in that commit meaningful rather than circular.
    (Tier.SIMPLE,  "medium",  "routers/threads_router.py:225 — medium -> simple"),

    # ── Phase 6.5 item 1: the one call site step 4 left behind.
    # The pair is the same as the orchestrator row above, so this asserts
    # nothing new about routing. It is here because the file's invariant is
    # "every (tier, legacy_hint) pair the migration introduces has a row" —
    # a pair with no row is a pair nobody proved is a no-op, and the next
    # person reading this list would not know the Cowork path existed.
    (Tier.COMPLEX, "complex",
     "workers/cowork_task_worker.py:165 — via agents/orchestrator.py::run()"),

    # ── step 6: CodeWiki, index enrichment, retrieval
    # Chunk enrichment is the ONE row in this file whose legacy_hint is not a
    # faithful reproduction, and the parity assertion below is therefore
    # weaker for it than for every other row. It used to pass
    # model_hint=ENRICH_MODEL, empty on a default install, which route()
    # treats as "no hint" and complexity-classifies per chunk. No _HINT_MAP
    # key means "auto", so there is nothing to put here that reproduces it —
    # "haiku" is what the tier was chosen to mean, not what the call site did
    # before. What this row still proves is the narrower claim that matters
    # at runtime: tier=SIMPLE + legacy_hint="haiku" routes exactly where a
    # bare "haiku" did, so the rollback path is not itself a new behaviour.
    (Tier.SIMPLE,  "haiku",    "workers/index_worker.py:752 — chunk enrichment"),
    (Tier.SIMPLE,  "haiku",
     "models/hybrid_retriever.py — query expansion + multi-query decomposition"),
    (Tier.COMPLEX, "solution", "sandbox/self_healing_engine.py:339"),
    (Tier.COMPLEX, "complex",  "workers/secure_code_gate_worker.py:126"),

    # ── step 8: the document pipeline
    # The body pairs are unremarkable — "complex" meant the same destination
    # before and after, which is the point.
    (Tier.COMPLEX, "complex",
     "workers/doc_worker.py:843,1750,1871 + _resolve_doc_route default; "
     "services/doc_reviser.py; agents/doc_generator_agent.py::_llm_call"),
    # DOC_MODEL_PROVIDER naming a governed tier (D28). Each of the four text
    # tiers carries ITS OWN word as the legacy hint rather than the module
    # default, because with governance off DOC_MODEL_PROVIDER=medium has
    # always meant model_hint="medium" — carrying "complex" here would
    # silently upgrade every flag-off deployment that set it.
    (Tier.MINI,    "mini",    "workers/doc_worker.py DOC_MODEL_PROVIDER=mini"),
    (Tier.SIMPLE,  "simple",  "workers/doc_worker.py DOC_MODEL_PROVIDER=simple"),
    (Tier.MEDIUM,  "medium",  "workers/doc_worker.py DOC_MODEL_PROVIDER=medium"),
    # Titling and the ≤5-bullet cosmetic summary: both were vendor SKU names
    # ("local" and "haiku") standing in for "short output, still has to follow
    # instructions".
    (Tier.SIMPLE,  "haiku",
     "workers/doc_worker.py::_title_route; agents/doc_generator_agent.py:778"),
    # step 8f — the two homeless sites. coach_router's hint is "mini" and not
    # "haiku" because its pre-migration destination was OPENAI_SIMPLE_MODEL
    # (gpt-5-mini) via a direct OpenAIGateway, and D15 is about reproducing
    # where a call site WENT, not about matching the tier's name.
    (Tier.SIMPLE,  "haiku",   "services/feedback_processor.py:337"),
    (Tier.SIMPLE,  "mini",    "routers/coach_router.py:972 — was openai_model_for_tier"),

    # ── step 9: the Chat Auto path
    # Step 9 introduces no pair this file has not already proved — the
    # classifier's whole vocabulary is {simple, medium, complex} and each word
    # maps to the tier of the same name. The rows are here anyway, because the
    # file's invariant is "every migrated call site's pair has a row that
    # NAMES it": a reader whose Auto turns started going somewhere new needs
    # to find the Auto path in this list, and a pair that is only implied by
    # step 1's summariser row is a pair nobody checked for step 9.
    #
    # The `simple` row is the one that matters. Pre-migration,
    # model_hint="simple" dispatched the in-house model via gateway_local_llm;
    # Tier.SIMPLE resolves to whatever the administrator assigned. So the
    # parity assertion below proves the ROLLBACK is exact — governance off and
    # this row is byte-identical — while saying nothing about flag-on, where
    # the change is the intended one (D35, §D.2). That limit is worth stating:
    # it is the same shape as the index_worker row above, and it is why step 9
    # needs field checks rather than only this file.
    (Tier.SIMPLE,  "simple",
     "gateway.py CIL task_complexity='simple'; workers/chat_worker.py:1556 "
     "cached summary — was the local model by name"),
    (Tier.MEDIUM,  "medium",
     "gateway.py flat Auto default + CIL 'medium'; routers/kb_ask_router.py; "
     "workers/chat_worker.py KB answer; gateway.py continue-truncated-answer"),
    (Tier.COMPLEX, "complex",
     "gateway.py voice_platform + CIL 'complex'; routers/kb_ask_router.py "
     "voice; workers/chat_worker.py:790,1148 docx/pptx structuring"),
    (Tier.SIMPLE,  "haiku",
     "gateway.py:3672 follow-up suggestions; routers/chat_router.py:2347 "
     "chat title"),
    (Tier.MINI,    "mini",    "gateway.py:3346 prompt enhancement (ENHANCE_MODEL_HINT)"),

    # ── step 10: the SDLC pipeline
    # Two pairs are new here, and both are ones no earlier step could have
    # produced: "solution" and "deep" are the last two legacy aliases that
    # mean COMPLEX, and SDLC was their only remaining caller.
    #
    # (COMPLEX, "solution") is the row to read carefully. Flag-OFF it is
    # byte-identical, which is what this file asserts. Flag-ON it is NOT the
    # same as before: _LEGACY_TO_GOVERNED maps the "solution" STRING to
    # (COMPLEX, require_role="review"), and asking for the tier directly does
    # not carry the role (D43). That is the intended narrowing — the reviewer
    # belongs to the two review gates, not to every hintless SDLC call — and
    # the limit of what a parity row can tell you about it is exactly the
    # limit step 9's `simple` row had.
    (Tier.COMPLEX, "solution",
     "agents/sdlc_pipeline/_core.py + agents/sdlc_state_machine.py _llm() "
     "default; agents/sdlc_context.py:363 exploration synthesis; "
     "agents/react_engine.py synthesis_route; the code_review gate"),
    # "deep" existed only to be the context-promotion target (§M.2); the
    # window is a constraint, so the tier is COMPLEX.
    (Tier.COMPLEX, "deep",
     "agents/sdlc_pipeline/_phases.py:353 + _core.py:2008 manifest judge"),
    # Already proved above; the rows name the SDLC sites because this file's
    # invariant is "every migrated call site's pair has a row that NAMES it".
    (Tier.SIMPLE,  "haiku",
     "SDLC locate (sdlc_patch_engine.py:641), normalize "
     "(sdlc_normalizer.py:118), classify (cli_classify_model)"),
    (Tier.COMPLEX, "complex",
     "SDLC coder/plan/implement (the CLI spawns); sdlc_patch_engine.py:362; "
     "_self_review; brd_fsd_pipeline.py:146; the governance scan + fixer"),
    (Tier.MEDIUM,  "medium",
     "both _llm() cross-provider second attempts (_core.py:400, "
     "sdlc_state_machine.py:100) and _generate_conflict_resolution"),
]

_IDS = [f"{t.value}<-{h}" for t, h, _ in MIGRATED]


@pytest.mark.parametrize("tier,legacy_hint,where", MIGRATED, ids=_IDS)
def test_migrated_call_site_routes_exactly_as_before(tier, legacy_hint, where):
    """route(tier=T, legacy_hint=H) == route(model_hint=H), field for field."""
    router = ModelRouter()
    prompt = "summarise the incident report attached to ticket AX-1299"

    before = router.route(prompt, model_hint=legacy_hint)
    after = router.route(prompt, tier=tier, legacy_hint=legacy_hint)

    assert after == before, (
        f"{where}: migrating to tier={tier.value} changed flag-off routing.\n"
        f"  was: {before}\n  now: {after}"
    )


@pytest.mark.parametrize("tier,legacy_hint,where", MIGRATED, ids=_IDS)
def test_the_legacy_hint_is_not_merely_the_default_coercion(tier, legacy_hint, where):
    """Guard against the shim being decorative.

    If `legacy_hint` happened to equal `_TIER_TO_LEGACY_HINT[tier]` for every
    row, the test above would pass whether or not the shim worked. At least
    one row must genuinely differ, and the rows that do are the ~20 call sites
    D15 exists for. This asserts the file as a whole still has teeth.
    """
    from models.model_router import _TIER_TO_LEGACY_HINT
    differing = [
        (t.value, h) for t, h, _ in MIGRATED if h != _TIER_TO_LEGACY_HINT[t]
    ]
    assert differing, (
        "No migrated pair differs from the default coercion any more — either "
        "_TIER_TO_LEGACY_HINT changed or the rows above were trimmed. Either "
        "way the parity assertions above have stopped proving anything."
    )


def test_streaming_entry_points_carry_the_hint_too():
    """stream() and generate() share route(), but they pass it differently —
    a shim wired into one and not the other would be invisible until a
    streaming call site was migrated."""
    router = ModelRouter()
    import inspect
    for name in ("route", "generate", "generate_structured",
                 "stream", "async_generate", "async_stream"):
        sig = inspect.signature(getattr(router, name))
        assert "legacy_hint" in sig.parameters, f"{name}() cannot be migrated"
        assert sig.parameters["legacy_hint"].kind is inspect.Parameter.KEYWORD_ONLY


def test_decision_is_a_dataclass_so_equality_is_structural():
    """The parity assertions above compare RoutingDecision with ==. That is
    only meaningful while it stays a dataclass; if it ever grows a custom
    __eq__ or becomes a plain class, those assertions silently degrade to
    identity comparison and pass for free."""
    from models.model_router import RoutingDecision
    assert dataclasses.is_dataclass(RoutingDecision)
    assert RoutingDecision.__eq__ is not object.__eq__
