# SPDX-License-Identifier: MIT
# ============================================================
# TIER GOVERNANCE — admin assignment of models to capability tiers
#
# The write surface for llm_tier_models: which models an administrator
# considers eligible for each of the eight tiers in core/tiers.py, in priority
# order. Three endpoints, per plan.html §L.4:
#
#   GET /model-governance/tiers                  — always exactly 8 rows
#   PUT /model-governance/tiers/{tier}/models    — replace one tier's list
#   GET /model-governance/tiers/resolved         — diagnostic: what resolves NOW
#
# There is deliberately NO create, delete or rename. The vocabulary is fixed in
# code (core/tiers.py), in the database (ck_llm_tier_models_tier) and here; the
# unregistered methods return 405 on their own, which a test asserts rather
# than adding handlers that exist only to refuse.
#
# ── Why this is a separate module from model_governance_router ──────────────
# That router registers @router.get("/{dept}") under the same
# /model-governance prefix. FastAPI matches in registration order, so a /tiers
# route declared after it would never fire — a GET would be served as "the
# department named 'tiers'". This module is therefore included in gateway.py
# BEFORE model_governance_router, so the literal path wins. Moving these
# handlers into that file would silently break GET /tiers.
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text as _text

from auth.rbac import require_role
from core.tiers import (
    ALL_TIERS,
    MODALITY_REQUIREMENT,
    TIER_DESCRIPTION,
    TIER_LABEL,
    TIER_USED_BY,
    Tier,
)

router = APIRouter(prefix="/model-governance", tags=["tier-governance"])
logger = logging.getLogger(__name__)

_require_admin = require_role("admin")


def _get_db():
    from db.database import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Request bodies ──────────────────────────────────────────────────────────


class TierModelAssignment(BaseModel):
    model_id: str = Field(..., description="llm_models.id (the row UUID, not the API model string)")
    priority: int = Field(100, ge=0, description="lower = preferred")
    role: Optional[str] = Field(None, max_length=32, description="NULL = general; 'review' = §M.3a")

    @field_validator("model_id")
    @classmethod
    def _must_be_uuid(cls, v: str) -> str:
        """Reject a non-UUID here rather than in Postgres.

        `model_id` is llm_models.id — a uuid column — so a malformed value
        reaches the database as `uuid = text` and comes back as a 500. It is a
        bad request, so it should read as one. The name is unfortunately
        overloaded: llm_models ALSO has a `model_id` column holding the string
        sent to the provider ("claude-sonnet-5"), and passing that here is the
        likely mistake, so say so.
        """
        import uuid as _uuid
        try:
            return str(_uuid.UUID(str(v)))
        except (ValueError, AttributeError, TypeError):
            raise ValueError(
                f"'{v}' is not a model row id. Expected the UUID from "
                f"llm_models.id, not the provider's model string."
            )


class TierModelsBody(BaseModel):
    models: list[TierModelAssignment] = Field(default_factory=list)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _parse_tier(value: str) -> Tier:
    """400, never 422: an unknown tier name is a bad path segment, not a
    validation failure on a body. Validated by the enum itself rather than a
    hand-written list, so this cannot drift from core/tiers.py."""
    try:
        return Tier(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"unknown tier '{value}' — the eight tiers are: "
                   + ", ".join(t.value for t in ALL_TIERS),
        )


def _assignments_by_tier(db) -> dict[str, list[dict]]:
    """Every assignment joined to its model and provider, priority-ordered.

    Reads llm_models/llm_providers directly rather than through
    get_enabled_models(), because the admin screen must show an assignment
    whose model has since been DISABLED — otherwise a row silently vanishes
    from the UI while still occupying a priority slot. The resolver applies
    the enabled filter; this surface reports the truth.
    """
    rows = db.execute(_text("""
        SELECT t.tier, t.model_id, t.priority, t.role, t.enabled,
               m.model_id AS api_model_id, m.display_name, m.enabled AS model_enabled,
               m.capabilities, p.name AS provider_name, p.family,
               p.enabled AS provider_enabled
        FROM llm_tier_models t
        JOIN llm_models m ON m.id = t.model_id
        JOIN llm_providers p ON p.id = m.provider_id
        WHERE t.org_id = 'default'
        ORDER BY t.tier, t.priority, m.model_id
    """)).mappings().all()

    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["tier"], []).append({
            "model_id": str(r["model_id"]),
            "api_model_id": r["api_model_id"],
            "display_name": r["display_name"],
            "provider_name": r["provider_name"],
            "family": r["family"],
            "priority": r["priority"],
            "role": r["role"],
            "enabled": r["enabled"],
            "model_enabled": r["model_enabled"],
            "provider_enabled": r["provider_enabled"],
        })
    return out


# ── GET /tiers ──────────────────────────────────────────────────────────────


@router.get("/tiers")
def list_tiers(_admin: dict = Depends(_require_admin), db=Depends(_get_db)):
    """The eight tiers and their eligible models. Admin only.

    Built from ALL_TIERS, not from the table, so a tier with no assignment
    still appears — as `status: "unassigned"`, with the features it disables
    named. A tier that simply vanished from this response would be the silent
    failure the whole migration is removing.
    """
    assigned = _assignments_by_tier(db)
    return {
        "tiers": [
            {
                "tier": t.value,
                "label": TIER_LABEL[t],
                "description": TIER_DESCRIPTION[t],
                "modality_requirement": MODALITY_REQUIREMENT[t],
                "models": assigned.get(t.value, []),
                "status": "active" if assigned.get(t.value) else "unassigned",
                "used_by": list(TIER_USED_BY[t]),
            }
            for t in ALL_TIERS
        ]
    }


# ── PUT /tiers/{tier}/models ────────────────────────────────────────────────


@router.put("/tiers/{tier}/models")
def set_tier_models(
        tier: str,
        body: TierModelsBody,
        admin: dict = Depends(_require_admin),
        db=Depends(_get_db),
):
    """Replace one tier's whole eligible list. Idempotent and ordered.

    Whole-list replacement rather than per-row PATCH because priority is a
    property of the LIST, not of a row: reordering two models is one coherent
    edit, and applying it as two independent writes would transiently violate
    the unique-priority invariant (which is why uq_tier_priority is DEFERRABLE).

    Guardrails per plan.html §J.2 — 400 for an unknown tier, 422 for anything
    that would make the assignment unusable.
    """
    t = _parse_tier(tier)
    entries = body.models or []

    # Duplicate priority — checked before touching the DB so the error names
    # the actual problem rather than surfacing a constraint violation.
    priorities = [e.priority for e in entries]
    if len(set(priorities)) != len(priorities):
        dupes = sorted({p for p in priorities if priorities.count(p) > 1})
        raise HTTPException(422, f"duplicate priority {dupes} within tier '{t.value}'")

    ids = [e.model_id for e in entries]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, f"the same model is listed twice for tier '{t.value}'")

    if entries:
        found = db.execute(_text("""
            SELECT m.id, m.model_id, m.enabled AS model_enabled, m.capabilities,
                   p.enabled AS provider_enabled, p.name AS provider_name
            FROM llm_models m JOIN llm_providers p ON p.id = m.provider_id
            WHERE m.id = ANY(CAST(:ids AS uuid[]))
        """), {"ids": ids}).mappings().all()
        by_id = {str(r["id"]): r for r in found}

        from core.tier_resolver import modality_of

        for e in entries:
            row = by_id.get(e.model_id)
            if row is None:
                raise HTTPException(422, f"unknown model {e.model_id}")
            if not row["model_enabled"]:
                raise HTTPException(
                    422, f"model '{row['model_id']}' is disabled and cannot be assigned")
            if not row["provider_enabled"]:
                raise HTTPException(
                    422, f"provider '{row['provider_name']}' is disabled, so its model "
                         f"'{row['model_id']}' cannot be assigned")
            need = MODALITY_REQUIREMENT[t]
            have = modality_of(row["capabilities"])
            if need not in have:
                # The check that matters most: without it an admin can assign a
                # text model to image-output, and the failure only surfaces
                # later as a wrong answer from a model that cannot do the job.
                raise HTTPException(
                    422,
                    f"model '{row['model_id']}' has modality {have}, which cannot "
                    f"satisfy tier '{t.value}' (requires '{need}')",
                )

    # One transaction: a half-applied reorder would leave the tier in a state
    # the admin never asked for. The deferred unique constraint is what makes
    # delete-then-insert of a reordered list legal here.
    try:
        db.execute(_text("SET CONSTRAINTS uq_tier_priority DEFERRED"))
        db.execute(_text(
            "DELETE FROM llm_tier_models WHERE tier = :tier AND org_id = 'default'"
        ), {"tier": t.value})
        for e in entries:
            db.execute(_text("""
                INSERT INTO llm_tier_models
                    (tier, model_id, priority, role, enabled, org_id, created_by)
                VALUES (:tier, :model_id, :priority, :role, TRUE, 'default', :by)
            """), {
                "tier": t.value, "model_id": e.model_id, "priority": e.priority,
                "role": e.role,
                "by": admin.get("sub") or admin.get("email", "unknown"),
            })
        db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        logger.exception("governance: tier assignment failed for %s", t.value)
        raise HTTPException(422, f"could not assign models to tier '{t.value}': {exc}")

    from core.tier_resolver import invalidate_tier_cache
    invalidate_tier_cache()

    logger.info(
        "governance: tier assignment set by admin=%s tier=%s models=%s",
        admin.get("email", "?"), t.value,
        [(e.model_id, e.priority, e.role) for e in entries],
    )
    return {"ok": True, "tier": t.value, "count": len(entries)}


# ── Explicit refusals ───────────────────────────────────────────────────────


@router.delete("/tiers/{tier}", status_code=405)
@router.delete("/tiers/{tier}/models/{model_id}", status_code=405)
def tiers_cannot_be_deleted(tier: str, model_id: str = ""):
    """405. Tiers are seeded by migration and cannot be created or deleted.

    This handler has to EXIST rather than relying on FastAPI's automatic 405,
    because model_governance_router declares DELETE /{dept}/{model_id} under
    the same prefix — so DELETE /model-governance/tiers/mini would otherwise
    be matched as "delete the model 'mini' from the department 'tiers'",
    execute against dept_model_permissions, and return {"ok": true}. It
    deletes nothing real, but an API that answers "ok" to "delete this tier"
    is exactly the kind of quiet lie this migration exists to remove.

    To remove a tier's models, PUT an empty list instead — the tier then reads
    `unassigned` and the features that request it report unavailable.
    """
    raise HTTPException(
        405,
        f"tiers cannot be deleted — the eight tiers are fixed. To clear "
        f"'{tier}', PUT an empty model list to /tiers/{tier}/models.",
    )


# ── GET /tiers/resolved ─────────────────────────────────────────────────────


@router.get("/tiers/resolved")
def resolved_tiers(
        tier: Optional[str] = Query(None, description="limit to one tier"),
        no_cloud_egress: bool = Query(False),
        min_context_window: Optional[int] = Query(None, ge=1),
        require_role: Optional[str] = Query(None),
        budget_state: Optional[str] = Query(None),
        channel: Optional[str] = Query(None),
        _admin: dict = Depends(_require_admin),
):
    """What each tier resolves to RIGHT NOW, under the supplied constraints.

    The point of this endpoint is the transition: while env constants still
    outrank tier assignments (until Phase 5), an operator needs to see what is
    ACTUALLY routing, not what the Tiers screen implies. Also the Phase 3 exit
    criterion and the doctor.sh probe.

    NoEligibleModel is REPORTED, not raised — an unassigned tier is a normal
    deployment state and the whole purpose of this endpoint is to show it, so
    surfacing it as a 500 would make the diagnostic useless exactly when it is
    needed.
    """
    from core.tier_resolver import Constraints, NoEligibleModel, resolve_tier

    targets = [_parse_tier(tier)] if tier else list(ALL_TIERS)
    c = Constraints(
        no_cloud_egress=no_cloud_egress,
        min_context_window=min_context_window,
        require_role=require_role,
        budget_state=budget_state,
    )

    out: list[dict[str, Any]] = []
    for t in targets:
        entry: dict[str, Any] = {"tier": t.value, "label": TIER_LABEL[t]}
        try:
            r = resolve_tier(t, c, channel=channel)
            entry.update({
                "status": "resolved",
                "model_id": r.model_id,
                "provider": r.provider_slug,
                "family": r.family,
                "served_by_tier": r.tier.value if r.tier else None,
                "via_fallback": r.via_fallback,
                "selection_mode": r.selection_mode,
            })
        except NoEligibleModel as exc:
            entry.update({
                "status": "unresolved",
                "reason": str(exc),
                "rejections": exc.rejections,
            })
        out.append(entry)

    return {"constraints": c.__dict__, "resolved": out}
