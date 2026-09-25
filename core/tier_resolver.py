# SPDX-License-Identifier: MIT
# ============================================================
# TIER RESOLVER  (Phase 3 — not yet wired into routing)
#
# Answers "given a capability tier and the constraints of this request, which
# concrete model should run it?" — the join that core/tiers.py (the eight
# approved tiers) and core/llm_provider_registry.py (what is configured) were
# missing.
#
# Nothing calls this yet. models/model_router.py is untouched in Phase 3; the
# switchover happens in Phase 5 behind TIER_GOVERNANCE_ENABLED. For now the
# only consumer is the diagnostic endpoint GET /model-governance/tiers/resolved,
# which lets an operator see what each tier WOULD resolve to before anything
# depends on the answer.
#
# A leaf module by construction: imports core.tiers and
# core.llm_provider_registry only, never models/. That keeps it importable from
# the router in Phase 5 without a cycle, exactly as core/tiers.py is today.
#
# ── Two design decisions worth knowing before reading the code ──────────────
#
# 1. SELECTION IS STRICT PRIORITY ORDER — first survivor wins. There is no
#    scoring. The administrator's ordering in the Tiers screen is the decision,
#    not a hint: a weighted score over quality/cost/latency would silently
#    override the "1, 2, 3" they set. router/policy.py and profiles/routing.py
#    implement such a scorer and remain deliberately unwired, as their own
#    docstrings say. The one exception is documented on `budget_state` below,
#    and it reorders only among candidates that already passed every filter.
#
# 2. FILTERS ARE DELIBERATELY ASYMMETRIC ABOUT MISSING DATA.
#      privacy_class / context_window : absent ⇒ EXCLUDED
#      supports_tools / supports_streaming : absent ⇒ ALLOWED
#    Under-permitting fails a request visibly and someone fixes the metadata.
#    Over-permitting either leaks data to a cloud provider or dispatches a call
#    the model will reject. The two are not equally bad, so they are not
#    treated equally.
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.logger import logger
from core.tiers import (
    MODALITY_REQUIREMENT,
    MODALITY_TEXT,
    TIER_FALLBACK_LADDER,
    Tier,
)

_KV_DB = 0
_CACHE_KEY = "llm_registry:tier_models"
_CACHE_TTL_SECONDS = 300   # safety net only — invalidate_tier_cache() clears on writes

PRIVACY_DEPLOYMENT_LOCAL = "deployment_local"

BUDGET_OK = "ok"
BUDGET_NEARING_CAP = "nearing_cap"
BUDGET_OVER = "over"

ROLE_REVIEW = "review"


# ── Result and failure types ────────────────────────────────────────────────


@dataclass(frozen=True)
class Constraints:
    """Everything about a request that narrows which model may serve it.

    Each of these would previously have been solved by inventing a new tier.
    They are constraints instead, so the tier keeps meaning "what capability
    does this task need" and never absorbs "what policy applies to this data"
    (plan.html §M).
    """

    no_cloud_egress: bool = False
    min_context_window: Optional[int] = None
    required_modality: Optional[str] = None
    distinct_from_family: Optional[str] = None
    require_role: Optional[str] = None
    budget_state: Optional[str] = None      # ok | nearing_cap | over
    needs_tools: bool = False
    needs_streaming: bool = False


@dataclass(frozen=True)
class ResolvedModel:
    """A concrete dispatch target. Everything a gateway needs to make the call."""

    model_id: str            # the string sent to the provider's API
    row_id: str              # llm_models.id — the UUID, for audit/joins
    provider_id: str
    provider_slug: str
    family: str
    base_url: Optional[str]
    capabilities: dict
    # Both None on the explicit/user path: that request named a model, it did
    # not ask for a capability, and reporting a tier it never requested would
    # make the §L.5 audit columns lie.
    tier: Optional[Tier] = None            # the tier that actually supplied this model
    requested_tier: Optional[Tier] = None  # what the caller asked for
    priority: int = 0
    role: Optional[str] = None

    @property
    def via_fallback(self) -> bool:
        """True when the ladder was walked — i.e. the requested tier had
        nothing eligible and a weaker tier supplied the model instead."""
        return self.tier is not None and self.tier != self.requested_tier

    @property
    def selection_mode(self) -> str:
        """The value Phase 5 writes to model_usages.selection_mode (§L.5)."""
        if self.requested_tier is None:
            return "explicit"
        return "fallback" if self.via_fallback else "tier"


class NoEligibleModel(Exception):
    """No model can serve this tier under these constraints.

    Raised rather than substituting something that cannot do the job. Silent
    substitution is the specific failure this whole migration exists to remove:
    a caller that asked for video generation and got a text model produces a
    confusing wrong answer, where an error produces a fixable one.

    `rejections` maps model_id → why it was dropped, so the operator-facing
    error can say what is actually wrong with the deployment.
    """

    def __init__(self, tier: Optional[Tier], constraints: Constraints, rejections: dict):
        self.tier = tier
        self.constraints = constraints
        self.rejections = rejections
        detail = "; ".join(f"{k}: {v}" for k, v in list(rejections.items())[:6]) \
            or "no candidates assigned"
        what = f"tier '{tier.value}'" if tier is not None else "the requested model"
        super().__init__(f"no eligible model for {what} ({detail})")


# ── Tier assignments (DB-backed, KV-cached — mirrors llm_provider_registry) ──


def _load_assignments_from_db() -> list[dict]:
    from db.database import SessionLocal
    from sqlalchemy import text as _text

    db = SessionLocal()
    try:
        rows = db.execute(_text(
            "SELECT tier, model_id, priority, role, org_id "
            "FROM llm_tier_models "
            "WHERE enabled = TRUE "
            "ORDER BY tier, priority, model_id"
        )).fetchall()
        # str() on the UUID is load-bearing twice over: psycopg2 hands back
        # uuid.UUID objects, while get_enabled_models() reports llm_models.id
        # as a string — so the join in _candidates_for() would silently never
        # match — and json.dumps() cannot serialise a UUID, so the KV cache
        # write would fail on every call and quietly degrade to a DB hit.
        return [
            {"tier": r[0], "model_row_id": str(r[1]), "priority": r[2],
             "role": r[3], "org_id": r[4]}
            for r in rows
        ]
    finally:
        db.close()


def get_tier_assignments() -> list[dict]:
    """Every enabled llm_tier_models row, priority-ordered.

    Cached in the same shared KV as get_enabled_models() and under the same
    TTL, because Phase 5 puts this on the request hot path. Its own key and
    its own invalidator: a tier reassignment does not change the model
    registry, and a provider edit does not change tier assignments.
    """
    try:
        from core.kv import get_kv
        import json
        raw = get_kv(_KV_DB).get(_CACHE_KEY)
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.warning(f"[tier_resolver] cache read failed, falling back to DB: {exc}")

    rows = _load_assignments_from_db()

    try:
        from core.kv import get_kv
        import json
        get_kv(_KV_DB).set(_CACHE_KEY, json.dumps(rows), ex=_CACHE_TTL_SECONDS)
    except Exception as exc:
        logger.warning(f"[tier_resolver] cache write failed: {exc}")
    return rows


def invalidate_tier_cache() -> None:
    """Call at the end of every write to llm_tier_models, so every gateway
    worker sees the reassignment on its next request rather than waiting out
    the TTL. Same contract as llm_provider_registry.invalidate_cache()."""
    try:
        from core.kv import get_kv
        get_kv(_KV_DB).delete(_CACHE_KEY)
    except Exception as exc:
        logger.warning(f"[tier_resolver] cache invalidation failed: {exc}")


# ── Capability readers ──────────────────────────────────────────────────────


def modality_of(capabilities: Optional[dict]) -> list[str]:
    """Read `capabilities.modality` in every shape the column may hold.

    A bare "video" is the pre-Phase-2 scalar form, still emitted by
    gateway.py and branched on by ai-ui's Chat.jsx / KbChat.jsx, so it has to
    keep meaning what it meant. Absent means text-only (plan.html §L.3a).
    """
    raw = (capabilities or {}).get("modality")
    if raw is None:
        return [MODALITY_TEXT]
    if isinstance(raw, str):
        return ["text", "video-out"] if raw == "video" else [raw]
    if isinstance(raw, list):
        return [m for m in raw if isinstance(m, str)]
    return [MODALITY_TEXT]


def _breaker_is_open(provider_slug: str, model_id: str) -> bool:
    """True only if a breaker for this provider/model EXISTS and is open.

    Deliberately does not call get_breaker(), which would CREATE a breaker as a
    side effect — resolving a tier must not register state. Phase 5 rekeys the
    breakers from per-tier to per-provider/model and starts populating these
    keys; until then this is a no-op, which is correct.

    Fails OPEN (returns False) on any error: a model must never be dropped
    because an observability lookup failed. That is the opposite of the privacy
    filter's stance, and deliberately so — an unavailable breaker tells us
    nothing about the model, whereas missing privacy metadata tells us we
    cannot prove the model is safe.
    """
    try:
        from core.circuit_breaker import _breakers

        for key in (f"{provider_slug}:{model_id}", provider_slug):
            breaker = _breakers.get(key)
            if breaker is not None and breaker.is_open():
                return True
    except Exception as exc:
        logger.debug(f"[tier_resolver] breaker check failed for {provider_slug}: {exc}")
    return False


def _reject_reason(model: dict, tier: Tier, c: Constraints) -> Optional[str]:
    """Why this model cannot serve this tier, or None if it can.

    Returns a reason string rather than a bool so NoEligibleModel can tell an
    operator what is actually wrong with their deployment — "every candidate
    is external" is actionable, "no eligible model" is not.
    """
    caps = model.get("capabilities") or {}

    # ── Modality: the tier's own requirement, always enforced ───────────────
    need = MODALITY_REQUIREMENT[tier]
    have = modality_of(caps)
    if need not in have:
        return f"modality {have} lacks {need}"
    if c.required_modality and c.required_modality not in have:
        return f"modality {have} lacks required {c.required_modality}"

    # ── Privacy: fail safe. Absent metadata is NOT deployment_local ─────────
    if c.no_cloud_egress and caps.get("privacy_class") != PRIVACY_DEPLOYMENT_LOCAL:
        return f"privacy_class={caps.get('privacy_class') or 'unset'} != deployment_local"

    # ── Context window: fail safe. An unknown window cannot be asserted ─────
    if c.min_context_window is not None:
        window = caps.get("context_window")
        if not isinstance(window, int) or isinstance(window, bool):
            return f"context_window unknown, cannot satisfy >= {c.min_context_window}"
        if window < c.min_context_window:
            return f"context_window {window} < {c.min_context_window}"

    # ── Cross-provider review (§M.3b): a relational requirement ─────────────
    if c.distinct_from_family and model.get("family") == c.distinct_from_family:
        return f"family {model.get('family')} is not distinct from {c.distinct_from_family}"

    # ── Call shape: permissive on absent metadata ───────────────────────────
    if c.needs_tools and caps.get("supports_tools") is False:
        return "does not support tools"
    if c.needs_streaming and caps.get("supports_streaming") is False:
        return "does not support streaming"

    # ── Availability ────────────────────────────────────────────────────────
    if _breaker_is_open(model.get("provider_slug", ""), model.get("model_id", "")):
        return "circuit breaker open"

    return None


def _cost_key(model: dict) -> tuple:
    """Sort key for budget pressure: cheaper first, unpriced last.

    Unpriced sorts last rather than first. A model with no cost metadata is
    not free — it is unmeasured, and treating "we do not know" as "zero" is
    how a budget control quietly stops working.
    """
    cost = (model.get("capabilities") or {}).get("cost_per_1m_output")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return (0, float(cost))
    return (1, 0.0)


# ── The resolver ────────────────────────────────────────────────────────────


def _candidates_for(tier: Tier, channel: Optional[str], org_id: str) -> list[dict]:
    """Enabled, assigned models for one tier in admin priority order.

    Joined against get_enabled_models() rather than against llm_models
    directly, so a disabled model, a disabled provider, and a channel
    restriction all drop out for free and the registry's Redis cache is
    reused. A tier row pointing at a now-disabled model simply yields no
    candidate, which is what ON DELETE CASCADE plus this join are for.
    """
    from core.llm_provider_registry import get_enabled_models

    by_row_id = {m["id"]: m for m in get_enabled_models(channel=channel)}
    out = []
    for row in get_tier_assignments():
        if row["tier"] != tier.value or row.get("org_id", "default") != org_id:
            continue
        model = by_row_id.get(row["model_row_id"])
        if model is None:
            continue
        out.append({**model, "priority": row["priority"], "role": row.get("role")})
    out.sort(key=lambda m: (m["priority"], m["model_id"]))
    return out


def _pick(candidates: list[dict], tier: Tier, c: Constraints,
          rejections: dict) -> Optional[dict]:
    """First survivor in priority order, with the two §M refinements."""
    survivors = []
    for model in candidates:
        reason = _reject_reason(model, tier, c)
        if reason is None:
            survivors.append(model)
        else:
            rejections[model["model_id"]] = reason

    if not survivors:
        return None

    # §M.3a — require_role is a PREFERENCE, not a filter. A deployment with one
    # model must still be able to run a review stage; the admin sees author and
    # reviewer coincide in the UI rather than having the stage fail.
    if c.require_role:
        preferred = [m for m in survivors if m.get("role") == c.require_role]
        if preferred:
            survivors = preferred

    # §M.4 / RT4 — the ONLY departure from admin priority order, confined to
    # candidates that already passed every filter, and only under real budget
    # pressure. Stable sort, so equal-cost candidates keep their priority order.
    if c.budget_state in (BUDGET_NEARING_CAP, BUDGET_OVER):
        survivors = sorted(survivors, key=_cost_key)

    return survivors[0]


def resolve_tier(tier: Tier, c: Optional[Constraints] = None, *,
                 channel: Optional[str] = None,
                 org_id: str = "default") -> ResolvedModel:
    """Resolve a capability tier to a concrete model under `c`.

    1. candidates = enabled assignments for `tier`, in admin priority order
    2. drop any that fail a constraint, or whose breaker is open
    3. FIRST SURVIVOR WINS — no scoring (see the module docstring)
    4. none left → walk TIER_FALLBACK_LADDER AT MOST ONCE, re-applying the
       same constraints
    5. still none → raise NoEligibleModel

    Raises NoEligibleModel rather than returning None: a caller that forgets to
    check a None gets a wrong model silently, which is the failure mode this
    replaces.
    """
    tier = Tier(tier)
    c = c or Constraints()
    rejections: dict = {}

    chosen = _pick(_candidates_for(tier, channel, org_id), tier, c, rejections)
    if chosen is not None:
        return _to_resolved(chosen, tier, tier)

    # ── Fallback ladder, walked at most once ────────────────────────────────
    # Never under no_cloud_egress (§M.5): the current code already refuses to
    # fall back across the privacy boundary via privacy_local_only, and that
    # behaviour is preserved exactly. Degrading a confidential request onto a
    # weaker tier could land it on an external model, which is the one outcome
    # this constraint exists to prevent.
    if c.no_cloud_egress:
        raise NoEligibleModel(tier, c, rejections)

    # The three modality tiers map to None: nothing substitutes for image or
    # video generation, so there is no ladder to walk.
    nxt = TIER_FALLBACK_LADDER.get(tier)
    if nxt is None:
        raise NoEligibleModel(tier, c, rejections)

    fallback_rejections: dict = {}
    chosen = _pick(_candidates_for(nxt, channel, org_id), nxt, c, fallback_rejections)
    if chosen is not None:
        logger.warning(
            "[tier_resolver] tier %s had no eligible model (%s) — fell back to %s",
            tier.value, rejections or "unassigned", nxt.value,
        )
        return _to_resolved(chosen, nxt, tier)

    rejections.update({f"{nxt.value}/{k}": v for k, v in fallback_rejections.items()})
    raise NoEligibleModel(tier, c, rejections)


def _to_resolved(model: dict, serving: Tier, requested: Tier) -> ResolvedModel:
    return ResolvedModel(
        model_id=model["model_id"],
        row_id=model["id"],
        provider_id=model["provider_id"],
        provider_slug=model["provider_slug"],
        family=model["family"],
        base_url=model.get("base_url"),
        capabilities=model.get("capabilities") or {},
        tier=serving,
        requested_tier=requested,
        priority=model.get("priority", 100),
        role=model.get("role"),
    )


def resolve_explicit(model_id: str, user: Optional[dict] = None, *,
                     channel: Optional[str] = None) -> ResolvedModel:
    """The USER path: the caller named a specific model.

    No tier is consulted and there is no substitution — an invalid pick is an
    error, not a silent downgrade to something that happens to be available.
    Tier assignments are irrelevant here by design: a model with no tier is
    still directly selectable (plan.html §H), because tiers govern only what
    the platform picks on the user's behalf.

    ACL enforcement stays where it already lives
    (model_governance_router.filter_allowed_models) and is applied by the
    caller; this function answers "is this model real, enabled and visible on
    this channel", which is the part the registry owns.
    """
    from core.llm_provider_registry import get_enabled_models

    for m in get_enabled_models(channel=channel):
        if m["model_id"] == model_id:
            return ResolvedModel(
                model_id=m["model_id"], row_id=m["id"],
                provider_id=m["provider_id"], provider_slug=m["provider_slug"],
                family=m["family"], base_url=m.get("base_url"),
                capabilities=m.get("capabilities") or {},
                tier=None, requested_tier=None, priority=0,
            )
    raise NoEligibleModel(
        None, Constraints(),
        {model_id: "not an enabled model on this channel"},
    )
