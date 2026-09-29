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
