# SPDX-License-Identifier: MIT
# ============================================================
# ENDPOINT MODEL CATALOG — cloud/local classification + cost for managed endpoints
#
# One place that answers three questions for the managed-endpoint feature:
#   1. Is this model CLOUD (paid) or LOCAL (in-house, free)?
#   2. What is the full catalog an admin may choose from?
#   3. What does a completed call cost in USD?
#
# WHY THIS MODULE EXISTS
#   The codebase has FIVE competing "is this in-house" predicates
#   (middleware/budget_middleware._is_inhouse_model, gateway._req_is_inhouse,
#   routers/messages_compat_router._is_in_house_model,
#   ABStudio governance._is_local_model, gateway_local_llm.is_local_model) and
#   FOUR cost functions with different unknown-model behaviour. Rather than add a
#   sixth/fifth, this module delegates to the AUTHORITATIVE source for each
#   question and is imported by both endpoint routers so they can never disagree
#   about what is billable.
#
# CLASSIFICATION STRATEGY (deliberately allowlist-based, not prefix-based)
#   cloud  := membership in the platform's own cloud catalog (feature-flag aware,
#             BLOCKED_MODELS filtered)
#   local  := gateway_local_llm.is_local_model() — the live LiteLLM catalog, so
#             names like "Kimi-k2.5" / "glm-5.2" that no prefix heuristic catches
#             are still recognised.
#   Anything in neither set is UNKNOWN and is never treated as free.
# ============================================================

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from core.logger import logger

# Conservative fallback rate for a model we cannot price: the gpt-5.4 rate, matching
# gateway._estimate_cost:315. Deliberately NOT (0,0) — an unpriced cloud model must
# over-bill rather than silently bill nothing (which is what
# messages_compat_router._compute_cost_usd and ABStudio's estimate_model_cost do).
_UNKNOWN_RATES = (2.00, 8.00)

# "local" appearing anywhere in the model name is the platform's de-facto $0 marker
# (gateway._estimate_cost:302). Kept for parity so labels like
# "Local (In-house) (Kimi-k2.5)" and "local:glm-5.2" price at zero.
_LOCAL_MARKER = "local"


# ── Cloud catalog ────────────────────────────────────────────────────────────

def get_cloud_models() -> List[str]:
    """
    Cloud (billable) models this platform exposes — sourced entirely from
    core.llm_provider_registry (the "LLM Providers" admin screen's data).
    A registry model counts as "cloud" unless its family is "ollama" or its
    capabilities mark it billing_tier="free".

    Mirrors routers/model_governance_router._all_model_ids() so the endpoint
    admin picker offers exactly the same cloud set as model governance.
    Returns [] on failure (fail-closed: no cloud models selectable) or if no
    providers are configured yet.

    Previously built from ~15 core.model_registry env-var constants plus
    ENABLE_* feature flags — those constants are role-specific overrides
    install.sh's "LLM Providers" flow never sets, so a purely
    admin-configured deployment had this function return a list of blank
    strings — see the LLM provider config design doc's post-launch fixes.
    """
    try:
        from core.model_registry import BLOCKED_MODELS
        from core.llm_provider_registry import get_enabled_models
    except Exception as exc:
        logger.warning("endpoint_catalog: registry import failed → %s", exc)
        return []

    try:
        out: List[str] = []
        for m in get_enabled_models():
            mid = m["model_id"]
            if not mid or mid in BLOCKED_MODELS or m["family"] == "ollama":
                continue
            if m["capabilities"].get("billing_tier") == "free":
                continue
            if mid not in out:
                out.append(mid)
        return out
    except Exception as exc:
        logger.warning("endpoint_catalog: registry read failed → %s", exc)
        return []


def get_local_models() -> List[str]:
    """Live local (LiteLLM) model catalog. [] when the proxy is unreachable."""
    try:
        from gateway_local_llm import get_local_gateway
        return list(get_local_gateway().list_models() or [])
    except Exception as exc:
        logger.warning("endpoint_catalog: local model list failed → %s", exc)
        return []


# ── Classification ───────────────────────────────────────────────────────────

def is_cloud_model(model: str) -> bool:
    """True only for models in the platform's cloud catalog (exact match)."""
    if not model:
        return False
    return model in set(get_cloud_models())


def is_local_model(model: str) -> bool:
    """
    True for in-house models served by the local LiteLLM fleet.

    Delegates to gateway_local_llm.is_local_model (live catalog, strips a
    "local:" prefix). Falls back to the "local" substring marker so labels the
    catalog doesn't know still classify correctly. Also checks
    core.llm_provider_registry for admin-registered Ollama models called by
    their bare model_id (e.g. "llama3.2"), which carry neither a "local:"
    prefix nor the substring "local".
    """
    if not model:
        return False
    try:
        from gateway_local_llm import is_local_model as _iglm
        if _iglm(model):
            return True
    except Exception:
        pass
    m = model.lower()
    if m.startswith("local:") or _LOCAL_MARKER in m:
        return True
    try:
        from core.llm_provider_registry import get_model as _get_registry_model
        reg = _get_registry_model(model)
        if reg and (reg["family"] == "ollama" or reg["capabilities"].get("billing_tier") == "free"):
            return True
    except Exception:
        pass
    return False


def classify_model(model: str) -> str:
    """'cloud' | 'local' | 'unknown'. Cloud wins ties — never under-bill."""
    if is_cloud_model(model):
        return "cloud"
    if is_local_model(model):
        return "local"
    return "unknown"


def provider_of(model: str) -> str:
    """
    'openai' | 'claude' | 'gemini' | 'unknown' for a CLOUD model id.

    Delegates to services.llm_spend.approved_models's proven provider regexes
    (_normalise + _provider_of) rather than re-implementing prefix matching a
    sixth time — that module already normalises display-label wrapping
    ("GPT-5.4 (Coding) (gpt-5.4) [fallback]" -> "gpt-54") and provider-prefixes
    a third-party-verified way. Only used to pick which of the three
    /llm/{provider}-tools-stream endpoints a tool-call request should hit
    (services/cloud_tool_stream.py) — never for cost or cloud/local
    classification, which stay authoritative in this module via
    get_cloud_models()/is_cloud_model().

    Returns "unknown" for local models and anything unrecognised; callers must
    treat "unknown" as "cannot serve tool calls for this model" rather than
    guessing a provider.
    """
    if not model:
        return "unknown"
    try:
        from services.llm_spend.approved_models import _normalise, _provider_of
    except Exception as exc:
        logger.warning("endpoint_catalog: approved_models import failed → %s", exc)
        return "unknown"

    canon = _normalise(model)
    provider = _provider_of(canon)
    # approved_models uses "anthropic" as the bucket name; the tools-stream
    # endpoint and gateway.py's _model_hint conventions use "claude".
    if provider == "anthropic":
        return "claude"
    if provider in ("openai", "gemini"):
        return provider
    return "unknown"


# Hints that carry no model choice yet. They may still resolve to a paid cloud
# model, so they are never budget-exempt.
_AUTO_HINTS = frozenset({"", "auto", "default"})


def is_budget_exempt(hint: str) -> bool:
    """True only when `hint` names a known in-house model, which costs nothing.

    THE one predicate for "skip the budget check". Five call paths each had
    their own copy of this, all byte-identical and all wrong the same way:

        _CLOUD_PREFIXES = ("gpt-", "claude-", "gemini-", "openai/",
                           "anthropic/", "google/", "azure/")
        in_house = hint and hint not in ("auto","default") \\
                   and not any(hint.startswith(p) for p in _CLOUD_PREFIXES)

    (middleware/budget_middleware.py, workers/chat_worker.py, gateway.py twice,
    routers/ide_router.py — plus a sixth, DIVERGENT variant in
    routers/messages_compat_router.py that is a normalisation allowlist, not a
    billing gate, and is deliberately left alone.)

    "Not one of seven known cloud prefixes therefore free" inverts the safe
    default. Every model an admin registers through the openai_compatible
    family whose id starts with none of those prefixes was declared free and
    skipped budget enforcement entirely — including paid models like
    mistralai/mixtral-8x7b-instruct, meta-llama/llama-3.1-70b-instruct,
    deepseek-chat, o3-mini, any Bedrock us.anthropic.* id, any Vertex
    ...@20240620 id, and every OpenRouter vendor/model id outside the list.
    Router tier hints ("complex", "haiku", "medium") were exempted too.

    classify_model() is allowlist-based and fails closed: anything it cannot
    place is "unknown", and "unknown" != "local", so an unrecognised model is
    budget-CHECKED rather than waved through.

    Case is preserved on purpose. Both halves of classify_model match exactly
    against a catalogue, and real in-house ids carry capitals
    ("gemma-4-31B-it", "qwen-3.6-35B-A3B"); lower-casing first made those miss
    the local catalogue and fall through to "unknown", which would newly bill a
    genuinely free in-house model.

    Never raises: on any failure it returns False, i.e. enforce the budget.
    """
    h = (hint or "").strip()
    if not h or h.lower() in _AUTO_HINTS:
        return False
    try:
        return classify_model(h) == "local"
    except Exception as exc:  # noqa: BLE001 — an outage must not decide billing
        logger.warning(
            "endpoint_catalog: budget classification failed for %r (%s) — enforcing budget",
            h, exc,
        )
        return False


def family_of(model: str) -> str:
    """
    'anthropic' | 'openai' | 'gemini' | 'openai_compatible' | 'local' | 'unknown'
    for any model id — the provider FAMILY, resolved from the DB registry rather
    than parsed out of the model's name.

    This is the one predicate that replaces name-prefix guessing wherever code
    needs to know "which vendor contract does this model speak". Three call
    sites that each inferred it independently, and got it wrong for
    admin-registered models, now delegate here:

      - services/feedback_processor.py, models/classifier.py,
        models/hybrid_retriever.py — they hardcoded provider="claude" next to a
        dynamically resolved model, so repointing a tier at a non-Anthropic
        model sent a mismatched pair to /llm/generate.
      - ABStudio app/core/llm_handler._classify_model — its "everything else is
        local" default routed OpenRouter ids like "anthropic/claude-sonnet-4-6"
        to the in-house LiteLLM endpoint.
      - middleware/budget_middleware — see classify_model() for the billing
        half of the same problem.

    Resolution order (registry first, name heuristics last):
      1. core.llm_provider_registry — authoritative. Its `family` column is
         already populated with exactly the five values the admin UI writes.
         "ollama" is reported as "local" because that is what callers act on.
      2. classify_model() == "local" — catches in-house LiteLLM ids that have
         no registry row (the live local catalog).
      3. provider_of() — the cloud-catalog regexes, for a cloud model the
         registry lookup missed.

    Returns "unknown" rather than guessing. Callers MUST handle it:
    "openai_compatible" and "unknown" cannot be served by the three built-in
    vendor gateways, so a caller whose only options are claude/openai/gemini
    must fall back, not coerce.
    """
    if not model:
        return "unknown"

    try:
        from core.llm_provider_registry import get_model as _get_registry_model
        reg = _get_registry_model(model)
    except Exception as exc:
        logger.warning("endpoint_catalog: registry family lookup failed for %r → %s", model, exc)
        reg = None
    if reg:
        fam = (reg.get("family") or "").strip().lower()
        if fam == "ollama":
            return "local"
        if fam in ("anthropic", "openai", "gemini", "openai_compatible"):
            return fam

    if classify_model(model) == "local":
        return "local"

    provider = provider_of(model)
    if provider == "claude":
        return "anthropic"
    if provider in ("openai", "gemini"):
        return provider
    return "unknown"


# Families the built-in vendor gateways (and the llm_proxy's /llm/generate
# provider field) can actually serve. "openai_compatible" is deliberately
# absent: those models need their provider row's own base_url, which the
# three-way provider switch has no way to supply.
_PROXY_PROVIDER_BY_FAMILY = {
    "anthropic": "claude",
    "openai":    "openai",
    "gemini":    "gemini",
}


def proxy_provider_for(model: str) -> Optional[str]:
    """
    The `provider` value to send to the LLM proxy's /llm/generate for `model`,
    or None when no built-in gateway can serve it.

    /llm/generate resolves its gateway from `provider` alone
    (services/llm_proxy/main.py::_resolve_gateway, which accepts only
    claude|openai|gemini) and forwards `model` untouched — so a mismatched pair
    reaches the wrong vendor's SDK and fails. Callers must treat None as "skip
    this optional enrichment", never as a reason to guess "claude".
    """
    return _PROXY_PROVIDER_BY_FAMILY.get(family_of(model))


def has_cloud_models(model_ids: Optional[List[str]]) -> bool:
    """True if any entry in an endpoint's allowlist is a paid cloud model."""
    if not model_ids:
        return False
    cloud = set(get_cloud_models())
    return any(m in cloud for m in model_ids)


def first_local_model(model_ids: Optional[List[str]]) -> Optional[str]:
    """
    First LOCAL model in an endpoint's allowlist, or None.

    Used as the fallback target for an unrecognised model so it resolves to free
    in-house inference instead of silently escalating to paid cloud (which is what
    the platform's own default routing does — see gateway._oai_model_hint
    returning None -> ModelRouter auto-route -> TIER_MEDIUM -> gpt-5.4).
    """
    for m in (model_ids or []):
        if is_local_model(m):
            return m
    return None


# ── Cost ─────────────────────────────────────────────────────────────────────

def _rates_for(model: str):
    """
    (input_usd, output_usd) per 1M tokens.

    Same resolution order as gateway._estimate_cost (the platform-standard
    implementation, and the only one that never returns $0 for an unknown cloud
    model): exact key, then substring scan so display labels like
    "GPT-5.4 (Coding) (gpt-5.4) [fallback]" still price correctly, then a
    conservative default.

    Replicated here rather than imported so the request path does not pull in the
    12k-line gateway.py module.
    """
    try:
        from core.model_registry import MODEL_COST_PER_1M
    except Exception:
        return _UNKNOWN_RATES

    rates = MODEL_COST_PER_1M.get(model)
    if rates is not None:
        return rates

    m = (model or "").lower()
    for mid, r in MODEL_COST_PER_1M.items():
        if mid and mid.lower() in m:
            return r
    return _UNKNOWN_RATES


def cheapest_cloud_model(model_ids: Optional[List[str]]) -> Optional[str]:
    """
    Cheapest CLOUD model in an endpoint's allowlist, or None if it contains no
    cloud models. Ranked by (input_per_1M + output_per_1M) from the same
    pricing table estimate_cost_usd/price_hint already use — ties broken by
    allowlist order (min() keeps the first-seen winner), so the result is
    deterministic for a given allowlist.

    This is the SECOND tier of the fallback an endpoint uses for an
    unrecognised model name: first_local_model() (free, preferred) is tried
    first by the caller; this is reached only when the allowlist has no local
    model at all. Computed here — never admin-set — so it can never drift out
    of sync with the endpoint's actual allowlist or the platform's current
    pricing.
    """
    cloud_ids = [m for m in (model_ids or []) if is_cloud_model(m)]
    if not cloud_ids:
        return None

    def _rank(m: str) -> float:
        rin, rout = _rates_for(m)
        return rin + rout

    return min(cloud_ids, key=_rank)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """
    USD cost of a call, as Decimal (callers persist NUMERIC(12,6)).

    In-house models are always free. Everything else is priced from
    MODEL_COST_PER_1M, defaulting to a conservative rate when unknown so an
    unpriced cloud model over-bills rather than escaping billing entirely.
    """
    if not model:
        return Decimal("0")

    m = model.lower()
    if _LOCAL_MARKER in m or is_local_model(model):
        return Decimal("0")

    rin, rout = _rates_for(model)
    cost = (
        (Decimal(str(input_tokens or 0))  * Decimal(str(rin)))
        + (Decimal(str(output_tokens or 0)) * Decimal(str(rout)))
    ) / Decimal("1000000")
    # 6 dp matches hod_allocation_ledger.endpoint_spend_usd / model_usages precision.
    return cost.quantize(Decimal("0.000001"))


def price_hint(model: str) -> Optional[Dict[str, float]]:
    """
    {"input_per_1m", "output_per_1m"} for the admin UI, or None for free models —
    so an admin sees the cost implication before enabling a cloud model.
    """
    if not model or classify_model(model) == "local":
        return None
    rin, rout = _rates_for(model)
    return {"input_per_1m": float(rin), "output_per_1m": float(rout)}
