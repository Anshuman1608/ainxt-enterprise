# SPDX-License-Identifier: MIT
"""Phase 3 — the tier resolver's selection and constraint semantics.

Four properties carry real consequences and are each tested directly:

1. SELECTION IS THE ADMIN'S PRIORITY ORDER, not a score. The Tiers screen
   promises "1, 2, 3" and means it. `router/policy.py` implements a weighted
   scorer that would quietly override that ordering; it stays unwired, and
   these tests are what would catch someone wiring it in.

2. MISSING METADATA IS TREATED ASYMMETRICALLY. Absent `privacy_class` or
   `context_window` EXCLUDES a model; absent `supports_tools` ALLOWS it.
   Under-permitting fails a request visibly and someone fixes the metadata.
   Over-permitting either egresses confidential data or overflows a context
   window. The two are not equally bad.

3. THE LADDER IS WALKED AT MOST ONCE, AND NEVER UNDER `no_cloud_egress`.
   Degrading a confidential request onto a weaker tier could land it on an
   external model — the exact outcome the constraint exists to prevent.

4. NOTHING IS EVER SUBSTITUTED SILENTLY. When no candidate survives,
   `NoEligibleModel` is raised. A caller that asked for video generation and
   received a text model produces a confusing wrong answer; an error produces
   a fixable one.

No DB and no Redis: both the registry and the assignment table are served from
in-memory lists, the same approach as tests/core/test_provider_base_url.py.
"""

from __future__ import annotations

import pytest

import core.llm_provider_registry as reg
import core.tier_resolver as tr
from core.tier_resolver import Constraints, NoEligibleModel, resolve_tier
from core.tiers import Tier

_LOCAL = "deployment_local"
_EXTERNAL = "external"


def _model(row_id: str, *, family: str = "anthropic", model_id: str | None = None,
           **caps) -> dict:
    return {
        "id": row_id,
        "model_id": model_id or f"model-{row_id}",
        "display_name": f"Model {row_id}",
        "capabilities": caps,
        "is_default": False,
        "sort_order": 0,
        "provider_id": f"p-{family}",
        "provider_slug": family,
        "provider_name": family,
        "family": family,
        "base_url": None,
    }


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    """An in-memory registry + tier table. Returns (models, assignments)."""
    models: list[dict] = []
    assignments: list[dict] = []

    monkeypatch.setattr(reg, "get_enabled_models", lambda channel=None: list(models))
    monkeypatch.setattr(tr, "get_tier_assignments", lambda: list(assignments))
    # No breaker state unless a test installs some.
    monkeypatch.setattr(tr, "_breaker_is_open", lambda slug, mid: False)

    def assign(tier: Tier, row_id: str, priority: int = 100, role=None):
        assignments.append({
            "tier": tier.value, "model_row_id": row_id,
            "priority": priority, "role": role, "org_id": "default",
        })

    return models, assign


# ── 1. Priority order is the decision ───────────────────────────────────────


def test_lowest_priority_number_wins(world):
    models, assign = world
    models += [_model("a"), _model("b")]
    assign(Tier.COMPLEX, "b", priority=1)
    assign(Tier.COMPLEX, "a", priority=2)
    assert resolve_tier(Tier.COMPLEX).row_id == "b"


def test_priority_beats_every_other_signal(world):
    """The cheap, huge-context, local model still loses to priority 1.

    This is the test that fails if someone wires the weighted scorer in: on
    quality/cost/latency grounds `cheap` wins on every axis, and it must
    still lose, because the administrator said so.
    """
    models, assign = world
    models += [
        _model("expensive", context_window=1000, cost_per_1m_output=75.0,
               privacy_class=_EXTERNAL),
        _model("cheap", family="ollama", context_window=1_000_000,
               cost_per_1m_output=0.0, privacy_class=_LOCAL),
    ]
    assign(Tier.COMPLEX, "expensive", priority=1)
    assign(Tier.COMPLEX, "cheap", priority=2)
    assert resolve_tier(Tier.COMPLEX).row_id == "expensive"


def test_second_candidate_used_when_first_filtered_out(world):
    models, assign = world
    models += [_model("ext", privacy_class=_EXTERNAL),
               _model("loc", family="ollama", privacy_class=_LOCAL)]
    assign(Tier.COMPLEX, "ext", priority=1)
    assign(Tier.COMPLEX, "loc", priority=2)
    r = resolve_tier(Tier.COMPLEX, Constraints(no_cloud_egress=True))
    assert r.row_id == "loc"
    assert not r.via_fallback


def test_unassigned_tier_raises(world):
    world[0].append(_model("a"))
    with pytest.raises(NoEligibleModel):
        resolve_tier(Tier.COMPLEX)


def test_assignment_to_a_disabled_model_yields_no_candidate(world):
    """The tier row survives; the model is simply not in the enabled set."""
    _, assign = world
    assign(Tier.COMPLEX, "gone", priority=1)
    with pytest.raises(NoEligibleModel):
        resolve_tier(Tier.COMPLEX)


# ── 2. Fail-safe vs permissive, per constraint ──────────────────────────────


def test_missing_privacy_class_is_not_local(world):
    """The single most consequential default in this module."""
    models, assign = world
    models.append(_model("unknown"))          # no privacy_class at all
    assign(Tier.COMPLEX, "unknown", priority=1)
    with pytest.raises(NoEligibleModel) as e:
        resolve_tier(Tier.COMPLEX, Constraints(no_cloud_egress=True))
    assert "unset" in str(e.value)


def test_external_privacy_class_is_excluded(world):
    models, assign = world
    models.append(_model("ext", privacy_class=_EXTERNAL))
    assign(Tier.COMPLEX, "ext", priority=1)
    with pytest.raises(NoEligibleModel):
        resolve_tier(Tier.COMPLEX, Constraints(no_cloud_egress=True))


def test_missing_context_window_cannot_satisfy_a_minimum(world):
    """An unknown window is not an infinite one."""
    models, assign = world
    models.append(_model("nowindow"))
    assign(Tier.COMPLEX, "nowindow", priority=1)
    with pytest.raises(NoEligibleModel) as e:
        resolve_tier(Tier.COMPLEX, Constraints(min_context_window=200_000))
    assert "unknown" in str(e.value)


@pytest.mark.parametrize("window, need, ok", [
    (200_000, 200_000, True),
    (200_001, 200_000, True),
    (199_999, 200_000, False),
])
def test_context_window_boundary(world, window, need, ok):
    models, assign = world
    models.append(_model("m", context_window=window))
    assign(Tier.COMPLEX, "m", priority=1)
    c = Constraints(min_context_window=need)
    if ok:
        assert resolve_tier(Tier.COMPLEX, c).row_id == "m"
    else:
        with pytest.raises(NoEligibleModel):
            resolve_tier(Tier.COMPLEX, c)


def test_missing_capability_flags_are_permissive(world):
    """Absent supports_tools means "unknown", and unknown must not block.

    The opposite stance to privacy: the cost of being wrong is a provider
    error on one call, not a data leak.
    """
    models, assign = world
    models.append(_model("m"))
    assign(Tier.COMPLEX, "m", priority=1)
    r = resolve_tier(Tier.COMPLEX, Constraints(needs_tools=True, needs_streaming=True))
    assert r.row_id == "m"


def test_explicit_false_capability_flag_excludes(world):
    models, assign = world
    models.append(_model("m", supports_tools=False))
    assign(Tier.COMPLEX, "m", priority=1)
    with pytest.raises(NoEligibleModel):
        resolve_tier(Tier.COMPLEX, Constraints(needs_tools=True))


# ── Modality ────────────────────────────────────────────────────────────────


def test_tier_modality_requirement_is_always_enforced(world):
    """No constraint needed — the tier itself demands it."""
    models, assign = world
    models.append(_model("textonly", modality=["text"]))
    assign(Tier.VIDEO_GENERATION, "textonly", priority=1)
    with pytest.raises(NoEligibleModel) as e:
        resolve_tier(Tier.VIDEO_GENERATION)
    assert "video-out" in str(e.value)


def test_bare_video_string_still_understood(world):
    """Pre-Phase-2 rows stored a scalar "video"; ai-ui still reads that form."""
    models, assign = world
    models.append(_model("veo", family="gemini", modality="video"))
    assign(Tier.VIDEO_GENERATION, "veo", priority=1)
    assert resolve_tier(Tier.VIDEO_GENERATION).row_id == "veo"


def test_absent_modality_means_text(world):
    models, assign = world
    models.append(_model("m"))
    assign(Tier.MEDIUM, "m", priority=1)
    assert resolve_tier(Tier.MEDIUM).row_id == "m"


# ── 3. The fallback ladder ──────────────────────────────────────────────────


def test_ladder_walked_when_tier_is_empty(world):
    models, assign = world
    models.append(_model("m"))
    assign(Tier.MEDIUM, "m", priority=1)        # complex → medium
    r = resolve_tier(Tier.COMPLEX)
    assert r.row_id == "m"
    assert r.tier is Tier.MEDIUM and r.requested_tier is Tier.COMPLEX
    assert r.via_fallback and r.selection_mode == "fallback"


def test_ladder_walked_at_most_once(world):
    """complex → medium is allowed; complex → medium → simple is not.

    An unbounded walk would let a `complex` request quietly land on `mini`,
    which is a capability collapse dressed up as resilience.
    """
    models, assign = world
    models.append(_model("m"))
    assign(Tier.SIMPLE, "m", priority=1)        # two rungs below complex
    with pytest.raises(NoEligibleModel):
        resolve_tier(Tier.COMPLEX)


def test_ladder_never_walked_under_no_cloud_egress(world):
    """The safety property. A local model exists one rung down and must NOT
    be reached, because reaching it would mean the walk is permitted — and
    the next deployment's rung down may be a cloud model."""
    models, assign = world
    models += [_model("ext", privacy_class=_EXTERNAL),
               _model("loc", family="ollama", privacy_class=_LOCAL)]
    assign(Tier.COMPLEX, "ext", priority=1)
    assign(Tier.MEDIUM, "loc", priority=1)
    with pytest.raises(NoEligibleModel):
        resolve_tier(Tier.COMPLEX, Constraints(no_cloud_egress=True))


@pytest.mark.parametrize("tier", [Tier.IMAGE_INPUT, Tier.IMAGE_OUTPUT,
                                  Tier.VIDEO_GENERATION])
def test_modality_tiers_never_fall_back(world, tier):
    """Nothing substitutes for image or video. A text model cannot produce a
    video, so degrading turns "unavailable" into a wrong answer."""
    models, assign = world
    models.append(_model("text"))
    for t in (Tier.COMPLEX, Tier.MEDIUM, Tier.SIMPLE, Tier.MINI):
        assign(t, "text", priority=1)
    with pytest.raises(NoEligibleModel):
        resolve_tier(tier)


def test_mini_has_no_rung_below_it(world):
    world[0].append(_model("x"))
    with pytest.raises(NoEligibleModel):
        resolve_tier(Tier.MINI)


# ── §M.3 — review role and cross-provider review ────────────────────────────


def test_require_role_prefers_the_tagged_candidate(world):
    models, assign = world
    models += [_model("general"), _model("reviewer")]
    assign(Tier.COMPLEX, "general", priority=1)
    assign(Tier.COMPLEX, "reviewer", priority=2, role="review")
    assert resolve_tier(Tier.COMPLEX, Constraints(require_role="review")).row_id == "reviewer"


def test_require_role_falls_back_to_general_candidates(world):
    """A preference, not a filter (§M.3a).

    On a single-model deployment author and reviewer coincide. That is a
    visible degradation the admin can see in the UI — not a failed stage.
    """
    models, assign = world
    models.append(_model("only"))
    assign(Tier.COMPLEX, "only", priority=1)
    assert resolve_tier(Tier.COMPLEX, Constraints(require_role="review")).row_id == "only"


def test_distinct_from_family_excludes_the_same_family(world):
    models, assign = world
    models += [_model("claude", family="anthropic"), _model("gpt", family="openai")]
    assign(Tier.MEDIUM, "claude", priority=1)
    assign(Tier.MEDIUM, "gpt", priority=2)
    r = resolve_tier(Tier.MEDIUM, Constraints(distinct_from_family="anthropic"))
    assert r.family == "openai"


def test_single_family_deployment_cannot_satisfy_distinct_review(world):
    """Correct and honest: the admin is warned at assignment time (§M.3b)."""
    models, assign = world
    models.append(_model("claude", family="anthropic"))
    assign(Tier.MEDIUM, "claude", priority=1)
    assign(Tier.SIMPLE, "claude", priority=1)
    with pytest.raises(NoEligibleModel):
        resolve_tier(Tier.MEDIUM, Constraints(distinct_from_family="anthropic"))


# ── §M.4 — budget pressure ──────────────────────────────────────────────────


def test_budget_pressure_reorders_survivors_by_cost(world):
    models, assign = world
    models += [_model("dear", cost_per_1m_output=75.0),
               _model("cheap", cost_per_1m_output=1.0)]
    assign(Tier.COMPLEX, "dear", priority=1)
    assign(Tier.COMPLEX, "cheap", priority=2)
    assert resolve_tier(Tier.COMPLEX).row_id == "dear"
    r = resolve_tier(Tier.COMPLEX, Constraints(budget_state="nearing_cap"))
    assert r.row_id == "cheap"


def test_budget_pressure_never_admits_a_filtered_candidate(world):
    """Reordering happens AFTER filtering. Cost pressure must not buy a way
    past the privacy constraint — the cheapest model is often the cloud one."""
    models, assign = world
    models += [_model("cheap_ext", privacy_class=_EXTERNAL, cost_per_1m_output=0.1),
               _model("dear_loc", family="ollama", privacy_class=_LOCAL,
                      cost_per_1m_output=50.0)]
    assign(Tier.COMPLEX, "cheap_ext", priority=1)
    assign(Tier.COMPLEX, "dear_loc", priority=2)
    r = resolve_tier(Tier.COMPLEX,
                     Constraints(no_cloud_egress=True, budget_state="nearing_cap"))
    assert r.row_id == "dear_loc"


def test_unpriced_model_sorts_last_under_pressure(world):
    """Unpriced is unmeasured, not free. Treating "unknown" as zero is how a
    budget control quietly stops working."""
    models, assign = world
    models += [_model("unpriced"), _model("priced", cost_per_1m_output=40.0)]
    assign(Tier.COMPLEX, "unpriced", priority=1)
    assign(Tier.COMPLEX, "priced", priority=2)
    assert resolve_tier(Tier.COMPLEX,
                        Constraints(budget_state="nearing_cap")).row_id == "priced"


# ── Availability ────────────────────────────────────────────────────────────


def test_open_breaker_skips_to_the_next_priority(world, monkeypatch):
    models, assign = world
    models += [_model("down", family="anthropic"), _model("up", family="openai")]
    assign(Tier.COMPLEX, "down", priority=1)
    assign(Tier.COMPLEX, "up", priority=2)
    monkeypatch.setattr(tr, "_breaker_is_open",
                        lambda slug, mid: slug == "anthropic")
    assert resolve_tier(Tier.COMPLEX).row_id == "up"


def test_breaker_lookup_failure_does_not_drop_a_candidate(world, monkeypatch):
    """Fails OPEN, unlike the privacy filter. An unavailable breaker tells us
    nothing about the model; missing privacy metadata tells us we cannot prove
    the model is safe."""
    import core.circuit_breaker as cb

    models, assign = world
    models.append(_model("m"))
    assign(Tier.COMPLEX, "m", priority=1)

    class _Exploding(dict):
        def get(self, *a, **k):
            raise RuntimeError("redis is down")

    monkeypatch.setattr(tr, "_breaker_is_open", tr._breaker_is_open)
    monkeypatch.setattr(cb, "_breakers", _Exploding())
    assert resolve_tier(Tier.COMPLEX).row_id == "m"


def test_resolving_does_not_create_breakers(world):
    """get_breaker() registers a singleton as a side effect; resolving a tier
    must not mutate global state."""
    import core.circuit_breaker as cb

    models, assign = world
    models.append(_model("m"))
    assign(Tier.COMPLEX, "m", priority=1)
    before = set(cb._breakers)
    tr._breaker_is_open("anthropic", "model-m")
    assert set(cb._breakers) == before


# ── Channel scoping ─────────────────────────────────────────────────────────


def test_channel_is_passed_through_to_the_registry(world, monkeypatch):
    seen: list = []
    monkeypatch.setattr(reg, "get_enabled_models",
                        lambda channel=None: seen.append(channel) or [])
    _, assign = world
    assign(Tier.COMPLEX, "x", priority=1)
    with pytest.raises(NoEligibleModel):
        resolve_tier(Tier.COMPLEX, channel="cli")
    assert "cli" in seen


# ── The user path ───────────────────────────────────────────────────────────


def test_resolve_explicit_ignores_tier_assignments(world):
    """A model with no tier is still directly selectable (plan.html §H)."""
    models, _ = world
    models.append(_model("free", model_id="some-model"))
    r = tr.resolve_explicit("some-model")
    assert r.row_id == "free"
    assert r.tier is None and r.requested_tier is None
    assert r.selection_mode == "explicit"


def test_resolve_explicit_does_not_substitute(world):
    """An invalid pick is an error, not a silent downgrade."""
    models, _ = world
    models.append(_model("other", model_id="something-else"))
    with pytest.raises(NoEligibleModel):
        tr.resolve_explicit("not-configured")
