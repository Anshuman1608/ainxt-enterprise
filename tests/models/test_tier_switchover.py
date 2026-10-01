# SPDX-License-Identifier: MIT
"""Phase 5's precedence inversion, unconditional since Phase 8 (D106, D107).

A request for a CAPABILITY is answered by the administrator's tier
assignments, and a tier with nothing eligible raises NoEligibleModel instead of
falling back to .env model constants. The TIER_GOVERNANCE_ENABLED=off half of
this file was retired with the switch.

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

    def assign(tier, row_id, priority=100, role=None):
        assignments.append({"tier": tier.value, "model_row_id": row_id,
                            "priority": priority, "role": role, "org_id": "default"})

    class World:
        pass
    w = World()
    w.models, w.assign, w.log, w.gw = models, assign, log, gw
    return w


# ── 1. Precedence: the whole point of the phase ─────────────────────────────

# hint → the tier it is governed as. Every entry in _LEGACY_TO_GOVERNED.
_GOVERNED_HINTS = [
    ("mini", Tier.MINI), ("haiku", Tier.SIMPLE), ("medium", Tier.MEDIUM),
    ("complex", Tier.COMPLEX), ("solution", Tier.COMPLEX),
    ("deep", Tier.COMPLEX), ("vision", Tier.IMAGE_INPUT),
]


@pytest.mark.parametrize("hint,tier", _GOVERNED_HINTS)
def test_a_capability_hint_selects_the_assignment(world, hint, tier):
    modality = "image-in" if tier is Tier.IMAGE_INPUT else "text"
    world.models.append(_model("a", modality=[modality], privacy_class=_EXTERNAL))
    world.assign(tier, "a")
    # The solution hint asks for role=review, which is a PREFERENCE — an
    # unroled candidate still serves it (§M.3a).
    d = ModelRouter().route("q", model_hint=hint)
    assert d.tier == mr.TIER_GOVERNED
    assert d.provider_model_override == "model-a"
    assert d.requested_tier is tier


# ── 2. Every hint resolves without env (D108) ───────────────────────────────

@pytest.mark.parametrize("hint,model_id,family", [
    ("opus-5", "claude-opus-5", "anthropic"), ("opus-4-8", "claude-opus-4-8", "anthropic"),
    ("sonnet-5", "claude-sonnet-5", "anthropic"), ("tera", "gpt-5.6-terra", "openai"),
    ("luna", "gpt-5.6-luna", "openai"), ("gemini-lite", "gemini-3.1-flash-lite", "gemini"),
])
def test_a_sku_alias_is_served_as_the_registry_model_it_names(world, hint, model_id, family):
    """Governance decides what the PLATFORM picks, never what a user named."""
    world.models.append(_model("named", family=family, model_id=model_id, privacy_class=_EXTERNAL))
    world.models.append(_model("other", family=family, modality=["text"], privacy_class=_EXTERNAL))
    for t in Tier:
        world.assign(t, "other")
    d = ModelRouter().route("q", model_hint=hint)
    assert d.tier == mr.TIER_REGISTRY and d.provider_model_override == model_id


def test_a_sku_alias_with_no_registered_model_is_refused(world):
    world.models.append(_model("a", modality=["text"], privacy_class=_EXTERNAL))
    from core.tier_resolver import NoEligibleModel
    with pytest.raises(NoEligibleModel, match="no enabled registry model"):
        ModelRouter().route("q", model_hint="opus-5")


@pytest.mark.parametrize("hint,tier,no_cloud", [
    ("simple", Tier.SIMPLE, False), ("local", Tier.SIMPLE, True),
    ("local_mini", Tier.INTENT_CLASSIFICATION, True),
])
def test_topology_aliases_resolve_through_their_tier(world, hint, tier, no_cloud):
    world.models.append(_model("cloud", modality=["text"], privacy_class=_EXTERNAL))
    world.models.append(_model("onprem", family="ollama", modality=["text"], privacy_class=_LOCAL))
    world.assign(tier, "cloud", priority=1)
    world.assign(tier, "onprem", priority=2)
    d = ModelRouter().route("q", model_hint=hint)
    assert d.requested_tier is tier
    assert d.provider_model_override == ("model-onprem" if no_cloud else "model-cloud")


def test_the_alias_extras_are_the_review_and_in_house_ones():
    assert mr._ALIAS_EXTRAS["solution"] == mr._ALIAS_EXTRAS["opus"] == {"require_role": tr.ROLE_REVIEW}
    assert all(v == {"no_cloud_egress": True} for k, v in mr._ALIAS_EXTRAS.items()
               if k not in ("solution", "opus"))


def test_the_role_constant_matches_the_resolvers(world):
    """_ROLE_REVIEW is duplicated to keep the router's import surface narrow.

    A duplicated constant is only safe while something checks it, and the
    failure it would cause is silent: require_role would simply never match,
    so §M.3a's reviewer preference would stop applying with no error anywhere.
    """
    assert mr._ROLE_REVIEW == tr.ROLE_REVIEW


# ── 3. An unassigned tier fails loudly (D107) ───────────────────────────────

def test_an_unassigned_tier_raises_instead_of_using_env_models(world):
    world.models.append(_model("a", modality=["text"], privacy_class=_EXTERNAL))
    from core.tier_resolver import NoEligibleModel
    with pytest.raises(NoEligibleModel) as exc:
        ModelRouter().route("q", model_hint="medium")
    assert exc.value.tier is Tier.MEDIUM


def test_generate_names_the_screen_that_fixes_it(world):
    out = ModelRouter().generate("q", model_hint="medium")
    assert out.startswith("Error:") and "Model Governance" in out
    assert "cloud provider" not in out          # not the privacy message


def test_a_resolver_failure_is_reported_not_routed_around(world, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("registry down")
    monkeypatch.setattr(tr, "resolve_tier_candidates", _boom)
    from core.tier_resolver import NoEligibleModel
    with pytest.raises(NoEligibleModel, match="registry down"):
        ModelRouter().route("q", model_hint="complex")


# ── 4. §M.1 — privacy as a constraint, not a tier rewrite ───────────────────

def test_confidential_data_keeps_its_tier_and_narrows_the_candidates(world):
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


def test_confidential_data_fails_closed_when_nothing_is_deployment_local(world):
    world.models.append(_model("cloud", modality=["text"], privacy_class=_EXTERNAL))
    world.assign(Tier.COMPLEX, "cloud")
    world.assign(Tier.MEDIUM, "cloud")     # the ladder's next rung, also external

    from core.tier_resolver import NoEligibleModel
    with pytest.raises(NoEligibleModel):
        ModelRouter().route("q", model_hint="complex", data_classification="RESTRICTED")


def test_generate_turns_a_fail_closed_resolution_into_an_error_string(world):
    """generate()'s contract is that it never raises, including here."""
    world.models.append(_model("cloud", modality=["text"], privacy_class=_EXTERNAL))
    world.assign(Tier.COMPLEX, "cloud")
    out = ModelRouter().generate("q", model_hint="complex",
                                 data_classification="RESTRICTED")
    assert out.startswith("Error: this request carries data")
    assert world.log == [], "no gateway may be called for a fail-closed turn"


def test_privacy_never_walks_the_fallback_ladder(world):
    """A weaker tier's model is still a model that may be external (§M.5)."""
    world.models.append(_model("onprem", family="ollama", modality=["text"],
                               privacy_class=_LOCAL))
    world.assign(Tier.MEDIUM, "onprem")    # complex → medium is the ladder
    from core.tier_resolver import NoEligibleModel
    with pytest.raises(NoEligibleModel):
        ModelRouter().route("q", model_hint="complex", data_classification="RESTRICTED")


# ── 5. §M.2 — context size filters the tier, it no longer switches tier ─────

def test_a_large_context_picks_a_wide_model_within_the_same_tier(world):
    world.models.append(_model("narrow", modality=["text"], privacy_class=_EXTERNAL,
                               context_window=8_000))
    world.models.append(_model("wide", modality=["text"], privacy_class=_EXTERNAL,
                               context_window=1_000_000))
    world.assign(Tier.COMPLEX, "narrow", priority=1)
    world.assign(Tier.COMPLEX, "wide", priority=2)

    d = ModelRouter().route("q", model_hint="complex", context_tokens=500_000)
    assert d.requested_tier is Tier.COMPLEX, "the tier must not be promoted"
    assert d.provider_model_override == "model-wide"


def test_a_small_context_keeps_the_admins_priority_order(world):
    world.models.append(_model("narrow", modality=["text"], privacy_class=_EXTERNAL,
                               context_window=8_000))
    world.models.append(_model("wide", modality=["text"], privacy_class=_EXTERNAL,
                               context_window=1_000_000))
    world.assign(Tier.COMPLEX, "narrow", priority=1)
    world.assign(Tier.COMPLEX, "wide", priority=2)
    d = ModelRouter().route("q", model_hint="complex", context_tokens=100)
    assert d.provider_model_override == "model-narrow"


# ── 6. §M.5 — within-tier fallback happens at DISPATCH time ─────────────────

def test_a_failing_candidate_yields_to_the_next_one(world, monkeypatch):
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

def test_walking_the_ladder_is_recorded_as_fallback(world):
    world.models.append(_model("m", modality=["text"], privacy_class=_EXTERNAL))
    world.assign(Tier.MEDIUM, "m")          # complex is unassigned; the ladder runs
    r = ModelRouter()
    r.generate("q", model_hint="complex")
    assert r.last_selection_mode == "fallback"
    assert r.last_requested_tier == "complex"


# ── 8. The modality tiers reach a real dispatch for the first time ──────────

def test_video_generation_dispatches_when_a_model_can_serve_it(world):
    world.models.append(_model("veo", family="gemini", modality=["video-out"],
                               privacy_class=_EXTERNAL))
    world.assign(Tier.VIDEO_GENERATION, "veo")
    d = ModelRouter().route("make a video", tier=Tier.VIDEO_GENERATION)
    assert d.provider_model_override == "model-veo"


def test_image_output_raises_rather_than_substituting_a_text_model(world):
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


# ── 10. Auto and the async entry point (D108) ───────────────────────────────

@pytest.mark.parametrize("verdict,tier", [
    ("simple", Tier.SIMPLE), ("medium", Tier.MEDIUM), ("complex", Tier.COMPLEX),
    ("deep", Tier.COMPLEX), ("something-new", Tier.MEDIUM),
])
def test_an_auto_verdict_resolves_through_its_tier(world, monkeypatch, verdict, tier):
    import models.classifier as clf
    monkeypatch.setattr(clf, "classify_with_confidence_llm", lambda p: (verdict, 0.95), raising=True)
    monkeypatch.setattr(clf, "detect_query_domain", lambda p: "chat", raising=True)
    world.models.append(_model("a", modality=["text"], privacy_class=_EXTERNAL))
    world.assign(tier, "a")
    assert ModelRouter().route("q").requested_tier is tier


def test_async_generate_keeps_the_tier(monkeypatch):
    import anyio
    seen = {}
    monkeypatch.setattr(ModelRouter, "generate",
                        lambda self, prompt, **kw: seen.update(kw) or "ok")
    out = anyio.run(lambda: ModelRouter().async_generate("q", tier=Tier.SIMPLE, legacy_hint="simple"))
    assert out == "ok" and seen["tier"] is Tier.SIMPLE and "model_hint" not in seen


def test_a_registry_id_is_served_as_itself_not_classified(world):
    """An id no alias names (an admin-added model) is the user's pick (§G)."""
    world.models.append(_model("custom", model_id="my-custom-model", privacy_class=_EXTERNAL))
    world.models.append(_model("auto", modality=["text"], privacy_class=_EXTERNAL))
    for t in Tier:
        world.assign(t, "auto")
    d = ModelRouter().route("q", model_hint="my-custom-model")
    assert d.tier == mr.TIER_REGISTRY and d.provider_model_override == "my-custom-model"
