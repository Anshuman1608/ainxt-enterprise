# SPDX-License-Identifier: MIT
"""Phase 1 of the LLM tier governance migration — vocabulary + zero-drift guard.

Two things are under test:

1. ``core.tiers`` is a closed, total, eight-member vocabulary.
2. Introducing it changed NOTHING. Every legacy ``model_hint`` string still
   routes exactly where ``_HINT_MAP`` says it should, and the new ``tier=``
   parameter cannot be reached by a legacy string.

The second point is the whole contract of Phase 1, and
``test_simple_collision_guarded`` is its sharpest edge: the legacy hint
``"simple"`` means LOCAL, while ``Tier.SIMPLE`` means "cheap, short output".
If those two ever converge before the per-call-site migration in Phase 6,
~18 call sites that run on the in-house GPU today would silently start
egressing to a cloud provider.

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
    _HINT_MAP,
    _TIER_TO_LEGACY_HINT,
    TIER_HAIKU,
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

# _HINT_MAP is keyed by both string literals and env-var constants. The
# constants default to "" and are stripped by the falsy-key guard in
# model_router, so only the literals are stable enough to assert on.
_LITERAL_HINTS = sorted(k for k in _HINT_MAP if k and not k.isupper())


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
        (_TIER_TO_LEGACY_HINT, "_TIER_TO_LEGACY_HINT"),
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


@pytest.mark.parametrize("hint", _LITERAL_HINTS)
def test_legacy_alias_map_covers_every_hint_map_key(hint: str) -> None:
    """Every hint the router accepts must be translatable at a boundary."""
    resolved = resolve_legacy_alias(hint)
    assert resolved is not None, f"{hint!r} has no LEGACY_INBOUND_ALIASES entry"
    assert isinstance(resolved, Tier) or resolved == EXPLICIT_MODEL


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


# ── 3. ZERO BEHAVIOUR CHANGE — the Phase 1 contract ──────────────────────────


@pytest.mark.parametrize("hint", _LITERAL_HINTS)
def test_legacy_hints_unchanged(hint: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every legacy hint still resolves to the tier ``_HINT_MAP`` declares.

    ``_HINT_MAP`` is the untouched source of truth for legacy routing, so
    asserting ``route()`` reproduces it proves the ``tier=`` parameter did not
    perturb the existing path. The registry lookup in ``route()`` step 1a is
    stubbed out because it would otherwise hit the DB and, for hints that
    collide with a real model id, legitimately win over ``_HINT_MAP``.
    """
    router = ModelRouter()
    monkeypatch.setattr(
        "core.llm_provider_registry.get_model", lambda _mid: None, raising=False
    )
    decision = router.route("a short neutral prompt", model_hint=hint)
    assert decision.tier == _HINT_MAP[hint], (
        f"hint {hint!r} routed to {decision.tier!r}, expected {_HINT_MAP[hint]!r}"
    )


def test_no_hint_still_auto_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing neither argument must behave exactly as before (auto-routing)."""
    router = ModelRouter()
    monkeypatch.setattr(
        "core.llm_provider_registry.get_model", lambda _mid: None, raising=False
    )
    decision = router.route("what is 2 + 2?")
    assert decision.tier, "auto-routing must still produce a tier"
    assert decision.hint is None


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


@pytest.mark.parametrize("tier", list(Tier))
def test_tier_kwarg_reaches_expected_legacy_tier(
    tier: Tier, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each Tier lands on the legacy tier ``_TIER_TO_LEGACY_HINT`` promises."""
    router = ModelRouter()
    monkeypatch.setattr(
        "core.llm_provider_registry.get_model", lambda _mid: None, raising=False
    )
    expected = _HINT_MAP[_TIER_TO_LEGACY_HINT[tier]]
    assert router.route("a short neutral prompt", tier=tier).tier == expected


# ── 5. THE R1 REGRESSION TEST ────────────────────────────────────────────────


def test_simple_collision_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """``"simple"`` and ``Tier.SIMPLE`` must NOT mean the same thing yet.

    The legacy string dispatches to the in-house gateway (TIER_SIMPLE); the new
    tier means cheap-and-short (TIER_HAIKU). They converge only when each call
    site is migrated deliberately in Phase 6. If this test ever fails because
    the two now agree, ~18 currently-local call sites have been repointed at a
    cloud provider — a data-residency incident, not a cost regression.
    """
    router = ModelRouter()
    monkeypatch.setattr(
        "core.llm_provider_registry.get_model", lambda _mid: None, raising=False
    )

    legacy = router.route("summarise this", model_hint="simple").tier
    new = router.route("summarise this", tier=Tier.SIMPLE).tier

    assert legacy == TIER_SIMPLE, "legacy 'simple' must still route to the local tier"
    assert new == TIER_HAIKU, "Tier.SIMPLE must route to the cheap cloud tier"
    assert legacy != new, "the simple collision guard has been breached"


def test_local_hint_still_pins_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """``"local"`` is an explicit in-house choice and must never leave the box."""
    router = ModelRouter()
    monkeypatch.setattr(
        "core.llm_provider_registry.get_model", lambda _mid: None, raising=False
    )
    assert router.route("anything", model_hint="local").tier == TIER_SIMPLE


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


def test_note_legacy_alias_never_raises() -> None:
    """It sits on a request path; a telemetry fault must not break the request."""
    from core.tiers import note_legacy_alias

    note_legacy_alias("", surface="cli")
    note_legacy_alias("   ", surface="ide")
    note_legacy_alias(None, surface="cli")  # type: ignore[arg-type]


def test_no_production_call_site_passes_tier() -> None:
    """``tier=`` must remain unused outside tests until Phase 6.

    The resolver that gives a tier its real meaning does not exist until
    Phase 3; a call site adopting the vocabulary early would silently get the
    Phase-1 stub mapping instead.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    skip = {"tests", "venv", ".git", "node_modules", "AgentStudio"}
    pattern = re.compile(r"\btier\s*=\s*Tier\.")

    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if not skip & set(path.relative_to(root).parts)
        and pattern.search(path.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert not offenders, f"tier= used in production code before Phase 6: {offenders}"
