# SPDX-License-Identifier: MIT
# ============================================================
# routers/feature_model_config_router.py — validation rules.
#
# Server-side validation is the part that matters here. The UI also filters the
# dropdown, but UI filtering is bypassable by any API client and both failure
# modes it prevents are SILENT at runtime:
#
#   * a text-only model on a vision feature just returns nonsense;
#   * a cloud model on a CONFIDENTIAL feature is overridden on-premise by
#     route()'s privacy floor, so the admin believes an assignment took effect
#     that never can.
#
# _capability_conflicts is tested directly rather than through the HTTP layer:
# the endpoints are thin wrappers over it plus a session, and exercising them
# would need a live DB, which puts these cases out of reach of tests/core/.
# ============================================================

from __future__ import annotations

import pytest

from routers.feature_model_config_router import (
    _LOCAL_ONLY_CLASSIFICATIONS, AssignmentUpsert, _capability_conflicts,
)


class _Feature:
    """Stand-in for a FeatureRegistry row — only the declared columns are read."""

    def __init__(self, **kw):
        self.feature_key = kw.get("feature_key", "test.feature")
        self.requires_vision = kw.get("requires_vision", False)
        self.requires_tools = kw.get("requires_tools", False)
        self.requires_streaming = kw.get("requires_streaming", False)
        self.min_context_tokens = kw.get("min_context_tokens")
        self.max_data_classification = kw.get("max_data_classification", "INTERNAL")


def _model(**kw) -> dict:
    return {
        "id": kw.get("id", "uuid-1"),
        "model_id": kw.get("model_id", "some-model"),
        "display_name": kw.get("display_name", "Some Model"),
        "provider_name": kw.get("provider_name", "OpenRouter"),
        "family": kw.get("family", "openai_compatible"),
        "capabilities": kw.get("capabilities", {}),
    }


# ── sparse metadata must not block an admin ──────────────────────────────────

def test_a_model_with_no_capability_metadata_is_allowed() -> None:
    # llm_models.capabilities is routinely sparse for admin-added models.
    # Refusing every unverifiable model would make the screen unusable, which
    # is worse than letting an admin make a choice we cannot check.
    feature = _Feature(requires_vision=True, requires_tools=True, min_context_tokens=200000)
    assert _capability_conflicts(feature, _model(capabilities={})) == []


# ── vision ───────────────────────────────────────────────────────────────────

def test_vision_feature_rejects_a_model_marked_text_only() -> None:
    feature = _Feature(feature_key="docs.ocr", requires_vision=True)
    problems = _capability_conflicts(feature, _model(capabilities={"modality": "text"}))
    assert problems and "image input" in problems[0]


def test_vision_feature_rejects_an_explicit_supports_vision_false() -> None:
    feature = _Feature(requires_vision=True)
    assert _capability_conflicts(feature, _model(capabilities={"supports_vision": False}))


@pytest.mark.parametrize("modality", ["image", "text+image", "vision", "multimodal-vision"])
def test_vision_feature_accepts_an_image_capable_model(modality: str) -> None:
    feature = _Feature(requires_vision=True)
    assert _capability_conflicts(feature, _model(capabilities={"modality": modality})) == []


# ── tools ────────────────────────────────────────────────────────────────────

def test_tool_feature_rejects_a_model_marked_without_tool_support() -> None:
    feature = _Feature(requires_tools=True)
    problems = _capability_conflicts(feature, _model(capabilities={"supports_tools": False}))
    assert problems and "tool calling" in problems[0]


def test_tool_feature_accepts_a_tool_capable_model() -> None:
    feature = _Feature(requires_tools=True)
    assert _capability_conflicts(feature, _model(capabilities={"supports_tools": True})) == []


# ── context window ───────────────────────────────────────────────────────────

def test_context_window_below_the_requirement_is_rejected() -> None:
    feature = _Feature(min_context_tokens=128000)
    problems = _capability_conflicts(feature, _model(capabilities={"context_window": 8000}))
    assert problems and "context tokens" in problems[0]


def test_context_window_at_or_above_the_requirement_is_accepted() -> None:
    feature = _Feature(min_context_tokens=128000)
    assert _capability_conflicts(feature, _model(capabilities={"context_window": 128000})) == []
    assert _capability_conflicts(feature, _model(capabilities={"context_window": 1000000})) == []


def test_a_non_numeric_context_window_is_not_treated_as_a_failure() -> None:
    # Discovery has been known to write strings here.
    feature = _Feature(min_context_tokens=128000)
    assert _capability_conflicts(feature, _model(capabilities={"context_window": "128k"})) == []


# ── privacy: the one check that is enforced unconditionally ──────────────────

@pytest.mark.parametrize("classification", sorted(_LOCAL_ONLY_CLASSIFICATIONS))
@pytest.mark.parametrize("family", ["anthropic", "openai", "gemini", "openai_compatible"])
def test_local_only_feature_rejects_every_cloud_family(
    classification: str, family: str,
) -> None:
    feature = _Feature(feature_key="hr.records", max_data_classification=classification)
    problems = _capability_conflicts(feature, _model(family=family))
    assert problems, f"{classification} feature accepted a {family} model"
    assert "on-premise" in problems[0]
    # The message must say the assignment would be INEFFECTIVE, not merely
    # disallowed — that is the part an admin needs to understand.
    assert "privacy floor" in problems[0]


@pytest.mark.parametrize("classification", sorted(_LOCAL_ONLY_CLASSIFICATIONS))
def test_local_only_feature_accepts_an_on_premise_model(classification: str) -> None:
    feature = _Feature(max_data_classification=classification)
    assert _capability_conflicts(feature, _model(family="ollama")) == []


@pytest.mark.parametrize("classification", ["PUBLIC", "INTERNAL"])
def test_non_sensitive_feature_accepts_a_cloud_model(classification: str) -> None:
    feature = _Feature(max_data_classification=classification)
    assert _capability_conflicts(feature, _model(family="anthropic")) == []


def test_privacy_is_enforced_even_with_no_capability_metadata() -> None:
    # Unlike the capability checks, this one is based on the provider's family,
    # which is always populated — so sparse metadata is no excuse.
    feature = _Feature(max_data_classification="RESTRICTED")
    assert _capability_conflicts(feature, _model(family="openai", capabilities={}))


def test_local_only_set_matches_the_routers_privacy_floor() -> None:
    from models.model_router import _LOCAL_ONLY_CLASSIFICATIONS as router_set

    # If these drift, the UI disables a different set of models than the
    # router actually pins on-premise.
    assert _LOCAL_ONLY_CLASSIFICATIONS == router_set


# ── multiple failures are all reported ───────────────────────────────────────

def test_all_conflicts_are_reported_not_just_the_first() -> None:
    feature = _Feature(
        requires_vision=True, requires_tools=True, min_context_tokens=200000,
        max_data_classification="CONFIDENTIAL",
    )
    problems = _capability_conflicts(feature, _model(
        family="openai",
        capabilities={"modality": "text", "supports_tools": False, "context_window": 8000},
    ))
    assert len(problems) == 4, problems


# ── request-body validation ──────────────────────────────────────────────────

def test_capability_override_rejects_a_vendor_tier_name() -> None:
    # The whole point of the capability vocabulary is that this field names no
    # vendor, so "haiku"/"sonnet"/"opus" must not be accepted here even though
    # _HINT_MAP would resolve them.
    for bad in ("haiku", "sonnet", "opus", "complex", "medium", "gpt", "nonsense"):
        with pytest.raises(Exception):
            AssignmentUpsert(capability_override=bad)


def test_capability_override_accepts_the_vocabulary() -> None:
    from core.feature_registry import CAPABILITIES

    for good in CAPABILITIES:
        assert AssignmentUpsert(capability_override=good).capability_override == good


def test_capability_override_normalises_case_and_blanks() -> None:
    assert AssignmentUpsert(capability_override="  BALANCED ").capability_override == "balanced"
    assert AssignmentUpsert(capability_override="").capability_override is None
    assert AssignmentUpsert(capability_override=None).capability_override is None


def test_org_id_defaults_to_default() -> None:
    assert AssignmentUpsert().org_id == "default"
    assert AssignmentUpsert(org_id="   ").org_id == "default"
    assert AssignmentUpsert(org_id=" acme ").org_id == "acme"
