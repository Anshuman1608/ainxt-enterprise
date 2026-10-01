# SPDX-License-Identifier: MIT
"""The tier vocabulary and the router's tier= parameter.

``core.tiers`` is a closed, total, eight-member vocabulary, and a legacy
string can never reach the ``tier=`` parameter. Phase 1's zero-drift guards
against ``_HINT_MAP`` went with that table in Phase 8 (Rev 22 stage 8.3).

House patterns followed here (see tests/models/test_model_router_async_stream.py
and tests/router_policy/test_privacy_floor_live.py):
  * ``ModelRouter()`` is constructed inline — ``__init__`` opens no connections,
    so no DB, Redis, or API keys are needed.
  * Providers are stubbed by monkeypatching the BOUND METHOD on the instance;
    returning ``None`` is the real "provider unavailable" contract.
  * Fake gateways are duck-typed plain classes, not ``unittest.mock``.
"""

from __future__ import annotations

import pytest

from core.tiers import (
    ALL_TIERS,
    EXPLICIT_MODEL,
    LEGACY_INBOUND_ALIASES,
    MODALITY_REQUIREMENT,
    TIER_FALLBACK_LADDER,
    Tier,
    is_tier,
    resolve_legacy_alias,
)
from models.model_router import (
    TIER_SIMPLE,
    ModelRouter,
)


# The eight approved tiers, spelled out rather than derived from the enum, so
# that a careless edit to core.tiers fails here instead of silently redefining
# the platform's vocabulary.
_EXPECTED_TIER_VALUES = {
    "mini",
    "simple",
    "medium",
    "complex",
    "image-input",
    "image-output",
    "video-generation",
    "intent-classification",
}

# Names that must NOT become tiers: vendor SKUs, deployment topology, and the
# two capability-shaped concepts that are routing constraints instead
# (plan.html §M.2 context size, §M.3 review role).
_MUST_NOT_BE_TIERS = [
    "local",
    "local-mini",
    "local_mini",
    "deep",
    "solution",
    "haiku",
    "opus",
    "sonnet",
    "gpt",
    "gemini",
    "vision",
    "tera",
    "luna",
]

# ── 1. The vocabulary is closed and total ────────────────────────────────────


def test_tier_enum_is_exactly_eight() -> None:
    assert len(Tier) == 8
    assert {t.value for t in Tier} == _EXPECTED_TIER_VALUES
    assert set(ALL_TIERS) == set(Tier), "ALL_TIERS must list every member"
    assert len(ALL_TIERS) == len(Tier), "ALL_TIERS must not contain duplicates"


@pytest.mark.parametrize("name", _MUST_NOT_BE_TIERS)
def test_tier_enum_rejects_non_tiers(name: str) -> None:
    """Vendor SKUs and topology names must not be constructible as tiers."""
    with pytest.raises(ValueError):
        Tier(name)
    assert not is_tier(name)


@pytest.mark.parametrize(
    "mapping, label",
    [
        (TIER_FALLBACK_LADDER, "TIER_FALLBACK_LADDER"),
        (MODALITY_REQUIREMENT, "MODALITY_REQUIREMENT"),
    ],
)
def test_tier_keyed_mappings_are_total(mapping: dict, label: str) -> None:
    """Every tier-keyed table must cover all eight — no silent KeyError later."""
    assert set(mapping) == set(Tier), f"{label} is not total over Tier"


def test_modality_tiers_have_no_fallback() -> None:
    """Nothing substitutes for image or video generation.

    Degrading an image request to a text model would turn "feature
    unavailable" into a confusing wrong answer (plan.html §M.5).
    """
    for tier in (Tier.IMAGE_INPUT, Tier.IMAGE_OUTPUT, Tier.VIDEO_GENERATION):
        assert TIER_FALLBACK_LADDER[tier] is None


def test_fallback_ladder_terminates() -> None:
    """Walking the ladder from any tier must reach None, never loop."""
    for start in Tier:
        seen, cur = set(), start
        while cur is not None:
            assert cur not in seen, f"cycle in TIER_FALLBACK_LADDER from {start}"
            seen.add(cur)
            cur = TIER_FALLBACK_LADDER[cur]


# ── 2. The legacy alias table is total at both client boundaries ─────────────


def test_legacy_alias_values_are_tier_or_sentinel() -> None:
    for key, value in LEGACY_INBOUND_ALIASES.items():
        assert isinstance(value, Tier) or value == EXPLICIT_MODEL, (
            f"{key!r} maps to {value!r}, which is neither a Tier nor EXPLICIT_MODEL"
        )


def test_local_prefixed_alias_is_explicit_model() -> None:
    """``local:<id>`` names one exact in-house model, whatever the suffix."""
    assert resolve_legacy_alias("local:kimi-k2.7") == EXPLICIT_MODEL
    assert resolve_legacy_alias("local:anything-at-all") == EXPLICIT_MODEL


def test_unknown_value_is_not_an_alias() -> None:
    """A concrete model id must fall through so the caller hits the registry."""
    assert resolve_legacy_alias("some-vendor/some-model-v3") is None
    assert resolve_legacy_alias("") is None


# ── 3. (retired in Phase 8) ──────────────────────────────────────────────────
# The Phase 1 "zero behaviour change with the flag off" tests went with
# TIER_GOVERNANCE_ENABLED: a capability hint now always resolves through the
# assignments, so there is no flag-off routing left to hold constant.


# ── 4. The new parameter is unreachable by accident ──────────────────────────


@pytest.mark.parametrize(
    "method",
    ["route", "generate", "stream", "async_generate", "async_stream",
     "generate_structured"],
)
def test_tier_kwarg_is_keyword_only(method: str) -> None:
    """A positional argument must never be able to land in ``tier``.

    This is one of the three properties containing the ``simple`` collision —
    see the module docstring of core/tiers.py.
    """
    import inspect

    param = inspect.signature(getattr(ModelRouter, method)).parameters.get("tier")
    assert param is not None, f"{method}() is missing the tier= parameter"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        f"{method}() tier= must be keyword-only, got {param.kind}"
    )
    assert param.default is None


def test_tier_and_model_hint_mutually_exclusive() -> None:
    router = ModelRouter()
    with pytest.raises(ValueError, match="not both"):
        router.route("hi", model_hint="complex", tier=Tier.COMPLEX)


@pytest.mark.parametrize("bogus", ["local", "deep", "solution", "haiku"])
def test_tier_kwarg_rejects_legacy_strings(bogus: str) -> None:
    """A legacy alias passed as ``tier=`` must be refused, not silently coerced."""
    router = ModelRouter()
    with pytest.raises(ValueError):
        router.route("hi", tier=bogus)


# ── 6. Phase 1 is additive: nothing in production uses the new vocabulary ────


def test_legacy_alias_counter_increments_and_is_scrapeable() -> None:
    """The shim's removal gate must actually record something.

    Phase 10 may delete the compatibility shim only once this counter reads
    zero for a full release — so a counter that silently never increments
    (or lands in a registry nothing scrapes) would make the gate meaningless.
    """
    from core.telemetry import telemetry_metrics
    from core.tiers import note_legacy_alias

    note_legacy_alias("opus", surface="cli")
    scraped = telemetry_metrics.to_prometheus()

    assert "ainxt_legacy_model_alias_total" in scraped, (
        "counter is not exposed on the Prometheus endpoint the platform scrapes"
    )
    assert 'alias="opus"' in scraped and 'surface="cli"' in scraped


def test_the_deprecation_warning_points_somewhere_actionable(caplog) -> None:
    """Phase 9 published the removal notice; the warning must lead to it.

    The text used to end "scheduled for removal once no client relies on it",
    which tells an operator neither when nor how to check. It now names
    CHANGELOG.md, where the removal release and the gate query live — and the
    gate is the reason this matters: it is a RANGE reading of a counter that
    resets on restart, so an operator who reads the counter once concludes
    the opposite of the truth.

    Asserted rather than trusted because it is a log string, which is exactly
    the kind of thing a later edit rewrites without noticing what depended on
    it.
    """
    import logging

    from core.tiers import _warned_legacy_aliases, note_legacy_alias

    alias = "opus-4-8"
    _warned_legacy_aliases.discard(alias)       # the warning is once-per-process
    with caplog.at_level(logging.WARNING):
        note_legacy_alias(alias, surface="ide")

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "CHANGELOG.md" in text, (
        "the deprecation warning no longer names the document that carries "
        "the removal release and the gate query"
    )
    assert "DEPRECATED" in text or "deprecated" in text
    assert "ainxt_legacy_model_alias_total" in text, (
        "the warning must name the counter, or the gate is unfindable from "
        "the one place an operator actually sees the problem"
    )


def test_the_published_notice_exists_and_explains_the_gate() -> None:
    """The warning points at CHANGELOG.md; this is the other end of that.

    A pointer to a section that does not exist is worse than no pointer. The
    two caveats below are the ones that make the gate readable at all — an
    unauthenticated scrape records 401s rather than zeroes, and a point
    reading of an in-process counter measures uptime.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    changelog = root / "CHANGELOG.md"
    assert changelog.exists(), "CHANGELOG.md is missing — the warning is a dead link"

    text = changelog.read_text(encoding="utf-8")
    assert "### Deprecated" in text
    assert "LEGACY_INBOUND_ALIASES" in text
    assert "increase(ainxt_legacy_model_alias_total" in text, (
        "the gate must be written as a RANGE query; the counter resets on "
        "restart, so `== 0` on a point reading is always eventually true"
    )
    assert "admin" in text and "401" in text, (
        "the notice must say the scrape job needs an admin credential — "
        "/metrics/prometheus is admin-only, and an unauthenticated job "
        "records 401s rather than zeroes"
    )


def test_note_legacy_alias_never_raises() -> None:
    """It sits on a request path; a telemetry fault must not break the request."""
    from core.tiers import note_legacy_alias

    note_legacy_alias("", surface="cli")
    note_legacy_alias("   ", surface="ide")
    note_legacy_alias(None, surface="cli")  # type: ignore[arg-type]
