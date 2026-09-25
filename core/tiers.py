# SPDX-License-Identifier: MIT
# ============================================================
# APPLICATION TIER VOCABULARY  —  the single source of truth
# ============================================================
#
# Eight capability tiers. A tier names WHAT A TASK NEEDS, never who supplies
# it: no tier here refers to a provider, a vendor, a model SKU, or a deployment
# topology. That is the whole point — the platform is open source and must run
# unchanged on a cloud-only, self-hosted-only, single-provider, or mixed
# deployment. An application module asks for `complex`; the administrator
# decides which configured model answers.
#
# This module is a LEAF: standard library only. It must never import from
# `models/`, `db/`, or anything that reaches the model router, so that
# `models/model_router.py` can import it at module scope without a cycle.
#
# ── Migration status ────────────────────────────────────────────────────────
# Introduced in Phase 1 of the LLM tier governance migration (see plan.html
# Rev 3 §D, §L). Phase 1 is ADDITIVE ONLY: this vocabulary exists, is tested,
# and is deliberately unused by production code. Call sites move over in
# Phase 6. Do not "helpfully" start passing Tier values from application code
# before then — the resolver that gives them their real meaning does not exist
# until Phase 3.
#
# ⚠ THE `simple` COLLISION — read before touching anything here.
#   The LEGACY string hint "simple" means LOCAL (it dispatches to the in-house
#   gateway). The NEW Tier.SIMPLE means "cheap, short-output", which on a
#   cloud-only deployment is a cloud model. They are NOT the same thing and
#   during Phases 1-5 they deliberately resolve differently.
#   Getting this wrong silently egresses ~18 currently-local call sites, which
#   is a data-residency incident rather than a cost regression. The two
#   vocabularies are kept in separate namespaces on purpose: the router accepts
#   the new one only via a keyword-only, enum-typed `tier=` parameter, so a
#   bare string can never reach it. See models/model_router.py::_coerce_tier.
# ============================================================

from __future__ import annotations

from enum import Enum
from typing import Final, Literal, Union


class Tier(str, Enum):
    """The eight application-facing capability tiers.

    Fixed set. An administrator may choose which models serve a tier, but
    cannot create, rename, or delete one — that is enforced in the schema
    (Phase 3) and here by the enum being closed.
    """

    #: Cheapest capable model. Very high call volume, minimal reasoning,
    #: short bounded output. Latency and unit cost dominate.
    MINI = "mini"

    #: Short, bounded generation or structured extraction that still needs
    #: reliable instruction-following — a title, a 3-sentence summary, a JSON
    #: verdict. Distinguished from MINI by required correctness, not length.
    SIMPLE = "simple"

    #: General-purpose reasoning, coding, multi-paragraph generation.
    MEDIUM = "medium"

    #: Deep reasoning, long-context synthesis, agentic code generation and
    #: review. The most capable tier the application can request.
    COMPLEX = "complex"

    #: Multimodal understanding: image in, text out.
    IMAGE_INPUT = "image-input"

    #: Image generation: text in, image out.
    IMAGE_OUTPUT = "image-output"

    #: Video generation: text in, video out. Billed per output second.
    VIDEO_GENERATION = "video-generation"

    #: Fast, high-frequency structured classification whose output decides
    #: OTHER routing. Kept separate from MINI/SIMPLE because it sits on the hot
    #: path of every Auto turn and a misconfiguration here mis-routes
    #: everything downstream rather than degrading one answer.
    INTENT_CLASSIFICATION = "intent-classification"


#: Every tier, in the canonical order used by the admin UI and the docs.
ALL_TIERS: Final[tuple[Tier, ...]] = (
    Tier.MINI,
    Tier.SIMPLE,
    Tier.MEDIUM,
    Tier.COMPLEX,
    Tier.IMAGE_INPUT,
    Tier.IMAGE_OUTPUT,
    Tier.VIDEO_GENERATION,
    Tier.INTENT_CLASSIFICATION,
)


# ── Degradation ladder ───────────────────────────────────────────────────────
# Where the resolver may fall back to when a tier has no eligible model left
# (every candidate disabled, filtered out by a constraint, or its circuit
# breaker open). Walked AT MOST ONCE, and never across a no-cloud-egress
# constraint — see plan.html §M.5.
#
# The three modality tiers map to None deliberately: nothing substitutes for
# image or video generation. A text model cannot produce a video, so silently
# degrading would turn "feature unavailable" into a confusing wrong answer.
TIER_FALLBACK_LADDER: Final[dict[Tier, Union[Tier, None]]] = {
    Tier.COMPLEX: Tier.MEDIUM,
    Tier.MEDIUM: Tier.SIMPLE,
    Tier.SIMPLE: Tier.MINI,
    Tier.MINI: None,
    Tier.INTENT_CLASSIFICATION: Tier.SIMPLE,
    Tier.IMAGE_INPUT: None,
    Tier.IMAGE_OUTPUT: None,
    Tier.VIDEO_GENERATION: None,
}


# ── Modality eligibility ─────────────────────────────────────────────────────
# What a model's `capabilities.modality` list must contain for it to be
# assignable to a tier. Consumed by the Phase 3 resolver and by the admin
# tier-assignment screen to filter its model dropdown.
MODALITY_TEXT: Final = "text"
MODALITY_IMAGE_IN: Final = "image-in"
MODALITY_IMAGE_OUT: Final = "image-out"
MODALITY_VIDEO_OUT: Final = "video-out"

MODALITY_REQUIREMENT: Final[dict[Tier, str]] = {
    Tier.MINI: MODALITY_TEXT,
    Tier.SIMPLE: MODALITY_TEXT,
    Tier.MEDIUM: MODALITY_TEXT,
    Tier.COMPLEX: MODALITY_TEXT,
    Tier.INTENT_CLASSIFICATION: MODALITY_TEXT,
    Tier.IMAGE_INPUT: MODALITY_IMAGE_IN,
    Tier.IMAGE_OUTPUT: MODALITY_IMAGE_OUT,
    Tier.VIDEO_GENERATION: MODALITY_VIDEO_OUT,
}


# ── Legacy inbound aliases (BOUNDARY ONLY — bounded lifetime) ────────────────
# CLI and IDE clients send provider/SKU-shaped hints today. Confirmed in the
# Phase 0 audit:
#   routers/messages_compat_router.py::_normalise_model  (CLI)
#   routers/ide_router.py::_hint_map                     (IDE)
#
# This table translates those values into either an approved tier or the
# EXPLICIT_MODEL sentinel, which means "the caller named a specific model —
# resolve it against the registry, do not treat it as a capability request".
#
# THREE RULES, all load-bearing:
#   1. Used ONLY at an inbound client boundary. Application code must never
#      consult it — application code asks for a Tier directly.
#   2. NEVER exposed by a governance API. The admin surface knows about eight
#      tiers and nothing else.
#   3. Bounded lifetime. Every translation increments
#      `ainxt_legacy_model_alias_total`; removal in Phase 10 is gated on that
#      counter reading zero for a full release.
EXPLICIT_MODEL: Final[Literal["EXPLICIT_MODEL"]] = "EXPLICIT_MODEL"

LEGACY_INBOUND_ALIASES: Final[dict[str, Union[Tier, str]]] = {
    # ── Topology-named legacy tiers ──────────────────────────────────────────
    # "simple" and "local" dispatch to the in-house gateway today. They are a
    # request for a LOCAL model, which is a deployment property and no longer a
    # tier; the capability actually being asked for at these call sites is
    # short-output generation. See plan.html §D.2 for the per-task derivation.
    "simple": Tier.SIMPLE,
    "local": Tier.SIMPLE,
    "local_mini": Tier.INTENT_CLASSIFICATION,
    "gpt-oss": Tier.INTENT_CLASSIFICATION,
    "gpt-oss-120b": Tier.INTENT_CLASSIFICATION,
    # ── Cheapest hosted ──────────────────────────────────────────────────────
    "mini": Tier.MINI,
    "gpt-mini": Tier.MINI,
    "gpt-5-mini": Tier.MINI,
    # ── Cheap cloud / short output (vendor SKU name for a capability) ────────
    "haiku": Tier.SIMPLE,
    # ── General coding / reasoning ───────────────────────────────────────────
    "medium": Tier.MEDIUM,
    "coding": Tier.MEDIUM,
    "agents": Tier.MEDIUM,
    "gpt": Tier.MEDIUM,
    "gpt-5.4": Tier.MEDIUM,
    # ── Deep reasoning ───────────────────────────────────────────────────────
    "complex": Tier.COMPLEX,
    "sonnet": Tier.COMPLEX,
    "claude": Tier.COMPLEX,
    # `solution` and `deep` collapse into COMPLEX. The behaviours they carried
    # become routing constraints rather than tiers: "a stronger model for
    # review" is role='review' within COMPLEX, and "a bigger context window" is
    # a min_context_window filter. See plan.html §M.2, §M.3.
    "solution": Tier.COMPLEX,
    "opus": Tier.COMPLEX,
    "deep": Tier.COMPLEX,
    "gpt-5-5": Tier.COMPLEX,
    # ── Image analysis ───────────────────────────────────────────────────────
    "vision": Tier.IMAGE_INPUT,
    # ── Specific SKUs: a user naming a model, not requesting a capability ────
    "opus-4-8": EXPLICIT_MODEL,
    "claude-opus-4-8": EXPLICIT_MODEL,
    "opus-5": EXPLICIT_MODEL,
    "claude-opus-5": EXPLICIT_MODEL,
    "sonnet-5": EXPLICIT_MODEL,
    "claude-sonnet-5": EXPLICIT_MODEL,
    "tera": EXPLICIT_MODEL,
    "gpt-5.6-terra": EXPLICIT_MODEL,
    "luna": EXPLICIT_MODEL,
    "gpt-5.6-luna": EXPLICIT_MODEL,
    "gemini": EXPLICIT_MODEL,
    "gemini-2.5-flash": EXPLICIT_MODEL,
    "gemini-2.0-flash": EXPLICIT_MODEL,
    "gemini-3.5-flash": EXPLICIT_MODEL,
    "gemini-3.1-flash-lite": EXPLICIT_MODEL,
    "gemini-3.1-flash-image": EXPLICIT_MODEL,
    # ── IDE-only aliases (routers/ide_router.py::_hint_map) ──────────────────
    "auto": Tier.SIMPLE,
    # ── CLI-only aliases (routers/messages_compat_router.py) ─────────────────
    "latest": Tier.COMPLEX,
    "opus-4-7": EXPLICIT_MODEL,
    "claude-opus-4-7": EXPLICIT_MODEL,
    "gemini-coding": EXPLICIT_MODEL,
    "gemini-flash": EXPLICIT_MODEL,
    "gemini-lite": EXPLICIT_MODEL,
    "gemini-coding-lite": EXPLICIT_MODEL,
    "gemini-image": EXPLICIT_MODEL,
}


def resolve_legacy_alias(value: str) -> Union[Tier, str, None]:
    """Translate one inbound client hint into a Tier or the EXPLICIT_MODEL sentinel.

    Returns ``None`` when the value is not a known legacy alias — the caller
    should then treat it as a concrete model id and resolve it against the
    registry.

    ``local:<id>`` always means "run this exact in-house model", so it maps to
    EXPLICIT_MODEL regardless of the suffix.

    BOUNDARY USE ONLY. Application code asks for a Tier directly; it must never
    round-trip through this table.
    """
    if not value:
        return None
    key = value.strip().lower()
    if key.startswith("local:"):
        return EXPLICIT_MODEL
    return LEGACY_INBOUND_ALIASES.get(key)


# Aliases already reported once this process, so the human-readable warning is
# emitted per distinct alias rather than per request. Mirrors the idiom in
# services/llm_spend/fetchers/anthropic_admin.py:81. The Prometheus counter
# below is incremented on EVERY translation — only the log line is throttled.
_warned_legacy_aliases: set = set()


def note_legacy_alias(value: str, surface: str) -> None:
    """Record that a client sent a legacy model alias. Never raises.

    Call this at an inbound client boundary (CLI / IDE) whenever a legacy
    alias is about to be translated. It does not change routing — it exists so
    the compatibility shim has a removal gate: Phase 10 may delete the shim
    only once ``ainxt_legacy_model_alias_total`` has read zero for a full
    release.

    ``surface`` is the client the value arrived from, e.g. "cli" or "ide".
    """
    try:
        key = (value or "").strip().lower()
        if not key:
            return
        from core.telemetry import telemetry_metrics

        telemetry_metrics.record_legacy_alias(alias=key, surface=surface)

        if key not in _warned_legacy_aliases:
            _warned_legacy_aliases.add(key)
            from core.logger import logger

            logger.warning(
                "[tiers] legacy model alias %r received from %s. Provider/SKU-shaped "
                "hints are deprecated in favour of the eight approved capability "
                "tiers (core.tiers.Tier); this compatibility translation is "
                "scheduled for removal once no client relies on it. "
                "Tracked as %s_legacy_model_alias_total.",
                key, surface, "ainxt",
            )
    except Exception:   # noqa: BLE001 — observability must never break a request
        pass


def is_tier(value: object) -> bool:
    """True when ``value`` is one of the eight tiers.

    Accepts a ``Tier`` or its string value. Deliberately does NOT accept
    near-misses like ``"local"`` or ``"deep"`` — those are legacy aliases and
    must go through :func:`resolve_legacy_alias` at a boundary instead.
    """
    if isinstance(value, Tier):
        return True
    if isinstance(value, str):
        return value in {t.value for t in Tier}
    return False
