# SPDX-License-Identifier: MIT
"""Phase 5 — the precedence inversion, behind TIER_GOVERNANCE_ENABLED.

The claim this phase makes is narrow and testable: with the flag ON, a request
for a CAPABILITY is answered by the administrator's tier assignments; with it
OFF, by the .env model constants, exactly as before. Everything here exists to
hold one half of that or the boundary between them.

The boundary is the interesting part. Only seven of the router's sixteen
internal tiers may be governed — see _LEGACY_TO_GOVERNED. Governing one of the
other nine would silently substitute a model for one a USER named, which is
the failure this migration exists to remove rather than introduce, so the
exclusions are asserted as explicitly as the inclusions.

No DB and no Redis: the registry and the assignment table are served from
in-memory lists, following tests/core/test_tier_resolver.py.
"""

from __future__ import annotations

import pytest

import core.llm_provider_registry as reg
import core.tier_resolver as tr
import models.model_router as mr
from core.tiers import Tier
from models.model_router import ModelRouter

_LOCAL = "deployment_local"
_EXTERNAL = "external"


def _model(row_id, *, family="anthropic", model_id=None, **caps):
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


class _Gw:
    def __init__(self, log, mode="ok"):
        self.log = log
        self.mode = mode
        self.available = True
        self._last_selected_model = "picked"
        self._last_input_tokens = self._last_output_tokens = 0
        self._last_cache_read_tokens = self._last_cache_creation_tokens = 0

    def generate(self, prompt, model=None, **kw):
        self.log.append(model)
        if self.mode == "raise":
            raise RuntimeError("boom")
        return [f"<{model}>"]


@pytest.fixture
def world(monkeypatch):
    """In-memory registry + assignments + a recording gateway for every family."""
    models: list[dict] = []
    assignments: list[dict] = []
    log: list = []
    gw = _Gw(log)

    monkeypatch.setattr(reg, "get_enabled_models", lambda channel=None: list(models))
    monkeypatch.setattr(tr, "get_tier_assignments", lambda: list(assignments))
    monkeypatch.setattr(tr, "_breaker_is_open", lambda slug, mid: False)
    # The governed path resolves its gateway from the registry row; the legacy
    # chains use the four cached singletons. Both are pointed at the same fake
    # so a test can tell the two apart by the MODEL that was requested rather
    # than by which object answered.
    monkeypatch.setattr(ModelRouter, "_gateway_for_registry_family",
                        lambda self, family, model_id: gw)
    for acc in ("_get_local", "_get_openai", "_get_claude", "_get_gemini"):
        monkeypatch.setattr(ModelRouter, acc, lambda self, _g=gw: _g)
    monkeypatch.setattr(mr, "_resolve_tier_model",
                        lambda env_value, family, tag: f"ENV:{family}/{tag}")
    monkeypatch.setattr(mr, "_tier_label", lambda t: f"LABEL[{t}]")
    # Fresh per test: the warning is once per PROCESS by design, which would
    # otherwise make the ordering of tests significant.
    monkeypatch.setattr(mr, "_ENV_FALLBACK_WARNED", set())

    def assign(tier, row_id, priority=100, role=None):
        assignments.append({"tier": tier.value, "model_row_id": row_id,
                            "priority": priority, "role": role, "org_id": "default"})

    class World:
        pass
    w = World()
    w.models, w.assign, w.log, w.gw = models, assign, log, gw
    return w


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("TIER_GOVERNANCE_ENABLED", "true")


@pytest.fixture
def off(monkeypatch):
    monkeypatch.delenv("TIER_GOVERNANCE_ENABLED", raising=False)


# ── 1. Precedence: the whole point of the phase ─────────────────────────────

# hint → the tier it is governed as. Every entry in _LEGACY_TO_GOVERNED.
_GOVERNED_HINTS = [
    ("mini", Tier.MINI), ("haiku", Tier.SIMPLE), ("medium", Tier.MEDIUM),
    ("complex", Tier.COMPLEX), ("solution", Tier.COMPLEX),
    ("deep", Tier.COMPLEX), ("vision", Tier.IMAGE_INPUT),
]


@pytest.mark.parametrize("hint,tier", _GOVERNED_HINTS)
def test_flag_on_selects_the_assignment_not_the_env_constant(world, on, hint, tier):
    modality = "image-in" if tier is Tier.IMAGE_INPUT else "text"
    world.models.append(_model("a", modality=[modality], privacy_class=_EXTERNAL))
    world.assign(tier, "a")
    # The solution hint asks for role=review, which is a PREFERENCE — an
    # unroled candidate still serves it (§M.3a).
    d = ModelRouter().route("q", model_hint=hint)
    assert d.tier == mr.TIER_GOVERNED
    assert d.provider_model_override == "model-a"
    assert d.requested_tier is tier


@pytest.mark.parametrize("hint,tier", _GOVERNED_HINTS)
def test_flag_off_ignores_the_assignment_entirely(world, off, hint, tier):
    modality = "image-in" if tier is Tier.IMAGE_INPUT else "text"
    world.models.append(_model("a", modality=[modality], privacy_class=_EXTERNAL))
    world.assign(tier, "a")
    d = ModelRouter().route("q", model_hint=hint)
    assert d.tier != mr.TIER_GOVERNED
    assert d.resolved is None


# ── 2. The nine tiers governance must NOT touch ─────────────────────────────

@pytest.mark.parametrize("hint", ["gemini", "opus-4-8", "opus-5", "sonnet-5",
                                  "tera", "luna", "local", "simple", "local_mini"])
def test_a_users_sku_pick_is_never_resolved_through_a_tier(world, on, hint):
    """Governance decides what the PLATFORM picks, never what a user picked.

    Each of these names a specific model or a deployment topology. Resolving
    them through a tier would substitute something else for what was asked
    for — and would do it silently, which is the exact defect the tier model
    is meant to remove.
    """
    world.models.append(_model("a", modality=["text"], privacy_class=_EXTERNAL))
    for t in Tier:
        world.assign(t, "a")
    d = ModelRouter().route("q", model_hint=hint)
    assert d.tier != mr.TIER_GOVERNED, f"{hint} must not be governed"


def test_the_governed_set_is_exactly_the_seven_capability_tiers():
    assert set(mr._LEGACY_TO_GOVERNED) == {
        mr.TIER_MINI, mr.TIER_HAIKU, mr.TIER_MEDIUM, mr.TIER_COMPLEX,
        mr.TIER_SOLUTION, mr.TIER_DEEP, mr.TIER_VISION,
    }


def test_the_role_constant_matches_the_resolvers(world):
    """_ROLE_REVIEW is duplicated to keep the router's import surface narrow.

    A duplicated constant is only safe while something checks it, and the
    failure it would cause is silent: require_role would simply never match,
    so §M.3a's reviewer preference would stop applying with no error anywhere.
    """
    assert mr._ROLE_REVIEW == tr.ROLE_REVIEW


# ── 3. Falling back to the env constants ────────────────────────────────────

def test_an_unassigned_tier_falls_back_and_warns_once_per_process(world, on, caplog):
    world.models.append(_model("a", modality=["text"], privacy_class=_EXTERNAL))
    # No assignment for medium at all.
    r = ModelRouter()
    with caplog.at_level("WARNING"):
        first = r.route("q", model_hint="medium")
        second = r.route("q", model_hint="medium")
    assert first.tier == mr.TIER_MEDIUM and second.tier == mr.TIER_MEDIUM
    hits = [rec for rec in caplog.records if "DEPRECATED .env model constants" in rec.getMessage()]
    assert len(hits) == 1, "the fallback warning must not fire once per request"


def test_a_resolver_failure_degrades_to_the_legacy_chain(world, on, monkeypatch):
    """Governance is an improvement, not a new way for routing to break."""
    def _boom(*a, **kw):
        raise RuntimeError("registry down")
    monkeypatch.setattr(tr, "resolve_tier_candidates", _boom)
    d = ModelRouter().route("q", model_hint="complex")
    assert d.tier == mr.TIER_COMPLEX


# ── 4. §M.1 — privacy as a constraint, not a tier rewrite ───────────────────

def test_confidential_data_keeps_its_tier_and_narrows_the_candidates(world, on):
    world.models.append(_model("cloud", modality=["text"], privacy_class=_EXTERNAL))
    world.models.append(_model("onprem", family="ollama", modality=["text"],
                               privacy_class=_LOCAL))
    world.assign(Tier.COMPLEX, "cloud", priority=1)
    world.assign(Tier.COMPLEX, "onprem", priority=2)

    d = ModelRouter().route("q", model_hint="complex", data_classification="RESTRICTED")
    # The BEFORE behaviour rewrote the tier to `simple`, running a hard
    # reasoning task on the smallest local model. The tier is now untouched.
    assert d.requested_tier is Tier.COMPLEX
    assert d.provider_model_override == "model-onprem"


def test_confidential_data_fails_closed_when_nothing_is_deployment_local(world, on):
    world.models.append(_model("cloud", modality=["text"], privacy_class=_EXTERNAL))
    world.assign(Tier.COMPLEX, "cloud")
    world.assign(Tier.MEDIUM, "cloud")     # the ladder's next rung, also external

    from core.tier_resolver import NoEligibleModel
    with pytest.raises(NoEligibleModel):
        ModelRouter().route("q", model_hint="complex", data_classification="RESTRICTED")


def test_generate_turns_a_fail_closed_resolution_into_an_error_string(world, on):
    """generate()'s contract is that it never raises, including here."""
    world.models.append(_model("cloud", modality=["text"], privacy_class=_EXTERNAL))
    world.assign(Tier.COMPLEX, "cloud")
    out = ModelRouter().generate("q", model_hint="complex",
                                 data_classification="RESTRICTED")
    assert out.startswith("Error: this request carries data")
    assert world.log == [], "no gateway may be called for a fail-closed turn"


def test_privacy_never_walks_the_fallback_ladder(world, on):
    """A weaker tier's model is still a model that may be external (§M.5)."""
    world.models.append(_model("onprem", family="ollama", modality=["text"],
                               privacy_class=_LOCAL))
    world.assign(Tier.MEDIUM, "onprem")    # complex → medium is the ladder
    from core.tier_resolver import NoEligibleModel
    with pytest.raises(NoEligibleModel):
        ModelRouter().route("q", model_hint="complex", data_classification="RESTRICTED")


# ── 5. §M.2 — context size filters the tier, it no longer switches tier ─────

def test_a_large_context_picks_a_wide_model_within_the_same_tier(world, on):
    world.models.append(_model("narrow", modality=["text"], privacy_class=_EXTERNAL,
                               context_window=8_000))
    world.models.append(_model("wide", modality=["text"], privacy_class=_EXTERNAL,
                               context_window=1_000_000))
    world.assign(Tier.COMPLEX, "narrow", priority=1)
    world.assign(Tier.COMPLEX, "wide", priority=2)

    d = ModelRouter().route("q", model_hint="complex", context_tokens=500_000)
    assert d.requested_tier is Tier.COMPLEX, "the tier must not be promoted"
    assert d.provider_model_override == "model-wide"


def test_a_small_context_keeps_the_admins_priority_order(world, on):
    world.models.append(_model("narrow", modality=["text"], privacy_class=_EXTERNAL,
                               context_window=8_000))
    world.models.append(_model("wide", modality=["text"], privacy_class=_EXTERNAL,
                               context_window=1_000_000))
    world.assign(Tier.COMPLEX, "narrow", priority=1)
    world.assign(Tier.COMPLEX, "wide", priority=2)
    d = ModelRouter().route("q", model_hint="complex", context_tokens=100)
    assert d.provider_model_override == "model-narrow"


# ── 6. §M.5 — within-tier fallback happens at DISPATCH time ─────────────────

def test_a_failing_candidate_yields_to_the_next_one(world, on, monkeypatch):
    """The resolver filters candidates whose breaker is ALREADY open. A call
    that fails right now is a different question, and only the dispatcher can
    answer it — one failure does not open a breaker."""
    world.models.append(_model("first", modality=["text"], privacy_class=_EXTERNAL))
    world.models.append(_model("second", modality=["text"], privacy_class=_EXTERNAL))
    world.assign(Tier.COMPLEX, "first", priority=1)
    world.assign(Tier.COMPLEX, "second", priority=2)

    calls = []

    class _Flaky:
        available = True
        _last_selected_model = None

        def generate(self, prompt, model=None, **kw):
            calls.append(model)
            if model == "model-first":
                raise RuntimeError("upstream 500")
            return [f"<{model}>"]

    monkeypatch.setattr(ModelRouter, "_gateway_for_registry_family",
                        lambda self, family, model_id: _Flaky())
    r = ModelRouter()
    out = r.generate("q", model_hint="complex")
    assert calls == ["model-first", "model-second"]
    assert out == "<model-second>"
    # "tier", not "fallback": the LADDER was never walked, a sibling candidate
    # within the same tier served the request.
    assert r.last_selection_mode == "tier"
    assert r.last_requested_tier == "complex"


# ── 7. §L.5 — the audit columns ─────────────────────────────────────────────

def test_the_legacy_path_records_no_provenance(world, off):
    r = ModelRouter()
    r.generate("q", model_hint="complex")
    assert r.last_selection_mode is None
    assert r.last_requested_tier is None


def test_walking_the_ladder_is_recorded_as_fallback(world, on):
    world.models.append(_model("m", modality=["text"], privacy_class=_EXTERNAL))
    world.assign(Tier.MEDIUM, "m")          # complex is unassigned; the ladder runs
    r = ModelRouter()
    r.generate("q", model_hint="complex")
    assert r.last_selection_mode == "fallback"
    assert r.last_requested_tier == "complex"


# ── 8. The modality tiers reach a real dispatch for the first time ──────────

def test_video_generation_dispatches_when_a_model_can_serve_it(world, on):
    world.models.append(_model("veo", family="gemini", modality=["video-out"],
                               privacy_class=_EXTERNAL))
    world.assign(Tier.VIDEO_GENERATION, "veo")
    d = ModelRouter().route("make a video", tier=Tier.VIDEO_GENERATION)
    assert d.provider_model_override == "model-veo"


def test_image_output_raises_rather_than_substituting_a_text_model(world, on):
    """Nothing substitutes for image generation, so there is no ladder to walk.

    A caller that asked for an image and received prose gets a confusing wrong
    answer; an error gets a fixable one.
    """
    world.models.append(_model("text-only", modality=["text"], privacy_class=_EXTERNAL))
    world.assign(Tier.IMAGE_OUTPUT, "text-only")
    from core.tier_resolver import NoEligibleModel
    with pytest.raises(NoEligibleModel) as exc:
        ModelRouter().route("draw", tier=Tier.IMAGE_OUTPUT)
    assert "image-out" in str(exc.value)


# ── 9. §M.4 — the breaker rekey must not retune the LLM breakers ────────────

def test_a_per_model_breaker_inherits_its_providers_tuning():
    from core.circuit_breaker import _BREAKER_DEFAULTS, _defaults_for
    assert _defaults_for("openai:gpt-5.4") == _BREAKER_DEFAULTS["openai"]
    # A model id containing its own colon must not confuse the split.
    assert _defaults_for("local:llama3.2:1b") == _BREAKER_DEFAULTS["local"]
    # An unknown provider still gets the documented generic default.
    assert _defaults_for("acme-inc:some-model") == (10, 30)
