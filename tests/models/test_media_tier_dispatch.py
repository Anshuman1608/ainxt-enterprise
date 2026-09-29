# SPDX-License-Identifier: MIT
"""Phase 6 §N.1 step 5 — resolving the two output-modality tiers.

Image generation and video generation never went through ModelRouter: one
hard-pinned provider="gemini" and the other called veo_model(). So there was
no hint to rename, and the migration needed a resolve-only entry point
instead — resolve_media_model().

The behaviour that matters most here is the REFUSAL. Both tiers are in
_NO_ENV_FALLBACK because every fallback available to them answers with text,
and "generate a video of a tiger" answered with a paragraph about tigers is
a confusing wrong answer where an error is a fixable one.
"""

from __future__ import annotations

import pytest

import core.llm_provider_registry as reg
import core.tier_resolver as tr
from core.tier_resolver import NoEligibleModel
from core.tiers import Tier
from models.model_router import resolve_media_model


def _model(row_id, *, family, modality, model_id=None, caps=None):
    c = {"modality": list(modality), "privacy_class": "external"}
    c.update(caps or {})
    return {
        "id": row_id, "model_id": model_id or row_id, "display_name": row_id,
        "capabilities": c, "is_default": False, "sort_order": 0,
        "provider_id": "p", "provider_slug": family, "provider_name": family,
        "family": family, "base_url": None,
    }


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    models: list = []
    assignments: list = []
    monkeypatch.setattr(reg, "get_enabled_models", lambda channel=None: list(models))
    monkeypatch.setattr(tr, "get_tier_assignments", lambda: list(assignments))
    monkeypatch.setattr(tr, "_breaker_is_open", lambda slug, mid: False)

    def assign(tier, row_id, priority=1):
        assignments.append({"tier": tier.value, "model_row_id": row_id,
                            "priority": priority, "role": None, "org_id": "default"})
    return models, assign


def test_video_generation_resolves_to_the_assignment(registry):
    models, assign = registry
    models.append(_model("veo", family="gemini", modality=["video-out"],
                         model_id="veo-3.1-generate-preview"))
    assign(Tier.VIDEO_GENERATION, "veo")

    rm = resolve_media_model(Tier.VIDEO_GENERATION)
    assert rm.model_id == "veo-3.1-generate-preview"
    assert rm.family == "gemini"


def test_image_output_resolves_to_the_assignment(registry):
    models, assign = registry
    models.append(_model("img", family="gemini", modality=["image-out"],
                         model_id="gemini-3.1-flash-image"))
    assign(Tier.IMAGE_OUTPUT, "img")
    assert resolve_media_model(Tier.IMAGE_OUTPUT).model_id == "gemini-3.1-flash-image"


def test_a_non_gemini_image_family_resolves_rather_than_being_filtered(registry):
    """The tier is provider-neutral by construction: an OpenAI image model
    assigned to image-output must come back as one. Whether this platform has
    a gateway for that family is the CALLER's question (chat_router answers
    it with a 503 naming the family), not the resolver's."""
    models, assign = registry
    models.append(_model("oai", family="openai", modality=["image-out"],
                         model_id="gpt-image-1"))
    assign(Tier.IMAGE_OUTPUT, "oai")

    rm = resolve_media_model(Tier.IMAGE_OUTPUT)
    assert (rm.family, rm.model_id) == ("openai", "gpt-image-1")


# ── The refusal, which is the point ───────────────────────────────────────


@pytest.mark.parametrize("tier", [Tier.IMAGE_OUTPUT, Tier.VIDEO_GENERATION])
def test_an_unassigned_output_tier_raises_instead_of_degrading(registry, tier):
    registry  # nothing assigned
    with pytest.raises(NoEligibleModel):
        resolve_media_model(tier)


@pytest.mark.parametrize("tier,wrong_modality", [
    (Tier.IMAGE_OUTPUT, ["text"]),
    (Tier.VIDEO_GENERATION, ["image-out"]),
])
def test_a_model_that_cannot_serve_the_modality_is_not_substituted(
        registry, tier, wrong_modality):
    """An administrator CAN assign a text model to video-generation. The
    resolver must reject it on capability rather than dispatch it and let the
    user discover the problem as a paragraph where a video should be."""
    models, assign = registry
    models.append(_model("wrong", family="gemini", modality=wrong_modality))
    assign(tier, "wrong")
    with pytest.raises(NoEligibleModel):
        resolve_media_model(tier)


def test_the_governance_flag_does_not_gate_media_resolution(registry, monkeypatch):
    """Unlike the text tiers, these two ignore TIER_GOVERNANCE_ENABLED.

    The flag's contract is "off restores the previous behaviour". For these
    paths the previous behaviour was a hardcoded provider, so there is no
    prior routing to restore — only a hardcode to remove (R3). Honouring the
    flag here would mean shipping a switch that turns the hardcode back on.
    """
    models, assign = registry
    models.append(_model("veo", family="gemini", modality=["video-out"],
                         model_id="veo-3.1-generate-preview"))
    assign(Tier.VIDEO_GENERATION, "veo")

    monkeypatch.delenv("TIER_GOVERNANCE_ENABLED", raising=False)
    assert resolve_media_model(Tier.VIDEO_GENERATION).model_id == "veo-3.1-generate-preview"
    monkeypatch.setenv("TIER_GOVERNANCE_ENABLED", "true")
    assert resolve_media_model(Tier.VIDEO_GENERATION).model_id == "veo-3.1-generate-preview"


def test_priority_order_is_the_fallback_order(registry):
    """§M.5 — the tier's priority column IS the primary/fallback chain that
    PRIMARY_VISION_PROVIDER + FALLBACK_VISION_PROVIDER used to hardcode, with
    no two-entry limit."""
    models, assign = registry
    models.append(_model("second", family="openai", modality=["image-out"]))
    models.append(_model("first", family="gemini", modality=["image-out"]))
    assign(Tier.IMAGE_OUTPUT, "second", priority=2)
    assign(Tier.IMAGE_OUTPUT, "first", priority=1)
    assert resolve_media_model(Tier.IMAGE_OUTPUT).model_id == "first"
