# SPDX-License-Identifier: MIT
# ============================================================
# FEATURE → MODEL RESOLUTION
# ============================================================
#
# One question, one answer: "which model should feature X use right now?"
#
# Before this existed, the answer was a tier-name string literal compiled into
# each call site — model_hint="complex" — so changing which model powers Skills
# generation or Teams triage meant editing Python and redeploying. This module
# is the layer that makes it an admin action instead.
#
# WHY THE RETURN VALUE IS A PLAIN STRING
#   ModelRouter.route() already resolves all three things this can return —
#   an admin-registered model_id, a capability name, or a legacy tier alias —
#   because route() step 1a (models/model_router.py) looks an arbitrary
#   registry model_id up from `model_hint` BEFORE consulting _HINT_MAP. So a
#   migrated call site is a one-argument substitution and needs no router
#   change:
#
#       model_router.generate(prompt, model_hint="complex")
#       model_router.generate(prompt, model_hint=resolve_feature_model(
#           "skills.generate", default="complex"))
#
#   The same mechanism is already proven in production by
#   agents_pg.preferred_model, passed straight through as model_hint by
#   agents/agent_builder.py, and by core/model_registry.sdlc_stage_hint()'s
#   SDLC_MODEL_<STAGE> env values.
#
# WHY IT NEVER RAISES, AND WHY `default` EXISTS
#   Passing the call site's CURRENT literal as `default=` is what makes the
#   migration safe to land file-by-file: until an admin assigns something, the
#   resolver hands back exactly what the call site would have used anyway, so
#   migrated and unmigrated sites behave identically. Any failure — unknown
#   feature, DB down, deleted model, disabled provider — returns `default` too.
#   A routing helper must never be the reason a user's turn fails.

from __future__ import annotations

import json
import os
import re
from typing import Optional

from core.logger import logger

# Same shared Redis KV + explicit-invalidation pattern as
# core/llm_provider_registry.py, deliberately: this is read on the hot path by
# every migrated call site, and an admin's change must be visible to every
# gateway worker on its next request rather than after a TTL. The TTL is only a
# safety net for a missed invalidation.
_KV_DB = 0
_CACHE_KEY = "feature_model_config:all"
_CACHE_TTL_SECONDS = 300

_ENV_PREFIX = "AINXT_FEATURE_MODEL_"

# feature_key -> env var suffix: "skills.generate" -> "SKILLS_GENERATE".
_ENV_SUFFIX_RE = re.compile(r"[^A-Z0-9]+")


def env_var_for(feature_key: str) -> str:
    """The break-glass env var name for a feature.

    Mirrors the proven SDLC_MODEL_<STAGE> pattern (core/model_registry.py) so
    an operator can reroute a feature during an incident without DB access.
    """
    return _ENV_PREFIX + _ENV_SUFFIX_RE.sub("_", (feature_key or "").upper()).strip("_")


# ── Config loading ───────────────────────────────────────────────────────────

def _load_from_db() -> dict[str, dict]:
    """All feature_model_config rows, keyed "<feature_key>\\x00<org_id>".

    Loaded in one query and cached whole rather than queried per feature: there
    are a few dozen features at most, and the hot path must not do a round trip
    per LLM call.
    """
    from db.database import SessionLocal
    from db.models import FeatureModelConfig

    db = SessionLocal()
    try:
        out: dict[str, dict] = {}
        for row in db.query(FeatureModelConfig).all():
            out[f"{row.feature_key}\x00{row.org_id}"] = {
                "feature_key": row.feature_key,
                "org_id": row.org_id,
                "model_id": row.model_id,
                "fallback_model_ids": list(row.fallback_model_ids or []),
                "capability_override": row.capability_override,
                "enabled": bool(row.enabled),
            }
        return out
    finally:
        db.close()


def _read_cache() -> Optional[dict[str, dict]]:
    try:
        from core.kv import get_kv
        raw = get_kv(_KV_DB).get(_CACHE_KEY)
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.warning(f"[feature_model_resolver] cache read failed, falling back to DB: {exc}")
    return None


def _write_cache(cfg: dict[str, dict]) -> None:
    try:
        from core.kv import get_kv
        get_kv(_KV_DB).set(_CACHE_KEY, json.dumps(cfg), ex=_CACHE_TTL_SECONDS)
    except Exception as exc:
        logger.warning(f"[feature_model_resolver] cache write failed: {exc}")


def invalidate_cache() -> None:
    """Call at the end of every write to feature_model_config.

    routers/feature_model_config_router.py does this on every mutation, the
    same way llm_provider_admin_router.py does for the provider registry.
    """
    try:
        from core.kv import get_kv
        get_kv(_KV_DB).delete(_CACHE_KEY)
    except Exception as exc:
        logger.warning(f"[feature_model_resolver] cache invalidation failed: {exc}")


def get_all_config() -> dict[str, dict]:
    """Every assignment, cache-first. Returns {} if the table is unreachable.

    {} means "nothing assigned", which degrades to the call sites' existing
    behaviour — the correct failure mode for this layer.
    """
    cached = _read_cache()
    if cached is not None:
        return cached
    try:
        cfg = _load_from_db()
    except Exception as exc:
        # Includes the table not existing yet, on a deployment that has the
        # code but has not run migrate.py.
        logger.warning(f"[feature_model_resolver] config load failed: {exc}")
        return {}
    _write_cache(cfg)
    return cfg


def get_config(feature_key: str, org_id: str = "default") -> Optional[dict]:
    """The effective row for (feature, org): the org's own, else 'default'."""
    cfg = get_all_config()
    return (cfg.get(f"{feature_key}\x00{org_id}")
            or cfg.get(f"{feature_key}\x00default"))


# ── Resolution ───────────────────────────────────────────────────────────────

def resolve_feature_model(
    feature_key: str,
    *,
    default: Optional[str] = None,
    org_id: str = "default",
    data_classification: Optional[str] = None,
) -> Optional[str]:
    """The model hint feature `feature_key` should use.

    Pass the call site's existing literal as `default` — it is returned
    whenever nothing overrides it, which is what keeps an unmigrated and a
    migrated call site behaviourally identical.

    Precedence, highest first:

      1. Privacy floor. CONFIDENTIAL and above must stay on-premise, and that
         is NOT overridable by feature config. Note this is belt-and-braces:
         route() step 0 enforces the same floor and wins regardless of what is
         returned here. Checking it here too means the admin UI's
         "effective model" preview tells the truth instead of showing a cloud
         model that will be silently overridden at runtime.
      2. Env break-glass — AINXT_FEATURE_MODEL_<KEY>. Lets an operator reroute
         a feature mid-incident with no DB access.
      3. The org's own feature_model_config row.
      4. The org_id='default' row (the platform-wide assignment).
      5. feature_registry's declared default_capability.
      6. `default` — the caller's own literal.

    Never raises.
    """
    try:
        # Record the feature on the request/job context so the llm_cost
        # producers can attribute spend to it without every one of the ~66
        # migrated call sites having to pass it a second time. See
        # core.logger.set_feature_key.
        try:
            from core.logger import set_feature_key
            set_feature_key(feature_key)
        except Exception:  # noqa: BLE001 — telemetry must not affect routing
            pass
        return _resolve(feature_key, default, org_id, data_classification)
    except Exception as exc:  # noqa: BLE001 — routing must never break a turn
        logger.warning(
            f"[feature_model_resolver] resolution failed for {feature_key!r} "
            f"({exc}) — falling back to {default!r}"
        )
        return default


def _resolve(
    feature_key: str,
    default: Optional[str],
    org_id: str,
    data_classification: Optional[str],
) -> Optional[str]:
    # 1. Privacy floor — a hard enterprise invariant, checked first.
    from models.model_router import _privacy_requires_local
    if _privacy_requires_local(data_classification):
        logger.info(
            "[feature_model_resolver] %s: data_classification=%s requires local — "
            "feature assignment not applied",
            feature_key, data_classification,
        )
        return "local-only"

    # 2. Env break-glass.
    env_value = (os.getenv(env_var_for(feature_key)) or "").strip()
    if env_value:
        logger.info(
            "[feature_model_resolver] %s: %s=%s (env break-glass)",
            feature_key, env_var_for(feature_key), env_value,
        )
        return env_value

    # 3 + 4. Per-org row, else the platform-wide 'default' row.
    row = get_config(feature_key, org_id)
    if row and row.get("enabled"):
        hint = _hint_from_row(row, feature_key)
        if hint:
            return hint

    # 5. The feature's declared platform default.
    from core.feature_registry import get_feature
    spec = get_feature(feature_key)
    if spec is not None and spec.default_capability:
        return spec.default_capability

    # 6. The call site's own literal.
    return default


def _hint_from_row(row: dict, feature_key: str) -> Optional[str]:
    """Turn an assignment row into a hint string, or None if it is unusable.

    A capability_override is returned as-is. A model_id is resolved through the
    registry to its API model_id, walking fallback_model_ids in order — a
    deleted, disabled, or provider-disabled model must not strand the feature,
    and admin-registered models get no cross-vendor fallback from the router's
    built-in tiers, so this cascade is the only fallback they have.
    """
    if row.get("capability_override"):
        return row["capability_override"]

    from core.llm_provider_registry import get_model_by_uuid

    candidates = [row.get("model_id")] + list(row.get("fallback_model_ids") or [])
    for position, pk in enumerate(candidates):
        if not pk:
            continue
        model = get_model_by_uuid(pk)
        if model:
            if position:
                logger.warning(
                    "[feature_model_resolver] %s: primary model unavailable, "
                    "using fallback #%d (%s)",
                    feature_key, position, model["model_id"],
                )
            return model["model_id"]

    if any(candidates):
        logger.warning(
            "[feature_model_resolver] %s: every assigned model is missing or "
            "disabled — falling through to the platform default",
            feature_key,
        )
    return None


# ── Admin-UI support ─────────────────────────────────────────────────────────

# Which precedence rule produced a resolution. Surfaced by the admin screen's
# "effective model" preview so an admin can see WHY a feature resolves the way
# it does — in particular when the privacy floor has overridden their choice,
# which is correct behaviour but invisible without this.
PRECEDENCE_LABELS = {
    "privacy_floor":      "Pinned on-premise by the data-classification floor",
    "env":                "Overridden by an environment variable (break-glass)",
    "org_assignment":     "Assigned for this organisation",
    "default_assignment": "Assigned platform-wide",
    "feature_default":    "Platform default for this feature",
    "call_site":          "Not assigned — the feature's built-in default applies",
}


def explain(
    feature_key: str,
    *,
    default: Optional[str] = None,
    org_id: str = "default",
    data_classification: Optional[str] = None,
) -> dict:
    """resolve_feature_model(), plus which rule won and a human label.

    Returns {"hint", "rule", "label"}. Kept deliberately separate from
    resolve_feature_model so the hot path stays a single string return.
    """
    try:
        rule = _which_rule(feature_key, default, org_id, data_classification)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[feature_model_resolver] explain failed for {feature_key!r}: {exc}")
        rule = "call_site"
    return {
        "hint": resolve_feature_model(
            feature_key, default=default, org_id=org_id,
            data_classification=data_classification,
        ),
        "rule": rule,
        "label": PRECEDENCE_LABELS.get(rule, rule),
    }


def _which_rule(
    feature_key: str,
    default: Optional[str],
    org_id: str,
    data_classification: Optional[str],
) -> str:
    from models.model_router import _privacy_requires_local
    if _privacy_requires_local(data_classification):
        return "privacy_floor"
    if (os.getenv(env_var_for(feature_key)) or "").strip():
        return "env"

    cfg = get_all_config()
    # An org_id of "default" resolving its own row IS the platform-wide
    # assignment, not an org-specific override, so only look for an
    # org_assignment when the caller asked about some other org. Getting this
    # backwards labelled every platform-wide assignment "Assigned for this
    # organisation" in the admin UI.
    if org_id != "default":
        own = cfg.get(f"{feature_key}\x00{org_id}")
        if own and own.get("enabled") and _hint_from_row(own, feature_key):
            return "org_assignment"
    shared = cfg.get(f"{feature_key}\x00default")
    if shared and shared.get("enabled") and _hint_from_row(shared, feature_key):
        return "default_assignment"

    from core.feature_registry import get_feature
    spec = get_feature(feature_key)
    if spec is not None and spec.default_capability:
        return "feature_default"
    return "call_site"
