# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt MODEL REGISTRY — platform-wide model policy, not model choice.
#
# Which model serves what is admin data: providers and models in the registry
# (core.llm_provider_registry), assignments on the Tiers screen. Phase 8
# removed the .env model constants that used to live here. What remains is the
# deny-list, the cost authority, the deployment posture and the SDLC tables.
# ============================================================

import os
import re

from core.logger import logger

# ============================================================
# PROVIDER POSTURE  —  the single switch an adopter needs
# ============================================================
#
#   LLM_PROVIDER=cloud   (default)  Any registered provider may serve a tier.
#   LLM_PROVIDER=local              An ASSERTION: every enabled model must be
#                                   deployment-local. Checked at startup
#                                   (posture_violations); it creates no tier
#                                   and no routing branch (§I.5).
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


def posture_violations() -> list:
    """Enabled models that break LLM_PROVIDER=local (not deployment-local); [] otherwise."""
    if not is_local_only():
        return []
    try:
        from core.llm_provider_registry import get_enabled_models
        return sorted(m["model_id"] for m in get_enabled_models()
                      if (m.get("capabilities") or {}).get("privacy_class") != "deployment_local")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[model_registry] posture check unavailable: {exc}")
        return []


# Exposes the raw OpenAI passthrough endpoints — an API surface toggle (§I.5 Keep).
ENABLE_RAW_OPENAI_API = os.getenv("ENABLE_RAW_OPENAI_API", "false").lower() in ("true", "1", "yes")

# Per-second price for a video model whose registry row records none; platform-wide.
VEO_COST_PER_SECOND = float(os.getenv("VEO_COST_PER_SECOND", "0.40"))


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
    """True when the user is ad_level 0 or admin. Availability is the video tier's assignment."""
    if not current_user:
        return False
    if current_user.get("role") == "admin":
        return True
    try:
        return int(current_user.get("ad_level", 6)) == 0
    except (TypeError, ValueError):
        return False


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
# Static by design (§I.6): retired ids only. Whether any other model is offered
# is llm_providers.enabled AND llm_models.enabled AND its channels.


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
# ends up the admin-configured default. Phase 8 removed the env var: a newer or
# self-hosted model declares capabilities.supports_temperature (accepts_temperature).
_MODELS_WITHOUT_TEMPERATURE_DEFAULTS = (
    "claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7",
)


def models_without_temperature() -> tuple[str, ...]:
    """Prefix list of model ids that must NOT receive a `temperature` param.

    Anything newer is declared per model: capabilities.supports_temperature=false.
    """
    return _MODELS_WITHOUT_TEMPERATURE_DEFAULTS


def accepts_temperature(model_id: str) -> bool:
    """False when the registry row says supports_temperature=false, or the id has a known prefix."""
    try:
        row = _registry_row_for((model_id or "").strip())
        declared = ((row or {}).get("capabilities") or {}).get("supports_temperature")
        if isinstance(declared, bool):
            return declared
    except Exception:  # noqa: BLE001
        pass
    return not (model_id or "").startswith(_MODELS_WITHOUT_TEMPERATURE_DEFAULTS)


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
# The reviewer is a ROLE within the complex tier (§M.3a), so an absent reviewer
# is answered by the resolver preferring the head of the tier.

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

    """
    mid = (model_id or "").strip()
    if mid in BLOCKED_MODELS:
        return True
    base = _DATED_SNAPSHOT.sub("", mid)
    return base != mid and base in BLOCKED_MODELS


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
# it names. Assign anything else and this resolver refuses, naming why.
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


def _tier_or_none(value: str):
    from core.tiers import Tier
    try:
        return Tier((value or "").strip().lower())
    except ValueError:
        return None


def cli_tier_model_id(tier, legacy_hint: str = "", override: str = "", *,
                      override_name: str = "", require_role: str = None) -> str:
    """Concrete model id for an out-of-process CLI phase (§N.1 step 10).

    The concrete-id twin of models.model_router.tier_request(), for callers
    that put a model name in argv. Precedence:

      1. `override` — SDLC_MODEL_<STAGE>, which accepts one of the eight tier
         names (resolved as that tier) or a model id (used as-is unless blocked).
      2. `tier` — the first assigned candidate the ainxt CLI can address.

    Raises NoEligibleModel when nothing qualifies, naming each rejection (D107).
    `legacy_hint` is accepted for call-site compatibility and not consulted.
    """
    from core.tier_resolver import Constraints, NoEligibleModel, resolve_tier_candidates
    from core.tiers import Tier

    if override and override.strip():
        value = override.strip()
        as_tier = _tier_or_none(value)
        if as_tier is not None:
            tier = as_tier
        elif is_blocked_model(value):
            logger.warning(f"[model_registry] {override_name or 'override'}={value!r} is a "
                           f"blocked model; using the tier assignment instead.")
        else:
            return value

    rejected: dict = {}
    try:
        candidates = resolve_tier_candidates(Tier(tier), Constraints(require_role=require_role))
    except NoEligibleModel:
        raise
    except Exception as exc:  # noqa: BLE001
        raise NoEligibleModel(Tier(tier), Constraints(), {"resolver": str(exc)}) from exc
    for cand in candidates:
        if is_blocked_model(cand.model_id):
            rejected[cand.model_id] = "blocked on this deployment"
            continue
        if not cli_model_is_addressable(cand.model_id):
            _known = sorted(cli_acceptable_model_ids())
            rejected[cand.model_id] = (
                "the ainxt CLI refuses this id; it accepts "
                + (f"only {', '.join(_known)}" if _known
                   else f"ids beginning {'/'.join(CLI_ADDRESSABLE_MODEL_PREFIXES)}"))
            continue
        return cand.model_id
    raise NoEligibleModel(Tier(tier), Constraints(), rejected)


# ── Named CLI phases ────────────────────────────────────────────────────────
#
# Thin wrappers so the phase → tier decision lives in ONE place per phase
# rather than at each of the eleven spawn sites. The tiers are plan.html §D.2's,
# chosen from what the phase DOES.


def cli_classify_model() -> str:
    """CLASSIFY — a JSON classification verdict. §D.2 `simple`."""
    from core.tiers import Tier
    return cli_tier_model_id(Tier.SIMPLE)


def cli_plan_model() -> str:
    """PLAN — long-context synthesis of a work item into a plan. §D.2 `complex`."""
    from core.tiers import Tier
    return cli_tier_model_id(Tier.COMPLEX)


def cli_implement_model() -> str:
    """IMPLEMENT — agentic code generation against visible code. §D.2 `complex`."""
    from core.tiers import Tier
    return cli_tier_model_id(Tier.COMPLEX)


def cli_coder_model() -> str:
    """The coder/fixer CLI session; SDLC_MODEL_CODER may name a tier (§I.3: tier names only)."""
    tier, _legacy, _extra = SDLC_STAGE_TIERS["coder"]
    pinned = os.getenv("SDLC_MODEL_CODER", "")
    if pinned.strip() and _tier_or_none(pinned) is None:
        logger.warning(f"[model_registry] SDLC_MODEL_CODER={pinned!r} is not a tier name; ignored.")
        pinned = ""
    return cli_tier_model_id(tier, override=pinned, override_name="SDLC_MODEL_CODER")


def veo_model() -> str:
    """The video-generation tier's model id, or "" when none is assigned."""
    from core.tiers import Tier
    try:
        from core.tier_resolver import resolve_tier
        return resolve_tier(Tier.VIDEO_GENERATION).model_id
    except Exception:  # noqa: BLE001 — callers treat "" as "video unavailable"
        return ""


def tier_cost_per_1m(hint: str) -> tuple[float, float]:
    """(input_usd, output_usd) per 1M tokens for a router hint, tier name or model id.

    A hint that names a tier (directly or as a legacy alias) is priced as that
    tier's head model; anything else is priced as a concrete id. Priced by
    rates_for(), so an unresolvable answer over-bills rather than costing $0.
    """
    from core.tiers import Tier, resolve_legacy_alias
    h = (hint or "").strip()
    tier = _tier_or_none(h) or resolve_legacy_alias(h.lower())
    if not h:
        tier = Tier.COMPLEX
    if isinstance(tier, Tier):
        try:
            from core.tier_resolver import resolve_tier
            return rates_for(resolve_tier(tier).model_id)
        except Exception:  # noqa: BLE001
            return UNPRICED_RATES
    return rates_for(h)


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
