# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt MODEL REGISTRY  (STRICT ENTERPRISE CONFIG)
# ============================================================
#
# Routing table:
#   simple   → Local (in-house Local LLM proxy)   — private, free, low-latency
#   medium   → GPT-5.4                           — coding, reasoning, agents
#   complex  → Claude Sonnet 4.6                 — deep reasoning, SDLC
#   deep     → GPT-5-5                           — latest OpenAI, explicit selection only
#   solution → Claude Opus 4.7                   — final synthesis (CLI/IDE only)
#   opus-4-8 → Claude Opus 4.8                   — CLI/IDE opt-in
#   opus-5   → Claude Opus 5                     — CLI/IDE opt-in (ENABLE_CLI_OPUS_5)
#   vision   → Gemini 3.1 Flash Image            — image generation (auto-detected)
#   gemini   → Gemini 3.5 Flash                  — explicit Gemini text routing (coding)
#
# All model identifiers are env-var-backed so they can be updated
# at deploy time without code changes.  The default values shown
# below are the AiNxt-approved production models — do not change
# without a formal model approval workflow.
#
# BLOCKED: claude-opus-4-6 and older Claude, claude-sonnet-4-5, gpt-5.2-pro
# ============================================================

import os
import re

from core.logger import logger

# ============================================================
# PROVIDER POSTURE  —  the single switch an adopter needs
# ============================================================
#
#   LLM_PROVIDER=cloud   (default)  Use the cloud models named below. Requires
#                                   credentials for whichever of Anthropic /
#                                   OpenAI / Google you actually route to.
#   LLM_PROVIDER=local              Resolve EVERY routing tier to
#                                   LOCAL_LLM_MODEL_NAME, served by your own
#                                   OpenAI-compatible endpoint (Ollama, vLLM,
#                                   LiteLLM, ...). No cloud provider account is
#                                   needed and no prompt leaves your network.
#
# The default is `cloud` so existing deployments are unaffected. An adopter who
# does not use Anthropic (or any cloud provider) sets ONE variable rather than
# overriding a dozen model ids individually. See docs/PROVIDERS.md.
#
# An unrecognised value is a hard error rather than a silent fallback: quietly
# routing to a different provider than the operator asked for would send prompts
# somewhere they did not intend.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "cloud").strip().lower()
_VALID_LLM_PROVIDERS = ("cloud", "local")
if LLM_PROVIDER not in _VALID_LLM_PROVIDERS:
    raise ValueError(
        "LLM_PROVIDER=%r is not valid (expected one of: %s). Refusing to start "
        "rather than silently routing to a provider you did not choose."
        % (LLM_PROVIDER, ", ".join(_VALID_LLM_PROVIDERS))
    )


def is_local_only() -> bool:
    """True when the deployment must not call a cloud model provider.

    Read via this helper rather than comparing the constant, so a future posture
    (e.g. a specific single cloud provider) only changes one place.
    """
    return LLM_PROVIDER == "local"


# ---------------- OPENAI (APPROVED) ----------------

OPENAI_SIMPLE_MODEL  = os.getenv("OPENAI_SIMPLE_MODEL",  "")    # set via OPENAI_SIMPLE_MODEL in .env
OPENAI_CODING_MODEL  = os.getenv("OPENAI_CODING_MODEL",  "")    # set via OPENAI_CODING_MODEL in .env
OPENAI_PRIMARY_MODEL = OPENAI_CODING_MODEL   # alias

OPENAI_LATEST_MODEL       = os.getenv("OPENAI_LATEST_MODEL",       "")   # set via OPENAI_LATEST_MODEL in .env
OPENAI_TERA_MODEL         = os.getenv("OPENAI_TERA_MODEL",         "")   # set via OPENAI_TERA_MODEL in .env
OPENAI_LUNA_MODEL         = os.getenv("OPENAI_LUNA_MODEL",         "")   # set via OPENAI_LUNA_MODEL in .env
OPENAI_OSS_MODEL          = os.getenv("OPENAI_OSS_MODEL",          "")   # set via OPENAI_OSS_MODEL in .env — your self-hosted model ID
OPENAI_DEEP_RESEARCH_MINI = os.getenv("OPENAI_DEEP_RESEARCH_MINI", "")   # set via OPENAI_DEEP_RESEARCH_MINI in .env
OPENAI_DEEP_RESEARCH      = os.getenv("OPENAI_DEEP_RESEARCH",      "")   # set via OPENAI_DEEP_RESEARCH in .env
OPENAI_IMAGE_MODEL        = os.getenv("OPENAI_IMAGE_MODEL",        "")   # set via OPENAI_IMAGE_MODEL in .env


# ---------------- CLAUDE (APPROVED) ----------------

CLAUDE_PRIMARY_MODEL = os.getenv("CLAUDE_PRIMARY_MODEL", "")   # set via CLAUDE_PRIMARY_MODEL in .env
CLAUDE_HAIKU         = os.getenv("CLAUDE_HAIKU",         "")   # set via CLAUDE_HAIKU in .env

# Sonnet 5 — explicit user selection, available on ALL channels (web Chat picker,
# CLI `/model sonnet-5`, IDE `/v1/models`, OpenAI-compat clients). NOT gated by
# ENABLE_OPUS — it is a Sonnet-tier model, not an Opus-tier one. Falls back to
# CLAUDE_PRIMARY_MODEL when the upstream call fails.
CLAUDE_SONNET_5_MODEL = os.getenv("CLAUDE_SONNET_5_MODEL", "")   # set via CLAUDE_SONNET_5_MODEL in .env

# Opus — solution-tier model for final synthesis in Threads @AiNxt + SDLC.
# CLI/IDE only; ENABLE_CHAT_OPUS=false keeps Opus out of the web Chat picker.
CLAUDE_OPUS_MODEL    = os.getenv("CLAUDE_OPUS_MODEL",   "")   # set via CLAUDE_OPUS_MODEL in .env
# Opus 4.6 is retired — kept as a constant so existing env-var references
# resolve cleanly, but it is always in BLOCKED_MODELS (see below).
CLAUDE_OPUS_46_MODEL = os.getenv("CLAUDE_OPUS_46_MODEL", "")  # RETIRED — always blocked; set via env if needed
# Opus 4.8 — CLI/IDE opt-in. Available via CLI (`/model opus-4-8`) and IDE
# plugins (/v1/models). NOT shown in the web Chat picker (/v1/all-models) and
# NOT used by the SDLC pipeline (which stays on Opus 4.7 via the solution tier).
CLAUDE_OPUS_48_MODEL = os.getenv("CLAUDE_OPUS_48_MODEL", "")   # set via CLAUDE_OPUS_48_MODEL in .env
# Opus 5 — CLI/IDE opt-in. Gated by ENABLE_CLI_OPUS_5 (default false).
# NOT shown in the web Chat picker. NOT used by the SDLC pipeline.
CLAUDE_OPUS_5_MODEL  = os.getenv("CLAUDE_OPUS_5_MODEL",  "")   # set via CLAUDE_OPUS_5_MODEL in .env
ENABLE_OPUS          = os.getenv("ENABLE_OPUS", "true").lower() in ("true", "1", "yes")
# Sonnet 5 is enabled on all channels by default. Kept env-var-backed so ops can
# hard-disable it (set to "false") without a code change if a rollback is needed.
ENABLE_SONNET_5      = os.getenv("ENABLE_SONNET_5", "true").lower() in ("true", "1", "yes")
ENABLE_CHAT_OPUS     = os.getenv("ENABLE_CHAT_OPUS", "false").lower() in ("true", "1", "yes")
ENABLE_CLI_OPUS_48   = os.getenv("ENABLE_CLI_OPUS_48", "true").lower() in ("true", "1", "yes")
# Opus 5 is a new model — opt-in only. Set ENABLE_CLI_OPUS_5=true to expose it
# on CLI and IDE channels. Web Chat never shows it regardless of this flag.
ENABLE_CLI_OPUS_5    = os.getenv("ENABLE_CLI_OPUS_5", "false").lower() in ("true", "1", "yes")
# Set ENABLE_RAW_OPENAI_API=true to re-enable direct access.
ENABLE_RAW_OPENAI_API = os.getenv("ENABLE_RAW_OPENAI_API", "false").lower() in ("true", "1", "yes")
# GPT-5.6 Tera and Luna — enabled on both Chat and CLI by default.
# Set to "false" to hide a variant without a code change.
ENABLE_GPT56_TERA    = os.getenv("ENABLE_GPT56_TERA", "true").lower() in ("true", "1", "yes")
ENABLE_GPT56_LUNA    = os.getenv("ENABLE_GPT56_LUNA", "true").lower() in ("true", "1", "yes")
SOLUTION_MODEL       = CLAUDE_OPUS_MODEL if ENABLE_OPUS else CLAUDE_PRIMARY_MODEL


# ---------------- GEMINI ----------------
# Four explicit models (gemini-2.5-flash deprecated):
#   GEMINI_TEXT_MODEL        — text/coding (multimodal: also handles vision analysis)
#   GEMINI_CODING_LITE_MODEL — lightweight coding
#   GEMINI_IMAGE_MODEL       — image generation (text → image, e.g. gemini-3.1-flash-image)
#   GEMINI_VISION_MODEL      — vision analysis (image → text description/analysis)
#
# IMPORTANT: GEMINI_VISION_MODEL must be a text-output model (e.g. gemini-3.5-flash),
# NOT the image-generation model. gemini-3.1-flash-image is designed for text→image
# generation; when used for image analysis it returns empty text (response.text = "").
# GEMINI_IMAGE_MODEL is used exclusively for /chat/image-generate (image generation).
# GEMINI_VISION_MODEL is used for /ask/image vision analysis and parse_image() in
# document_parser.py — both need a model that returns text, not image bytes.

GEMINI_TEXT_MODEL        = os.getenv("GEMINI_TEXT_MODEL",        "")   # set via GEMINI_TEXT_MODEL in .env
GEMINI_CODING_LITE_MODEL = os.getenv("GEMINI_CODING_LITE_MODEL", "")   # set via GEMINI_CODING_LITE_MODEL in .env
GEMINI_IMAGE_MODEL       = os.getenv("GEMINI_IMAGE_MODEL",       "")   # set via GEMINI_IMAGE_MODEL in .env
# Default to GEMINI_TEXT_MODEL (gemini-3.5-flash) — a multimodal model that can
# analyse images and return text. Previously aliased to GEMINI_IMAGE_MODEL which
# caused empty responses when /ask/image was called with non-generation prompts
# (e.g. "improve the UI") because the image-generation model returns image bytes,
# not text, leaving response.text = "".
GEMINI_VISION_MODEL      = os.getenv("GEMINI_VISION_MODEL",      GEMINI_TEXT_MODEL)

# Veo 3.1 video preview — Gemini provider, long-running operation, returns MP4.
# Chat-UI-only. Per-user access is governed by model governance tables
# (dept_model_permissions / user_model_permissions) — same as every other model.
# NOT shown in CLI (/v1/models), IDE (/ide/models), or OpenAI-compat (/v1/models).
VEO_MODEL          = os.getenv("VEO_MODEL",   "")   # set via VEO_MODEL in .env
VEO_DISPLAY        = os.getenv("VEO_DISPLAY", "Veo 3.1 (video preview)")
VEO_ENABLED        = os.getenv("VEO_ENABLED", "false").lower() in ("true", "1", "yes")
# Per-second cost (Veo is billed by output video duration, not tokens).
# Placeholder — update with official Vertex AI Veo 3.1 preview pricing.
VEO_COST_PER_SECOND = float(os.getenv("VEO_COST_PER_SECOND", "0.40"))


# ---------------- VISION ROUTING ----------------
# Primary vision provider at runtime: gemini | openai | local_llm
# Change via env vars — no code changes needed.
PRIMARY_VISION_PROVIDER  = os.getenv("PRIMARY_VISION_PROVIDER",  "")   # set via PRIMARY_VISION_PROVIDER in .env
# Fallback if primary fails. Set to "none" to disable.
FALLBACK_VISION_PROVIDER = os.getenv("FALLBACK_VISION_PROVIDER", "")   # set via FALLBACK_VISION_PROVIDER in .env
# Comma-separated IDs of local hosted models that accept image/vision input.
# Set LOCAL_VISION_MODELS to the vision-capable model IDs served by your local
# LLM endpoint (e.g. LOCAL_VISION_MODELS=llava:13b,bakllava:7b for Ollama).
# Defaults to empty — vision falls back to the cloud provider (Gemini/OpenAI).
LOCAL_VISION_MODELS: list[str] = [
    m.strip()
    for m in os.getenv("LOCAL_VISION_MODELS", "").split(",")
    if m.strip()
]


# ---------------- LOCAL LLM (in-house GPU) ----------------

LOCAL_LLM_MODEL_NAME = os.getenv("LOCAL_LLM_MODEL_NAME", "local-llm")

# ---------------- EVERYDAY-CHAT FALLBACK CHAIN ----------------
# Ordered fallback for the default everyday-chat mini tier when the primary
# (GPT-5-mini) is unavailable / its circuit breaker is open. Comma-separated;
# each entry is either a hint understood by the router ("haiku") or a pinned
# local model ("local:<id>"). Admins retune via the CHAT_FALLBACK_CHAIN env var
# without a code change.
# Default: fall back to Claude Haiku only. Add local model IDs for your
# deployment, e.g. CHAT_FALLBACK_CHAIN=haiku,local:llama3.1:8b
CHAT_FALLBACK_CHAIN: list[str] = [
    m.strip() for m in os.getenv(
        "CHAT_FALLBACK_CHAIN", ""
    ).split(",") if m.strip()
]
# No code default — set CHAT_FALLBACK_CHAIN in .env.
# OSS: leave blank (no fallback assumed).
# Internal: set to your preferred chain, e.g. local:kimi-k2.7-code,local:glm-5.2,haiku


# ---------------- COST (one authority, read from the registry) ----------------
#
# Per-model prices are admin data on the registry row (capabilities
# cost_per_1m_input / cost_per_1m_output), set on Admin > LLM Providers. No
# vendor publishes prices through an API, so nothing here can know them.

# Charged for a paid model with no recorded price: over-bill, never bill nothing.
UNPRICED_RATES: tuple[float, float] = (2.00, 8.00)
_FREE_RATES: tuple[float, float] = (0.0, 0.0)


def _registry_row_for(model: str):
    """The enabled registry row for an id, or for the longest id inside a display label."""
    try:
        from core.llm_provider_registry import get_enabled_models, get_model
    except Exception:
        return None
    row = get_model(model)
    if row is not None:
        return row
    low = model.lower()
    hits = [m for m in get_enabled_models() if m["model_id"] and m["model_id"].lower() in low]
    return max(hits, key=lambda m: len(m["model_id"])) if hits else None


def price_of(model: str):
    """Recorded (input, output) per 1M, (0, 0) if local or free, None if a paid model has no price."""
    m = (model or "").strip()
    if not m:
        return None
    try:
        row = _registry_row_for(m)
    except Exception as exc:
        logger.warning(f"[model_registry] price lookup failed for {m!r}: {exc}")
        row = None
    if row is not None:
        caps = row.get("capabilities") or {}
        cin, cout = caps.get("cost_per_1m_input"), caps.get("cost_per_1m_output")
        if isinstance(cin, (int, float)) and isinstance(cout, (int, float)):
            return (float(cin), float(cout))
        if row.get("family") == "ollama" or caps.get("billing_tier") == "free":
            return _FREE_RATES
        return None
    low = m.lower()
    if low.startswith("local:") or "local" in low:
        return _FREE_RATES
    try:
        from gateway_local_llm import is_local_model
        if is_local_model(m):
            return _FREE_RATES
    except Exception:
        pass
    return None


def rates_for(model: str) -> tuple[float, float]:
    """(input_usd, output_usd) per 1M tokens; UNPRICED_RATES when no price is recorded."""
    return price_of(model) or UNPRICED_RATES


def rate_per_second_for(model: str):
    """Recorded per-second price for a video model, or None."""
    try:
        row = _registry_row_for((model or "").strip())
    except Exception:
        return None
    val = ((row or {}).get("capabilities") or {}).get("cost_per_second")
    return float(val) if isinstance(val, (int, float)) else None





# ---------------- VEO ACCESS GATE (ad_level 0 or admin) ----------------
#
# Access is granted to:
#   - Users with ad_level == 0 (most senior execs), OR
#   - Users with role == "admin"
# Both fields are read from the JWT-decoded `current_user` dict.
# Fail-closed: missing claims → no access.
def is_veo_allowed_for_user(current_user: dict | None) -> bool:
    """Return True when VEO is globally enabled AND the user is ad_level 0 or admin."""
    if not VEO_ENABLED:
        return False
    if not current_user:
        return False
    if current_user.get("role") == "admin":
        return True
    try:
        return int(current_user.get("ad_level", 6)) == 0
    except (TypeError, ValueError):
        return False


# ---------------- DISPLAY NAMES (for UI labels and logs) ----------------
#
# Human-readable names shown in dropdowns, logs, and audit trails.
# Override via env vars if your internal branding differs from model IDs.
# These are intentionally separate from model IDs so a model can be
# upgraded (e.g. claude-sonnet-4-6 → claude-sonnet-4-7) with a single
# env-var change and the display name updates automatically.

CLAUDE_PRIMARY_DISPLAY  = os.getenv("CLAUDE_PRIMARY_DISPLAY",  "Claude Sonnet")
CLAUDE_HAIKU_DISPLAY    = os.getenv("CLAUDE_HAIKU_DISPLAY",    "Claude Haiku")
CLAUDE_OPUS_DISPLAY     = os.getenv("CLAUDE_OPUS_DISPLAY",     "Claude Opus 4.7")
CLAUDE_OPUS_48_DISPLAY  = os.getenv("CLAUDE_OPUS_48_DISPLAY",  "Claude Opus 4.8")
CLAUDE_OPUS_5_DISPLAY   = os.getenv("CLAUDE_OPUS_5_DISPLAY",   "Claude Opus 5")
CLAUDE_SONNET_5_DISPLAY = os.getenv("CLAUDE_SONNET_5_DISPLAY", "Claude Sonnet 5")
OPENAI_CODING_DISPLAY   = os.getenv("OPENAI_CODING_DISPLAY",   "GPT-5.4 (Coding)")
OPENAI_SIMPLE_DISPLAY   = os.getenv("OPENAI_SIMPLE_DISPLAY",   "GPT-5-mini (Fast)")
OPENAI_LATEST_DISPLAY   = os.getenv("OPENAI_LATEST_DISPLAY",   "GPT-5-5 (Latest)")
OPENAI_TERA_DISPLAY     = os.getenv("OPENAI_TERA_DISPLAY",     "GPT-5.6 Terra")
OPENAI_LUNA_DISPLAY     = os.getenv("OPENAI_LUNA_DISPLAY",     "GPT-5.6 Luna")
OPENAI_OSS_DISPLAY      = os.getenv("OPENAI_OSS_DISPLAY",      "GPT-OSS 120B (In-house)")
GEMINI_DISPLAY          = os.getenv("GEMINI_DISPLAY",          "Gemini")
GEMINI_TEXT_DISPLAY        = os.getenv("GEMINI_TEXT_DISPLAY",        "Gemini 3.5 Flash (Coding)")
GEMINI_CODING_LITE_DISPLAY = os.getenv("GEMINI_CODING_LITE_DISPLAY", "Gemini 3.1 Flash-Lite (Coding)")
GEMINI_IMAGE_DISPLAY       = os.getenv("GEMINI_IMAGE_DISPLAY",       "Gemini 3.1 Flash Image")
LOCAL_LLM_DISPLAY       = os.getenv("LOCAL_LLM_DISPLAY",       "Local (In-house)")


# ---------------- BLOCKED MODELS ----------------

BLOCKED_MODELS: set[str] = {

    # Claude — retired/old models always blocked
    "claude-opus-4-6",   # retired — superseded by Opus 4.7/4.8
    "claude-opus-4-5",
    "claude-opus-4",
    "claude-opus-3",
    "claude-sonnet-4-5", # retired — superseded by Sonnet 4.6

    # OpenAI — blocked pro variant
    "gpt-5.2-pro",
    # gpt-5.2 is retired — replaced by gpt-5.4
    "gpt-5.2",

}
# Opus 4.7 blocked only when ENABLE_OPUS=false
if not ENABLE_OPUS:
    BLOCKED_MODELS.add(CLAUDE_OPUS_MODEL)
    BLOCKED_MODELS.add(CLAUDE_OPUS_48_MODEL)


# ---------------- MODELS THAT REJECT `temperature` ----------------
#
# Anthropic has progressively stopped accepting `temperature` on newer Claude
# generations — they 400 outright instead of clamping/ignoring it. Both
# direct-dispatch paths that call the Anthropic SDK need to agree on this:
# gateway_claude.py (the main platform gateway) and
# AgentStudio/backend/app/core/llm_handler.py's ClaudeDirectClient (Agent
# Studio). This used to be a bare env var (MODELS_WITHOUT_TEMPERATURE) with
# no built-in defaults at all — meaning a fresh install with the env var
# unset sent `temperature` to opus-5/sonnet-5/opus-4-7/opus-4-8 unconditionally
# and got a 400 on every single call to any of them, including whichever one
# ends up the admin-configured default. The env var still works, purely
# additive, for any future/self-hosted model this list doesn't yet cover.
_MODELS_WITHOUT_TEMPERATURE_DEFAULTS = (
    "claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7",
)


def models_without_temperature() -> tuple[str, ...]:
    """Prefix list of model ids that must NOT receive a `temperature` param."""
    extra = tuple(
        p.strip() for p in os.getenv("MODELS_WITHOUT_TEMPERATURE", "").split(",") if p.strip()
    )
    return _MODELS_WITHOUT_TEMPERATURE_DEFAULTS + extra


# ---------------- SDLC PER-STAGE CAPABILITY TIERS ----------------
#
# §N.1 step 10. Each SDLC stage names the CAPABILITY it needs; an administrator
# decides which model serves that capability on Admin > Model Governance >
# Tiers. What this table replaced — SDLC_STAGE_MODEL_DEFAULTS, a stage → router
# hint map read through sdlc_stage_hint() — chose from .env instead, so the
# admin screen had no effect on any SDLC stage.
#
# Each entry is (Tier, legacy_hint, constraints).
#
#   Tier          what the stage needs, per plan.html §D.2. NOT derived from
#                 which model happens to be assigned today: an assignment is
#                 data an administrator edits, and a mapping derived from it
#                 goes stale the first time they do.
#   legacy_hint   the hint this stage passed BEFORE migration (D15). Used
#                 verbatim whenever governance is off or the tier resolves to
#                 nothing, so turning the flag off is provably a no-op. This is
#                 why classify/locate/normalize carry "haiku" and not "simple":
#                 D15 reproduces where a call site WENT, not the tier's name.
#   constraints   §M constraints the stage — and only the stage — can know.
#
# WHY ONLY FIVE STAGES. The old table declared seventeen. Twelve of them had no
# caller anywhere in the tree (analyze, design, synthesis, diagnose,
# solution_review, cross_model_review, fixer, exploration, noncode, classify,
# pre_coding_build) or reached only unreachable code (plan), so the matching
# SDLC_MODEL_<STAGE> variables did nothing at all — an operator who set
# SDLC_MODEL_DESIGN=solution believed they had changed something and had not.
# SDLC_MIXED_MODEL_RUNBOOK.md had already started listing some of them as
# "no-op vars". They are gone rather than carried forward, because a table the
# migration exists to make honest should not keep advertising twelve knobs that
# are not connected to anything. The CLI phases (classify/plan/implement) are
# not absent from the pipeline — they resolve through cli_tier_model_id() and
# SDLC_CLI_<PHASE>_MODEL below, which is a different mechanism.
#
# ENABLE_OPUS is no longer consulted here. It downgraded solution → complex so
# callers "never need to special-case Opus availability"; both now resolve to
# the same tier and the reviewer is a ROLE within it (§M.3a), so an absent
# reviewer is answered by the resolver preferring the head of the tier instead.
# ENABLE_OPUS still gates BLOCKED_MODELS above, which is where it belongs.

def _sdlc_stage_tiers() -> dict:
    """stage → (Tier, legacy_hint, constraints). Built lazily; see SDLC_STAGE_TIERS.

    Both imports are function-local. core.tiers is a stdlib-only leaf and
    core.tier_resolver imports only it and core.logger, so neither can cycle
    back here — but this module is imported by models/model_router.py at module
    scope, and keeping the dependency inside the function means a future top
    level import in either of them cannot turn that into a cycle at gateway
    start (N10-h).
    """
    from core.tiers import Tier
    from core.tier_resolver import ROLE_REVIEW
    return {
        # Region picking: returns a JSON array of line numbers. Bounded
        # structured extraction that has to parse — §D.2 `simple`.
        "locate":            (Tier.SIMPLE,  "haiku",    {}),
        # JSON field extraction from ticket text.
        "normalize":         (Tier.SIMPLE,  "haiku",    {}),
        # Surgical edits against visible code — §D.2 `complex`.
        "coder":             (Tier.COMPLEX, "complex",  {}),
        # A review GATE. §M.3a: "a stronger model reviews" is a ROLE within the
        # tier, not a tier of its own — which is what `solution` was. A
        # preference, not a filter, so a single-model deployment still runs the
        # gate with author and reviewer coinciding.
        "code_review":       (Tier.COMPLEX, "solution", {"require_role": ROLE_REVIEW}),
        # The manifest cross-validator. It JUDGES the plan, so it needs the same
        # capability that wrote it (§D.2 `complex`, and legacy `deep` maps there
        # — see models/model_router.py::_LEGACY_TO_GOVERNED). The thing that
        # made it a GPT model was never the tier: it was "do not let the author
        # mark its own homework", which is §M.3b's distinct_from_family and is
        # supplied per-call by the caller that knows who the author was.
        "manifest_validate": (Tier.COMPLEX, "deep",     {}),
    }


# Public, resolved once. A module-level dict rather than a function call at each
# site so the ratchet and the tests have a single object to assert against.
SDLC_STAGE_TIERS: dict = _sdlc_stage_tiers()

# Opus 4.8 is CLI/IDE-only; blocked when either the global Opus switch is off
# OR the CLI-specific opt-in is off. This guarantees the chat-picker (which does
# not list Opus 4.8) and SDLC (which routes via `solution` → Opus 4.7) are never
# accidentally upgraded to 4.8.
if not ENABLE_OPUS or not ENABLE_CLI_OPUS_48:
    BLOCKED_MODELS.add(CLAUDE_OPUS_48_MODEL)

# Opus 5 is CLI/IDE-only and opt-in. Blocked unless ENABLE_CLI_OPUS_5=true.
if not ENABLE_CLI_OPUS_5:
    BLOCKED_MODELS.add(CLAUDE_OPUS_5_MODEL)

# Sonnet 5 kill-switch — no channel gating, only a global on/off.
if not ENABLE_SONNET_5:
    BLOCKED_MODELS.add(CLAUDE_SONNET_5_MODEL)

# Operator extension — block additional models without code changes.
# Format: BLOCKED_MODELS_EXTRA=model-a,model-b
# The base set above (retired models) is always enforced regardless of this var.
_BLOCKED_MODELS_EXTRA: set[str] = {
    m.strip() for m in os.getenv("BLOCKED_MODELS_EXTRA", "").split(",") if m.strip()
}
if _BLOCKED_MODELS_EXTRA:
    BLOCKED_MODELS.update(_BLOCKED_MODELS_EXTRA)


# A vendor's dated snapshot of a model is that model: providers list
# "claude-sonnet-4-5-20250929" where this set says "claude-sonnet-4-5", so an
# exact-match deny-list silently stops applying the moment an admin runs
# "Sync models". Measured on a live deployment: two retired models were being
# advertised and served that way.
_DATED_SNAPSHOT = re.compile(r"-\d{8}$")


def is_blocked_model(model_id: str) -> bool:
    """The one place `BLOCKED_MODELS` is interpreted (D85).

    Every caller — the catalogue filter in core.llm_provider_registry and the
    per-request gates — goes through here, so "advertised" and "servable"
    cannot drift apart again.

    An empty id deliberately keeps today's answer rather than a better one:
    `""` IS in BLOCKED_MODELS on an admin-only install (the blank SKU
    constants get added under their flags), and gateway_claude.py:118 records
    what changing that costs.
    """
    mid = (model_id or "").strip()
    if mid in BLOCKED_MODELS:
        return True
    base = _DATED_SNAPSHOT.sub("", mid)
    return base != mid and base in BLOCKED_MODELS


def _role_model(env_value: str, family: str, tag: str) -> str:
    """Fall back to a registry-configured model when a role-specific env
    constant (CLAUDE_PRIMARY_MODEL, OPENAI_CODING_MODEL, etc.) is blank.

    Mirrors ``models/model_router.py``'s ``_resolve_tier_model()`` exactly —
    this module can't import that one (model_router.py imports
    core.model_registry at module level, so importing the reverse would be
    circular) — so the same env-override → registry-lookup-by-family/tag →
    "" chain is duplicated here as the single fallback every CLI/tier
    resolver below goes through. `tag` uses the same vocabulary as
    db/migrate.py's `_AC1_MODEL_ROLE_TAGS` backfill.

    No-op when env_value is already set — zero behavior change for any
    deployment that has its role env vars configured.
    """
    if env_value:
        return env_value
    try:
        from core.llm_provider_registry import get_enabled_models
        candidates = [m for m in get_enabled_models() if m["family"] == family]
        tagged = [m for m in candidates if tag in (m["capabilities"].get("tier_tags") or [])]
        pick = tagged[0] if tagged else (candidates[0] if candidates else None)
        return pick["model_id"] if pick else ""
    except Exception as exc:
        logger.warning(f"[model_registry] registry fallback failed family={family} tag={tag}: {exc}")
        return ""


def _tier_env_override(tier: str) -> str:
    """Operator override for a router tier's concrete model, provider-agnostic.

    ``SDLC_TIER_<TIER>_MODEL`` takes precedence over the family-specific constant
    (CLAUDE_*/OPENAI_*), letting a harness with NO Anthropic provider map every
    tier to its own models in ONE place instead of per-stage. Returns "" when
    unset, so the caller falls through to the historical constant → registry chain
    (see ``_role_model``). The value is only ever used as a model-id string passed
    to the router (registry lookup) or to the ``ainxt`` CLI ``--model`` argv
    element (spawned without a shell) — never interpolated into a shell command —
    and it is still gated by BLOCKED_MODELS downstream."""
    return (os.getenv(f"SDLC_TIER_{tier.upper()}_MODEL") or "").strip()


# ---------------- TIER → CONCRETE MODEL ID, FOR OUT-OF-PROCESS CONSUMERS ----
#
# §N.1 step 10. Everything above this line resolves a tier for an IN-PROCESS
# call, where the answer is routing kwargs and models/model_router.py does the
# resolving. The SDLC CLI phases cannot use that: they spawn the `ainxt` binary
# with `--model <id>`, so the model name leaves the process and the answer has
# to be a concrete id.
#
# What this replaces — cli_model_for_tier()'s `_tier_to_role` — never consulted
# the tier assignments at all. It mapped a hint to a .env constant through a
# table that hardcoded ("anthropic", …) for three tiers and ("openai", …) for
# two, which made the coder, plan and implement phases — the long agentic
# sessions that write the code — immune to the Admin > Model Governance screen.
#
# THE ADDRESSABILITY BAR — and what it is NOT.
#
# The first version of this checked a model id PREFIX (claude/gpt/gemini/...),
# on the theory that the spawned CLI calls back into this platform's own
# Anthropic-compatible endpoint, where _normalise_model rewrites an unknown
# prefix to CLAUDE_PRIMARY_MODEL. That theory was wrong twice over, and it
# suspended a live PLAN phase with "CLI exited with code 1":
#
#   1. The `ainxt` binary does not accept arbitrary model ids. It accepts the
#      ALIASES in its own config (bin/config.toml -> ~/.ainxt/config.toml),
#      and each alias's underlying `model` value. Anything else is refused
#      before a request is made: Couldn't set model '<id>': Invalid params:
#      "unknown model id".
#   2. On a default install that config points the CLI STRAIGHT AT the
#      provider with its own key, not at this platform. So the compat router's
#      rewriting never enters into it.
#
# A prefix is therefore necessary and nowhere near sufficient: every id the
# CLI rejected in that incident began with "claude". The bar has to be the
# CLI's own vocabulary, so it is read from the CLI's own config.
#
# THE CONSEQUENCE, STATED PLAINLY: on a deployment whose config.toml carries
# one alias, exactly one model is assignable to the SDLC CLI phases — the one
# it names. Assign anything else and this resolver says so and falls back.
# Governing those phases from the Tiers screen requires the operator to give
# the CLI a config that knows the models they intend to use.

def _cli_config_path():
    """Where the `ainxt` binary reads its model aliases from."""
    import pathlib as _pl
    home = (os.getenv("AINXT_HOME") or "").strip()
    return _pl.Path(home or (_pl.Path.home() / ".ainxt")) / "config.toml"


_CLI_MODELS_CACHE: dict = {}


def cli_acceptable_model_ids() -> frozenset:
    """Every value the `ainxt` CLI will accept for --model, or an empty set.

    Parsed from the CLI's own config so the two cannot disagree: the alias
    names under [model.<alias>], plus each alias's `model` value, which the
    binary also resolves. Cached on the file's mtime, so `sdlc-setup.sh`
    regenerating it takes effect without a restart.

    Returns an EMPTY set when the config is absent or unparseable — which is
    the normal case in the gateway process, because only the SDLC worker
    mounts it. An empty set means "unknown", and the caller then falls back to
    the prefix heuristic rather than rejecting everything. That is safe
    precisely because a process without the config is a process that never
    spawns the CLI.
    """
    path = _cli_config_path()
    try:
        stat = path.stat()
    except OSError:
        return frozenset()
    key = (str(path), stat.st_mtime_ns)
    hit = _CLI_MODELS_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:                              # noqa: BLE001
        logger.warning(f"[model_registry] could not parse the ainxt CLI config at "
                       f"{path} ({exc}) — falling back to the prefix heuristic")
        return frozenset()
    names = set()
    for alias, cfg in (data.get("model") or {}).items():
        names.add(alias)
        if isinstance(cfg, dict) and isinstance(cfg.get("model"), str):
            names.add(cfg["model"].strip())
    _CLI_MODELS_CACHE.clear()          # only ever one live config
    _CLI_MODELS_CACHE[key] = frozenset(n for n in names if n)
    return _CLI_MODELS_CACHE[key]


# Retained ONLY as the fallback for a process that cannot see the CLI config
# (see cli_acceptable_model_ids). Mirrors the pass-through set in
# routers/messages_compat_router.py::_normalise_model; a drift test pins them.
CLI_ADDRESSABLE_MODEL_PREFIXES = (
    "claude", "gpt", "o1", "o3", "o4", "gemini", "local", "ollama",
)


def cli_model_is_addressable(model_id: str) -> bool:
    """True when the `ainxt` CLI will accept this id for --model.

    Authoritative when the CLI's config is readable; a prefix guess otherwise.
    The guess is deliberately the weaker answer and never the only one relied
    on where it matters — the SDLC worker, which is the only process that
    spawns the CLI, is also the only one that mounts the config.
    """
    mid = (model_id or "").strip()
    if not mid:
        return False
    known = cli_acceptable_model_ids()
    if known:
        return mid in known
    return mid.lower().startswith(CLI_ADDRESSABLE_MODEL_PREFIXES)


def _legacy_cli_model_for_tier(hint: str) -> str:
    """The pre-governance answer: hint → .env constant → registry-by-family.

    Retained verbatim as the D15/D50 fallback — governance off, or the tier
    resolves to nothing usable — so `TIER_GOVERNANCE_ENABLED=` reproduces
    today's model id exactly. It is not called on the governed path and it is
    removed with the rest of the .env model constants in Phase 8.
    """
    if is_local_only():
        return LOCAL_LLM_MODEL_NAME

    enable_opus = os.getenv("ENABLE_OPUS", "true").lower() in ("true", "1", "yes")
    # Two extras on top of the shared matcher: this function re-reads
    # ENABLE_OPUS at call time rather than at import, so a test that flips it
    # sees the change.
    _opus_off = set() if enable_opus else {CLAUDE_OPUS_MODEL, CLAUDE_OPUS_48_MODEL}

    def blocked(mid: str) -> bool:
        return mid in _opus_off or is_blocked_model(mid)

    _tier_to_role = {
        "solution":  (_tier_env_override("solution") or CLAUDE_OPUS_MODEL,    "anthropic", "opus"),
        "complex":   (_tier_env_override("complex")  or CLAUDE_PRIMARY_MODEL, "anthropic", "complex"),
        # "simple" is the provider-neutral operator name for the cheap/fast tier
        # (internally keyed "haiku"): SDLC_TIER_SIMPLE_MODEL overrides it.
        "haiku":     (_tier_env_override("simple")   or CLAUDE_HAIKU,         "anthropic", "haiku"),
        "medium":    (_tier_env_override("medium")   or OPENAI_CODING_MODEL,  "openai",    "medium"),
        "deep":      (_tier_env_override("deep")     or OPENAI_LATEST_MODEL,  "openai",    "deep"),
    }
    _key = (hint or "").strip().lower()
    if _key not in _tier_to_role:
        # Not a known tier: treat a non-empty hint as a concrete model id and
        # return it verbatim. BLOCKED_MODELS is the only gate — an operator who
        # names a model has opted in to it. The value is used solely as a
        # model-id string / argv element, never in a shell.
        if hint and hint.strip() and not blocked(hint.strip()):
            return hint.strip()
        return _role_model(_tier_env_override("complex") or CLAUDE_PRIMARY_MODEL, "anthropic", "complex")

    env_value, family, tag = _tier_to_role[_key]
    model_id = _role_model(env_value, family, tag)

    if blocked(model_id):
        return _role_model(_tier_env_override("complex") or CLAUDE_PRIMARY_MODEL, "anthropic", "complex")
    return model_id


def cli_tier_model_id(tier, legacy_hint: str, override: str = "", *,
                      override_name: str = "", require_role: str = None) -> str:
    """Concrete model id for an out-of-process CLI phase (§N.1 step 10).

    The concrete-id twin of models.model_router.tier_request(): same precedence,
    same D15 parity contract, different return type because the caller needs a
    string to put in argv rather than kwargs to splat into generate().

    Precedence, highest first:

      0. is_local_only() — the deployment posture wins over everything. Checked
         before the tier so no cloud model id can escape to a CLI spawn.
      1. `override` — a non-blank per-phase env var (SDLC_CLI_<PHASE>_MODEL,
         SDLC_MODEL_<STAGE>, SDLC_GOVERNANCE_*_MODEL). The operator named a
         model, and governance does not second-guess an explicit name for the
         same reason it does not second-guess a user's dropdown pick. DEPRECATED
         (§I, Phase 8) and warned once per variable per process.
      2. `tier` — the administrator's assignment, when governance is on AND at
         least one candidate is addressable.
      3. `legacy_hint` — what this phase resolved before migration, through the
         untouched .env chain. Reached whenever (2) produces nothing.

    `require_role` is a preference within the tier (§M.3a), matching the
    resolver's own semantics: a deployment with one model still gets an answer.
    """
    # 0. Deployment posture. Before the tier, not after: the whole point of
    #    LLM_PROVIDER=local is that no cloud id can reach a provider, and a
    #    governed answer is still a cloud id.
    if is_local_only():
        return LOCAL_LLM_MODEL_NAME

    # 1. Operator override.
    if override and override.strip():
        value = override.strip()
        if override_name and override_name not in _CLI_OVERRIDE_WARNED:
            _CLI_OVERRIDE_WARNED.add(override_name)
            logger.warning(
                f"[model_registry] {override_name}={value!r} is set, so it overrides the "
                f"tier assignment for this SDLC phase. This variable is DEPRECATED — "
                f"assign a model on Model Governance > Tiers and unset it."
            )
        # A tier NAME is still accepted here: SDLC_CLI_PLAN_MODEL=solution has
        # always meant "the solution tier", and reading it as a model id would
        # hand the CLI the literal string "solution".
        if value.lower() in _LEGACY_CLI_TIER_NAMES:
            return _legacy_cli_model_for_tier(value.lower())
        if value.lower() == "local":
            # Historical: the ainxt CLI has no Ollama bridge, so "local" meant
            # the cheap Anthropic model, not the in-house one. Preserved.
            return _role_model(CLAUDE_HAIKU, "anthropic", "haiku")
        if is_blocked_model(value):
            return _role_model(_tier_env_override("complex") or CLAUDE_PRIMARY_MODEL,
                               "anthropic", "complex")
        return value

    # 2. The administrator's assignment.
    try:
        from core.tiers import governance_enabled
        governed = governance_enabled()
    except Exception:                                    # noqa: BLE001
        governed = False

    if governed:
        rejected: list = []
        try:
            from core.tier_resolver import Constraints, resolve_tier_candidates
            from core.tiers import Tier
            for cand in resolve_tier_candidates(
                    Tier(tier), Constraints(require_role=require_role)):
                if is_blocked_model(cand.model_id):
                    rejected.append(f"{cand.model_id} (blocked on this deployment)")
                    continue
                if not cli_model_is_addressable(cand.model_id):
                    _known = sorted(cli_acceptable_model_ids())
                    rejected.append(
                        f"{cand.model_id} (the ainxt CLI refuses this id; it accepts "
                        + (f"only {', '.join(_known)}" if _known
                           else f"ids beginning {'/'.join(CLI_ADDRESSABLE_MODEL_PREFIXES)}")
                        + ")")
                    continue
                return cand.model_id
        except Exception as exc:                          # noqa: BLE001
            rejected.append(f"tier resolution unavailable ({exc})")

        _warn_cli_tier_fallback(tier, legacy_hint, rejected)

    # 3. The pre-migration answer.
    return _legacy_cli_model_for_tier(legacy_hint)


# Warned once per (tier, reason-set) so a pipeline that resolves the same phase
# forty times in one run produces one line, not forty.
_CLI_OVERRIDE_WARNED: set = set()
_CLI_TIER_FALLBACK_WARNED: set = set()
_LEGACY_CLI_TIER_NAMES = frozenset({"haiku", "complex", "medium", "solution", "deep"})


def _warn_cli_tier_fallback(tier, legacy_hint: str, rejected: list) -> None:
    name = getattr(tier, "value", tier)
    key = (str(name), tuple(rejected))
    if key in _CLI_TIER_FALLBACK_WARNED:
        return
    _CLI_TIER_FALLBACK_WARNED.add(key)
    logger.warning(
        f"[model_registry] no model assigned to the {name!r} tier can be used by the "
        f"ainxt CLI — " + ("; ".join(rejected) or "the tier has no candidates") +
        f". Falling back to the deprecated .env chain for {legacy_hint!r}. To govern "
        f"this phase from Model Governance > Tiers, assign a model the CLI is "
        f"configured for (see `ainxt models`, generated into bin/config.toml by "
        f"sdlc-setup.sh) — the tier assignment cannot widen what the CLI accepts."
    )


# ── Named CLI phases ────────────────────────────────────────────────────────
#
# Thin wrappers so the phase → tier decision lives in ONE place per phase
# rather than at each of the eleven spawn sites, and so the deprecated
# SDLC_CLI_<PHASE>_MODEL override is applied identically at all of them.
# The tiers are plan.html §D.2's, chosen from what the phase DOES.


def cli_classify_model() -> str:
    """CLASSIFY — a JSON classification verdict. §D.2 `simple`."""
    from core.tiers import Tier
    return cli_tier_model_id(Tier.SIMPLE, "haiku",
                             os.getenv("SDLC_CLI_CLASSIFY_MODEL", ""),
                             override_name="SDLC_CLI_CLASSIFY_MODEL")


def cli_plan_model() -> str:
    """PLAN — long-context synthesis of a work item into a plan. §D.2 `complex`."""
    from core.tiers import Tier
    return cli_tier_model_id(Tier.COMPLEX, "complex",
                             os.getenv("SDLC_CLI_PLAN_MODEL", ""),
                             override_name="SDLC_CLI_PLAN_MODEL")


def cli_implement_model() -> str:
    """IMPLEMENT — agentic code generation against visible code. §D.2 `complex`."""
    from core.tiers import Tier
    return cli_tier_model_id(Tier.COMPLEX, "complex",
                             os.getenv("SDLC_CLI_IMPLEMENT_MODEL", ""),
                             override_name="SDLC_CLI_IMPLEMENT_MODEL")


def cli_coder_model() -> str:
    """The coder/fixer CLI session. Replaces cli_model_for("coder").

    Honours SDLC_MODEL_CODER, which is the one SDLC_MODEL_<STAGE> variable that
    reached a CLI spawn rather than an in-process call.
    """
    from core.tiers import Tier
    tier, legacy, _extra = SDLC_STAGE_TIERS["coder"]
    return cli_tier_model_id(tier, legacy, os.getenv("SDLC_MODEL_CODER", ""),
                             override_name="SDLC_MODEL_CODER")


def veo_model() -> str:
    """Resolve the concrete Veo (video-gen) model id.

    Resolution order: explicit ``VEO_MODEL`` env override → an enabled
    registry model of family "gemini" tagged "video" → "" (unchanged from
    the historical blank-constant behavior). Single source of truth for
    both the actual dispatch model (``gateway_gemini.py::generate_veo_video()``)
    and the cost/audit lookups in ``routers/chat_router.py`` — previously
    those two consulted the same blank env constant independently, so a
    blank ``VEO_MODEL`` was both a billing/audit-integrity bug (Veo became
    free and the audit trail recorded no model) and a genuine dispatch
    risk on the direct-SDK path.
    """
    return _role_model(VEO_MODEL, "gemini", "video")


def tier_cost_per_1m(hint: str) -> tuple[float, float]:
    """(input_usd, output_usd) per 1M tokens for a router hint/tier.

    Single source of truth for SDLC cost accounting (RFD R3) — resolves the
    hint to its concrete model id (env override → registry lookup, via
    ``_role_model()``) and prices it with ``rates_for()``. Reads ENABLE_OPUS at call time for the solution tier.
    Local/simple → (0, 0). Unknown hints fall back to the Sonnet (complex)
    rate/model, never $0, so an unrecognised tier over-bills rather than
    silently under-bills.
    """
    h = (hint or "").strip().lower()
    if h in ("local", "simple"):
        return (0.0, 0.0)
    _solution_env = (
        (_tier_env_override("solution") or CLAUDE_OPUS_MODEL)
        if os.getenv("ENABLE_OPUS", "true").lower() in ("true", "1", "yes")
        else (_tier_env_override("complex") or CLAUDE_PRIMARY_MODEL)
    )
    _map = {
        "solution": (_solution_env,                                            "anthropic", "opus"),
        "complex":  (_tier_env_override("complex") or CLAUDE_PRIMARY_MODEL,    "anthropic", "complex"),
        "haiku":    (_tier_env_override("simple")  or CLAUDE_HAIKU,            "anthropic", "haiku"),  # SDLC_TIER_SIMPLE_MODEL
        "medium":   (_tier_env_override("medium")  or OPENAI_CODING_MODEL,     "openai",    "medium"),
        "deep":     (_tier_env_override("deep")    or OPENAI_LATEST_MODEL,     "openai",    "deep"),
        "mini":     (_tier_env_override("mini")    or OPENAI_SIMPLE_MODEL,     "openai",    "simple"),
    }
    if h in _map:
        env_value, family, tag = _map[h]
        model_id = _role_model(env_value, family, tag)
    elif h:
        # Concrete model id (SDLC_MODEL_<STAGE> can now be a raw id) — price it
        # directly rather than mislabeling it as the Sonnet (complex) tier.
        model_id = h
    else:
        model_id = _role_model(_tier_env_override("complex") or CLAUDE_PRIMARY_MODEL, "anthropic", "complex")

    return rates_for(model_id)


# ============================================================
# SDLC GROUNDING / MINIMALISM CHARTER
# ------------------------------------------------------------
# A single authoritative block injected at the top of the scope-defining SDLC
# prompts (analyst, designer / bug-solutioning, coder). Counters the well-known
# LLM bias toward gold-plating — speculative abstractions, unrequested features,
# drive-by refactors, invented dependencies — without tipping into under-build:
# compliance, validation, error handling, and tests for the changed behavior are
# explicitly kept in-scope. "Ask" is calibrated for an autonomous pipeline: raise
# an open question ONLY for high-impact ambiguity, else take the smallest sane
# interpretation and state the assumption.
#
# Rule 5 (clarifying questions) is gated per stage: only stages that actually feed
# the AWAITING_USER_INPUT gate — the feature analyst and the bug solutioning/fix
# designer — get the "raise an open question" variant. Every other stage (feature
# designer, coder, revisions) cannot pause for an answer, so it gets the no-ask
# variant: resolve ambiguity yourself with the minimal interpretation. Rules 1-4
# (grounding/minimalism) apply everywhere.
#
# Kill-switch: set SDLC_GROUNDING_CHARTER=false to disable injection everywhere
# (read at call time — no restart). Default on.
# ============================================================
_GROUNDING_RULES_CORE = (
    "=== ENGINEERING CHARTER — GROUNDING & MINIMALISM (MANDATORY) ===\n"
    "Apply these to everything you produce below:\n"
    "1. MINIMAL SCOPE. Make the SMALLEST change that FULLY satisfies the ticket. Match the\n"
    "   complexity, structure, and idioms of the surrounding code. Do NOT add speculative\n"
    "   abstractions, layers, patterns, configuration, or features the ticket did not ask\n"
    "   for — \"for future flexibility\" / \"just in case\" is NOT a reason.\n"
    "2. NOT A LICENSE TO UNDER-BUILD. Required input validation, error handling, security /\n"
    "   PCI-DSS / PII compliance, and tests for the changed behavior ARE part of \"what is\n"
    "   needed\" — never drop them to look minimal.\n"
    "3. STAY IN BOUNDS. Touch only the files and symbols this ticket requires. No drive-by\n"
    "   refactors, renames, reformatting, or cleanup of code you were not asked to change.\n"
    "4. REAL DEPENDENCIES ONLY. Use the standard library and dependencies already present in\n"
    "   the repo manifest. Never import a package that does not exist. Add a new dependency\n"
    "   only if strictly necessary, only one that really exists, and declare it in the manifest.\n"
)

# Asking variant — ONLY for stages whose open_questions feed the user-input gate.
_GROUNDING_RULE_ASK = (
    "5. SMALLEST REASONABLE INTERPRETATION. If a requirement is ambiguous, take the simplest\n"
    "   interpretation and STATE the assumption. Raise it as an open question ONLY when the\n"
    "   ambiguity is high-impact (materially changes scope or behavior, or a wrong guess\n"
    "   forces rework). Do not pause for low-impact ambiguity.\n"
)

# No-ask variant — for stages that run autonomously and cannot pause for an answer.
_GROUNDING_RULE_NOASK = (
    "5. SMALLEST REASONABLE INTERPRETATION. If a requirement is ambiguous, take the simplest\n"
    "   reasonable interpretation and STATE the assumption explicitly, then proceed. This\n"
    "   stage runs autonomously and CANNOT pause for a user answer — do not block, defer, or\n"
    "   wait on clarifying questions; resolve the ambiguity yourself with the minimal interpretation.\n"
)

_GROUNDING_CHARTER_END = "=== END CHARTER ===\n\n"

# Back-compat full charter (asking variant) for any direct reference.
SDLC_GROUNDING_CHARTER = _GROUNDING_RULES_CORE + _GROUNDING_RULE_ASK + _GROUNDING_CHARTER_END


def grounding_charter(allow_questions: bool = False) -> str:
    """Return the SDLC grounding/minimalism charter block, or '' when disabled via
    SDLC_GROUNDING_CHARTER=false (read at call time so the toggle needs no restart).

    allow_questions=True  → rule 5 permits raising an open question (use ONLY at stages
                            that feed the AWAITING_USER_INPUT gate: feature analyst,
                            bug solutioning/fix designer).
    allow_questions=False → rule 5 tells the stage to resolve ambiguity itself (designer,
                            coder, revisions — they cannot pause for input)."""
    if os.getenv("SDLC_GROUNDING_CHARTER", "true").strip().lower() in ("false", "0", "no"):
        return ""
    rule5 = _GROUNDING_RULE_ASK if allow_questions else _GROUNDING_RULE_NOASK
    return _GROUNDING_RULES_CORE + rule5 + _GROUNDING_CHARTER_END
