# SPDX-License-Identifier: MIT
# ============================================================
# FEATURE → MODEL CONFIG — admin CRUD
# ============================================================
#
# The admin API behind ai-ui/src/components/FeatureModelConfig.jsx: assign a
# configured LLM to a platform feature, per org, at runtime.
#
# The catalogue of features is code-seeded (core/feature_registry.py, upserted
# by migrate.py Part AD1), so there is no create/delete for features here —
# only assignment. Resolution is core/feature_model_resolver.py.
#
# Every route requires Depends(require_admin), same as
# routers/llm_provider_admin_router.py: which model serves which feature is
# structurally an admin decision.
#
# VALIDATION IS SERVER-SIDE ON PURPOSE. The UI also filters the dropdown by the
# feature's declared requirements, but UI filtering is bypassable by any API
# client, and the two failure modes it prevents are both silent at runtime:
#   * a text-only model assigned to a vision feature just returns nonsense;
#   * a cloud model assigned to a CONFIDENTIAL feature is silently overridden
#     on-premise by route()'s privacy floor, so the admin believes an
#     assignment took effect that never can.

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from auth.dependencies import require_admin
from core.feature_model_resolver import env_var_for, explain, invalidate_cache
from core.feature_registry import (
    CAPABILITIES, DATA_CLASSIFICATIONS, FEATURES, categories, get_feature,
)
from core.logger import logger
from db.database import SessionLocal
from db.models import FeatureModelConfig, FeatureRegistry

router = APIRouter(prefix="/feature-models", tags=["feature-model-config"])

# Classifications at or above which a feature must stay on-premise. Mirrors
# models/model_router.py's _LOCAL_ONLY_CLASSIFICATIONS — the runtime privacy
# floor — so the UI and the router agree about which features are local-only.
_LOCAL_ONLY_CLASSIFICATIONS = frozenset({"CONFIDENTIAL", "RESTRICTED", "PCI_SENSITIVE"})


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Request bodies ───────────────────────────────────────────────────────────

class AssignmentUpsert(BaseModel):
    """An assignment. Either pin a model, or pin a capability — not both.

    Both being None is valid and means "inherit the platform default", which is
    a distinct, visible state from having no row at all: it records that the
    admin looked at this feature and chose to leave it alone.
    """

    model_id: Optional[str] = None            # llm_models.id (UUID), not the API model_id
    fallback_model_ids: Optional[list[str]] = None
    capability_override: Optional[str] = None
    enabled: Optional[bool] = None
    org_id: str = "default"

    @field_validator("capability_override")
    @classmethod
    def validate_capability(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        v = v.strip().lower()
        if v not in CAPABILITIES:
            raise ValueError(
                f"capability_override must be one of {sorted(CAPABILITIES)}, got {v!r}. "
                f"Vendor tier names are not accepted here — the point of the "
                f"capability vocabulary is that this field names no vendor."
            )
        return v

    @field_validator("org_id")
    @classmethod
    def validate_org(cls, v: str) -> str:
        v = (v or "default").strip()
        return v or "default"


class ClassificationUpdate(BaseModel):
    """The highest data sensitivity a feature is permitted to process."""

    max_data_classification: str

    @field_validator("max_data_classification")
    @classmethod
    def validate_classification(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if v not in DATA_CLASSIFICATIONS:
            raise ValueError(
                f"max_data_classification must be one of "
                f"{list(DATA_CLASSIFICATIONS)}, got {v!r}."
            )
        return v


# ── Serialisation ────────────────────────────────────────────────────────────

def _model_summary(m: Optional[dict]) -> Optional[dict]:
    if not m:
        return None
    return {
        "id": m["id"],
        "model_id": m["model_id"],
        "display_name": m["display_name"],
        "provider_name": m["provider_name"],
        "family": m["family"],
        "is_local": m["family"] == "ollama",
    }


def _assignment_out(cfg: Optional[FeatureModelConfig]) -> Optional[dict]:
    if cfg is None:
        return None
    return {
        "feature_key": cfg.feature_key,
        "org_id": cfg.org_id,
        "model_id": cfg.model_id,
        "fallback_model_ids": list(cfg.fallback_model_ids or []),
        "capability_override": cfg.capability_override,
        "enabled": bool(cfg.enabled),
        "updated_by": cfg.updated_by,
        "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
    }


def _feature_out(row: FeatureRegistry) -> dict:
    spec = get_feature(row.feature_key)
    cls = (row.max_data_classification or "").upper()
    return {
        "feature_key": row.feature_key,
        "display_name": row.display_name,
        "category": row.category,
        "description": row.description,
        "owning_module": row.owning_module,
        "default_capability": row.default_capability,
        "requires_vision": bool(row.requires_vision),
        "requires_tools": bool(row.requires_tools),
        "requires_streaming": bool(row.requires_streaming),
        "min_context_tokens": row.min_context_tokens,
        "max_data_classification": row.max_data_classification,
        # The UI disables cloud models entirely for these, rather than letting
        # an admin assign one that the privacy floor will silently override.
        "local_only": cls in _LOCAL_ONLY_CLASSIFICATIONS,
        # The break-glass env var, shown so an operator can find it during an
        # incident without reading the source.
        "env_var": env_var_for(row.feature_key),
        # True when the DB row has drifted from the code declaration, i.e.
        # migrate.py has not been re-run since a feature was added or changed.
        "declared_in_code": spec is not None,
    }


# ── Capability checks ────────────────────────────────────────────────────────

def _capability_conflicts(feature: FeatureRegistry, model: dict) -> list[str]:
    """Why `model` cannot serve `feature`, or [] if it can.

    Checks are deliberately one-sided: a requirement is only REJECTED when the
    model's capabilities metadata positively says it is unmet. Missing metadata
    passes. `llm_models.capabilities` is populated by discovery and by hand, so
    it is routinely sparse for admin-added models, and refusing every model
    with an incomplete row would make the screen unusable — which is a worse
    outcome than letting an admin make a choice we cannot verify.

    The privacy check below is the exception: it is based on the provider's
    `family`, which is always populated, so it is enforced unconditionally.
    """
    caps = model.get("capabilities") or {}
    problems: list[str] = []

    if feature.requires_vision:
        modality = str(caps.get("modality") or "").lower()
        supports = caps.get("supports_vision")
        if supports is False or (modality and "image" not in modality and "vision" not in modality):
            problems.append(
                f"{feature.feature_key} needs image input, but "
                f"{model['model_id']} is not marked as supporting it"
            )

    if feature.requires_tools and caps.get("supports_tools") is False:
        problems.append(
            f"{feature.feature_key} needs tool calling, but "
            f"{model['model_id']} is marked as not supporting it"
        )

    if feature.min_context_tokens:
        window = caps.get("context_window")
        if isinstance(window, int) and window < feature.min_context_tokens:
            problems.append(
                f"{feature.feature_key} needs at least "
                f"{feature.min_context_tokens} context tokens, but "
                f"{model['model_id']} has {window}"
            )

    if (feature.max_data_classification or "").upper() in _LOCAL_ONLY_CLASSIFICATIONS:
        if model["family"] != "ollama":
            problems.append(
                f"{feature.feature_key} may process "
                f"{feature.max_data_classification} data, so it must use an "
                f"on-premise model. {model['model_id']} is served by "
                f"{model['provider_name']} ({model['family']}). Assigning it "
                f"would have no effect: the router's privacy floor pins these "
                f"turns on-premise regardless."
            )

    return problems


# ── Routes ───────────────────────────────────────────────────────────────────
# Static paths before "/{feature_key}" — FastAPI matches in registration order
# (same comment as llm_provider_admin_router.py / endpoint_mgmt_router.py).

@router.get("/capabilities", summary="The capability vocabulary and UI groupings")
def list_capabilities(admin: dict = Depends(require_admin)):
    return {
        "capabilities": list(CAPABILITIES),
        "categories": list(categories()),
        "classifications": ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED", "PCI_SENSITIVE"],
        "local_only_classifications": sorted(_LOCAL_ONLY_CLASSIFICATIONS),
    }


@router.get("/retrieval", summary="Which models retrieval is using (read-only)")
def get_retrieval_models(
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    """The embedding and reranking models in use, and why neither is a dropdown.

    llm_models.model_kind lets an admin REGISTER embedding and rerank models,
    but neither is assignable at runtime, for two different reasons that the
    response spells out per model (see core/embedding_model.py). Serving them
    read-only is deliberate: an operator needs to know what their vectors were
    built with — that is the thing they must check before ever changing it —
    and offering a control that would corrupt the index would be worse than
    offering none.

    `stored_vectors` reports what is actually in the index, grouped by the
    model that produced it. More than one entry, or any NULL-provenance rows,
    means a reindex is incomplete or predates provenance tracking — which is
    what a cutover has to resolve before search results can be trusted.
    """
    from core.embedding_model import active_embedding_model, describe_retrieval_models

    out = describe_retrieval_models()
    active = active_embedding_model()

    # Registered candidates, so the screen can show what an operator COULD
    # move to (via env + a reindex), not just what is running.
    try:
        from core.llm_provider_registry import get_models_by_kind
        out["registered_candidates"] = {
            kind: [
                {"model_id": m["model_id"], "display_name": m["display_name"],
                 "provider_name": m["provider_name"], "family": m["family"]}
                for m in get_models_by_kind(kind)
            ]
            for kind in ("embedding", "rerank")
        }
    except Exception as exc:
        logger.warning(f"[feature-model-config] retrieval candidates unavailable: {exc}")
        out["registered_candidates"] = {"embedding": [], "rerank": []}

    # What the index actually holds. Counted per table because they have
    # different lifecycles: the answer cache is disposable, the other two are not.
    from sqlalchemy import text as _text

    stored: dict = {}
    for table in ("document_embeddings", "semantic_memory", "semantic_answer_cache"):
        try:
            rows = db.execute(_text(
                f"SELECT COALESCE(embed_model, '(unknown)') AS tag, count(*) AS n "
                f"FROM {table} GROUP BY 1 ORDER BY 2 DESC"
            )).fetchall()
            stored[table] = {r[0]: int(r[1]) for r in rows}
        except Exception as exc:
            # Table or column absent (migrate.py not run), or it lives on the
            # pgvector engine rather than this session's.
            stored[table] = {"(unavailable)": 0}
            logger.debug(f"[feature-model-config] {table} provenance unavailable: {exc}")

    out["stored_vectors"] = stored
    out["active_tag"] = active
    # True when every stored vector was produced by the model now configured.
    out["consistent"] = all(
        set(tags) <= {active} or sum(tags.values()) == 0
        for tags in stored.values()
        if "(unavailable)" not in tags
    )
    return out


@router.post("/sync", summary="Re-seed feature_registry from the code declarations")
def sync_registry(
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    """Bring feature_registry back in step with core/feature_registry.py.

    migrate.py Part AD1 does this on every deploy, but this endpoint lets an
    operator fix a drifted catalogue without a migration run — and, more
    usefully, tells them WHAT drifted. Only the declared columns are touched;
    assignments in feature_model_config are never modified.
    """
    declared = {spec.feature_key: spec for spec in FEATURES}
    existing = {r.feature_key: r for r in db.query(FeatureRegistry).all()}

    added, updated = [], []
    for key, spec in declared.items():
        row = existing.get(key)
        if row is None:
            row = FeatureRegistry(feature_key=key)
            db.add(row)
            added.append(key)
        else:
            before = (row.display_name, row.category, row.default_capability,
                      row.requires_vision, row.requires_tools, row.requires_streaming,
                      row.min_context_tokens, row.max_data_classification)
            after = (spec.display_name, spec.category, spec.default_capability,
                     spec.requires_vision, spec.requires_tools, spec.requires_streaming,
                     spec.min_context_tokens, spec.max_data_classification)
            if before != after:
                updated.append(key)
        row.display_name = spec.display_name
        row.category = spec.category
        row.description = spec.description or None
        row.owning_module = spec.owning_module
        row.default_capability = spec.default_capability
        row.requires_vision = spec.requires_vision
        row.requires_tools = spec.requires_tools
        row.requires_streaming = spec.requires_streaming
        row.min_context_tokens = spec.min_context_tokens
        row.max_data_classification = spec.max_data_classification

    # Rows in the DB that code no longer declares. NOT deleted: a stale row is
    # harmless (nothing resolves against it), while deleting it would CASCADE
    # away the admin's assignment. Reported so an operator can decide.
    orphaned = sorted(set(existing) - set(declared))

    db.commit()
    invalidate_cache()

    logger.info(
        f"[feature-model-config] registry sync: +{len(added)} ~{len(updated)} "
        f"orphaned={len(orphaned)} by {admin.get('email', 'unknown')}"
    )
    return {"added": added, "updated": updated, "orphaned": orphaned,
            "total_declared": len(declared)}


@router.put("/{feature_key}/classification",
            summary="Set the highest data sensitivity a feature may process")
def set_classification(
    feature_key: str,
    body: ClassificationUpdate,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    """Raise or lower a feature's max_data_classification.

    This is the one feature_registry column an operator owns rather than the
    code: requires_vision/requires_tools/min_context_tokens describe what the
    CODE needs, but which data a feature is permitted to see is a policy
    decision specific to a deployment. migrate.py Part AD1 therefore seeds it
    once and COALESCEs on re-seed, so a value set here survives every
    subsequent migration.

    Raising it to CONFIDENTIAL or above makes the feature on-premise-only: the
    router's privacy floor pins those turns to a local model at runtime, and
    this endpoint refuses to leave a cloud model assigned to it — otherwise the
    assignment would look effective while being silently overridden.
    """
    row = db.query(FeatureRegistry).filter_by(feature_key=feature_key).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Feature '{feature_key}' is not registered.")

    previous = row.max_data_classification
    row.max_data_classification = body.max_data_classification

    # If this makes the feature local-only, any cloud model already assigned to
    # it would now be silently overridden at runtime. Surface that instead of
    # leaving a misleading assignment in place.
    stranded: list[str] = []
    if body.max_data_classification in _LOCAL_ONLY_CLASSIFICATIONS:
        from core.llm_provider_registry import get_model_by_uuid
        for cfg in db.query(FeatureModelConfig).filter_by(feature_key=feature_key).all():
            for pk in [cfg.model_id] + list(cfg.fallback_model_ids or []):
                model = get_model_by_uuid(pk) if pk else None
                if model and model["family"] != "ollama":
                    stranded.append(f"{cfg.org_id}:{model['model_id']}")
    if stranded:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"'{feature_key}' has cloud model(s) assigned: {stranded}. "
                f"Setting it to {body.max_data_classification} would pin the "
                f"feature on-premise and silently override them. Clear or "
                f"re-point those assignments first."
            ),
        )

    db.commit()
    db.refresh(row)
    invalidate_cache()

    logger.info(
        f"[feature-model-config] {feature_key} classification "
        f"{previous} → {row.max_data_classification} by {admin.get('email', 'unknown')}"
    )
    return {
        "feature": _feature_out(row),
        "resolved": explain(
            feature_key, default=None,
            data_classification=row.max_data_classification,
        ),
    }


@router.get("", summary="Every feature, its assignment, and what it resolves to")
def list_features(
    org_id: str = "default",
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    """One row per registered feature, with the effective resolution.

    `resolved` carries the hint the router will actually receive plus WHICH
    precedence rule produced it, so an admin can tell "I assigned this" from
    "the privacy floor overrode me" from "nothing is assigned" — the three
    states that otherwise look identical in a dropdown.
    """
    rows = db.query(FeatureRegistry).order_by(
        FeatureRegistry.category, FeatureRegistry.feature_key,
    ).all()

    assignments = {
        (c.feature_key, c.org_id): c
        for c in db.query(FeatureModelConfig).filter(
            FeatureModelConfig.org_id.in_([org_id, "default"])
        ).all()
    }

    from core.llm_provider_registry import get_model_by_uuid

    out = []
    for row in rows:
        cfg = assignments.get((row.feature_key, org_id)) or assignments.get((row.feature_key, "default"))
        item = _feature_out(row)
        item["assignment"] = _assignment_out(cfg)
        item["assigned_model"] = (
            _model_summary(get_model_by_uuid(cfg.model_id)) if cfg and cfg.model_id else None
        )
        spec = get_feature(row.feature_key)
        item["resolved"] = explain(
            row.feature_key,
            default=None,
            org_id=org_id,
            data_classification=row.max_data_classification,
        )
        item["owning_module"] = spec.owning_module if spec else row.owning_module
        out.append(item)

    return {"org_id": org_id, "features": out}


@router.get("/{feature_key}", summary="One feature, with the models eligible for it")
def get_feature_detail(
    feature_key: str,
    org_id: str = "default",
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    """The feature plus the eligible/ineligible split of the live catalogue.

    Both lists are returned rather than only the eligible one: an admin looking
    for a model they expected to see needs to know it was excluded and why,
    otherwise the dropdown looks broken.
    """
    row = db.query(FeatureRegistry).filter_by(feature_key=feature_key).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Feature '{feature_key}' is not registered.")

    from core.llm_provider_registry import get_enabled_models

    eligible, ineligible = [], []
    for m in get_enabled_models():
        problems = _capability_conflicts(row, m)
        summary = _model_summary(m)
        if problems:
            summary["reasons"] = problems
            ineligible.append(summary)
        else:
            eligible.append(summary)

    cfg = (db.query(FeatureModelConfig).filter_by(feature_key=feature_key, org_id=org_id).first()
           or db.query(FeatureModelConfig).filter_by(feature_key=feature_key, org_id="default").first())

    item = _feature_out(row)
    item["assignment"] = _assignment_out(cfg)
    item["resolved"] = explain(
        feature_key, default=None, org_id=org_id,
        data_classification=row.max_data_classification,
    )
    item["eligible_models"] = eligible
    item["ineligible_models"] = ineligible
    return {"feature": item}


@router.put("/{feature_key}", summary="Assign a model (or capability) to a feature")
def upsert_assignment(
    feature_key: str,
    body: AssignmentUpsert,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    row = db.query(FeatureRegistry).filter_by(feature_key=feature_key).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"Feature '{feature_key}' is not registered.")

    if body.model_id and body.capability_override:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Set either model_id or capability_override, not both — "
                   "pinning a model and pinning a capability are alternatives.",
        )

    from core.llm_provider_registry import get_model_by_uuid

    fallbacks = [f for f in (body.fallback_model_ids or []) if f]
    if body.model_id and body.model_id in fallbacks:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The primary model cannot also appear in the fallback chain.",
        )
    if len(set(fallbacks)) != len(fallbacks):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The fallback chain contains the same model more than once.",
        )

    # Validate the primary and every fallback: a fallback that cannot serve the
    # feature is worse than no fallback, because it only fails over to it once
    # the primary is already down.
    for label, pk in [("model_id", body.model_id)] + [
        (f"fallback_model_ids[{i}]", f) for i, f in enumerate(fallbacks)
    ]:
        if not pk:
            continue
        model = get_model_by_uuid(pk)
        if not model:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{label}: no enabled model with id '{pk}'. It may have "
                       f"been deleted, or its provider disabled.",
            )
        problems = _capability_conflicts(row, model)
        if problems:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{label}: " + "; ".join(problems),
            )

    cfg = db.query(FeatureModelConfig).filter_by(
        feature_key=feature_key, org_id=body.org_id,
    ).first()
    if cfg is None:
        cfg = FeatureModelConfig(feature_key=feature_key, org_id=body.org_id)
        db.add(cfg)

    cfg.model_id = body.model_id or None
    cfg.fallback_model_ids = fallbacks
    cfg.capability_override = body.capability_override
    if body.enabled is not None:
        cfg.enabled = body.enabled
    cfg.updated_by = admin.get("email") or admin.get("sub")

    db.commit()
    db.refresh(cfg)
    invalidate_cache()

    logger.info(
        f"[feature-model-config] {feature_key} org={body.org_id} → "
        f"model={body.model_id or '-'} capability={body.capability_override or '-'} "
        f"fallbacks={len(fallbacks)} by {admin.get('email', 'unknown')}"
    )
    return {
        "assignment": _assignment_out(cfg),
        "resolved": explain(
            feature_key, default=None, org_id=body.org_id,
            data_classification=row.max_data_classification,
        ),
    }


@router.delete("/{feature_key}", summary="Clear a feature's assignment")
def delete_assignment(
    feature_key: str,
    org_id: str = "default",
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    """Remove the row entirely, reverting the feature to the platform default.

    Distinct from an assignment with both fields empty, which records a
    deliberate "inherit" decision. Deleting says "I never decided".
    """
    cfg = db.query(FeatureModelConfig).filter_by(
        feature_key=feature_key, org_id=org_id,
    ).first()
    if not cfg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"No assignment for '{feature_key}' in org '{org_id}'.")
    db.delete(cfg)
    db.commit()
    invalidate_cache()

    logger.info(
        f"[feature-model-config] cleared {feature_key} org={org_id} "
        f"by {admin.get('email', 'unknown')}"
    )
    return {"ok": True, "feature_key": feature_key, "org_id": org_id}
