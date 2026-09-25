# SPDX-License-Identifier: MIT
"""Phase 3 — the tier governance admin API.

The guardrails here are the ones plan.html §J.2 makes server-side promises
about, and each exists because the failure it prevents is silent:

  * A tier cannot be created, renamed or deleted. The vocabulary is fixed in
    code, in the database CHECK, and here.
  * A model whose modality cannot satisfy a tier is rejected at ASSIGNMENT
    time (422), not discovered at resolve time. Assigning a text model to
    image-output otherwise produces a confident wrong answer months later.
  * Two models cannot share a priority slot within a tier, because "which of
    these two is first" would then be undefined.

The route-shadowing test is the load-bearing one: model_governance_router
declares `GET /{dept}` under the same /model-governance prefix, so if these
handlers are ever moved into that module or included after it, `GET /tiers`
starts returning department permissions and every test above it still passes.

Rows are created against a uuid-suffixed throwaway provider and removed in
fixture teardown regardless of outcome, so this never touches real config.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

import routers.tier_governance_router as TG
from db.database import SessionLocal

BASE = "/model-governance/tiers"


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client():
    """Both routers, in gateway.py's order, so the shadowing test is real."""
    from routers.model_governance_router import router as mg

    app = FastAPI()
    app.include_router(TG.router)
    app.include_router(mg)
    app.dependency_overrides[TG._require_admin] = lambda: {
        "sub": "test-admin", "email": "admin@test.invalid",
    }
    return TestClient(app)


@pytest.fixture
def fixture_models(db):
    """A throwaway provider with three models of differing modality."""
    suffix = uuid.uuid4().hex[:8]
    pid = str(uuid.uuid4())
    db.execute(text(
        "INSERT INTO llm_providers (id, name, slug, family, enabled, extra_config) "
        "VALUES (:id, :name, :slug, 'openai_compatible', TRUE, '{}'::jsonb)"
    ), {"id": pid, "name": f"TierTest {suffix}", "slug": f"__tiertest_{suffix}"})

    made = {}
    specs = [
        ("text", ["text"], True),
        ("video", ["text", "video-out"], True),
        ("disabled", ["text"], False),
    ]
    for name, modality, enabled in specs:
        mid = str(uuid.uuid4())
        db.execute(text(
            "INSERT INTO llm_models (id, provider_id, model_id, display_name, "
            "capabilities, enabled, is_default, sort_order, source) "
            "VALUES (:id, :pid, :mid, :dn, CAST(:caps AS jsonb), :en, FALSE, 9999, 'manual')"
        ), {
            "id": mid, "pid": pid, "mid": f"__tiertest_{name}_{suffix}",
            "dn": f"TierTest {name}", "en": enabled,
            "caps": '{"modality": %s, "privacy_class": "external"}' % (
                str(modality).replace("'", '"')),
        })
        made[name] = mid
    db.commit()

    yield {"provider_id": pid, **made}

    db.execute(text("DELETE FROM llm_tier_models WHERE model_id = ANY(CAST(:ids AS uuid[]))"),
               {"ids": list(made.values())})
    db.execute(text("DELETE FROM llm_models WHERE provider_id = CAST(:p AS uuid)"), {"p": pid})
    db.execute(text("DELETE FROM llm_providers WHERE id = CAST(:p AS uuid)"), {"p": pid})
    db.commit()


def _put(client, tier, models):
    return client.put(f"{BASE}/{tier}/models", json={"models": models})


# ── Route shadowing ─────────────────────────────────────────────────────────


def test_get_tiers_is_not_swallowed_by_the_dept_route(client):
    """model_governance_router's GET /{dept} would answer this as the
    department named "tiers" if the include order were ever reversed."""
    body = client.get(BASE).json()
    assert "tiers" in body, f"served by the wrong route: {body}"
    assert len(body["tiers"]) == 8


# ── The eight tiers are fixed ───────────────────────────────────────────────


def test_always_exactly_eight_tiers_including_unassigned(client):
    from core.tiers import ALL_TIERS

    tiers = client.get(BASE).json()["tiers"]
    assert [t["tier"] for t in tiers] == [t.value for t in ALL_TIERS]
    for t in tiers:
        assert t["status"] in ("active", "unassigned")
        assert t["label"] and t["modality_requirement"] and t["used_by"]


def test_tiers_cannot_be_created(client):
    assert client.post(BASE, json={}).status_code == 405


def test_tiers_cannot_be_deleted(client):
    """405 with an explanation, NOT the 200 that DELETE /{dept}/{model_id}
    would otherwise return for "the department 'tiers'"."""
    r = client.delete(f"{BASE}/mini")
    assert r.status_code == 405
    assert "cannot be deleted" in r.json()["detail"]


def test_tier_model_rows_cannot_be_deleted_individually(client):
    assert client.delete(f"{BASE}/mini/models/{uuid.uuid4()}").status_code == 405


@pytest.mark.parametrize("bad", ["deep", "solution", "local", "vision", "Mini", ""])
def test_unknown_tier_is_400(client, bad):
    assert _put(client, bad or "%20", []).status_code in (400, 404)


def test_400_names_the_eight_tiers(client):
    detail = _put(client, "deep", []).json()["detail"]
    assert "intent-classification" in detail and "video-generation" in detail


# ── Assignment guardrails (422) ─────────────────────────────────────────────


def test_disabled_model_cannot_be_assigned(client, fixture_models):
    r = _put(client, "medium", [{"model_id": fixture_models["disabled"], "priority": 5}])
    assert r.status_code == 422 and "disabled" in r.json()["detail"]


def test_modality_mismatch_is_rejected(client, fixture_models):
    """The check that matters most — without it the failure surfaces much
    later as a text model asked to generate a video."""
    r = _put(client, "video-generation",
             [{"model_id": fixture_models["text"], "priority": 5}])
    assert r.status_code == 422
    assert "video-out" in r.json()["detail"]


def test_matching_modality_is_accepted(client, fixture_models):
    assert _put(client, "video-generation",
                [{"model_id": fixture_models["video"], "priority": 5}]).status_code == 200


def test_duplicate_priority_is_rejected(client, fixture_models):
    r = _put(client, "medium", [
        {"model_id": fixture_models["text"], "priority": 7},
        {"model_id": fixture_models["video"], "priority": 7},
    ])
    assert r.status_code == 422 and "duplicate priority" in r.json()["detail"]


def test_same_model_listed_twice_is_rejected(client, fixture_models):
    r = _put(client, "medium", [
        {"model_id": fixture_models["text"], "priority": 7},
        {"model_id": fixture_models["text"], "priority": 8},
    ])
    assert r.status_code == 422


def test_unknown_model_is_rejected(client):
    r = _put(client, "medium", [{"model_id": str(uuid.uuid4()), "priority": 7}])
    assert r.status_code == 422 and "unknown model" in r.json()["detail"]


def test_api_model_string_instead_of_row_uuid_is_a_clean_422(client):
    """llm_models has BOTH an `id` UUID and a `model_id` string, so passing
    the latter is the likely mistake. It must not reach Postgres as
    `uuid = text` and come back a 500."""
    r = _put(client, "medium", [{"model_id": "claude-sonnet-5", "priority": 7}])
    assert r.status_code == 422


# ── Replacement semantics ───────────────────────────────────────────────────


def test_put_replaces_the_whole_list_and_is_ordered(client, fixture_models):
    _put(client, "medium", [
        {"model_id": fixture_models["text"], "priority": 11},
        {"model_id": fixture_models["video"], "priority": 12},
    ])
    got = _tier(client, "medium")
    ours = [m for m in got["models"] if m["model_id"] in fixture_models.values()]
    assert [m["priority"] for m in ours] == [11, 12]


def test_reordering_is_legal_in_one_transaction(client, fixture_models):
    """Delete-then-insert of a swapped list transiently duplicates a priority.
    This is what uq_tier_priority DEFERRABLE exists for."""
    a, b = fixture_models["text"], fixture_models["video"]
    assert _put(client, "medium", [{"model_id": a, "priority": 21},
                                   {"model_id": b, "priority": 22}]).status_code == 200
    assert _put(client, "medium", [{"model_id": a, "priority": 22},
                                   {"model_id": b, "priority": 21}]).status_code == 200
    ours = {m["model_id"]: m["priority"]
            for m in _tier(client, "medium")["models"]
            if m["model_id"] in (a, b)}
    assert ours == {a: 22, b: 21}


def test_put_is_idempotent(client, fixture_models):
    payload = [{"model_id": fixture_models["text"], "priority": 31}]
    first = _put(client, "medium", payload).json()
    assert _put(client, "medium", payload).json() == first


def test_empty_list_makes_a_tier_unassigned(client, fixture_models):
    _put(client, "video-generation",
         [{"model_id": fixture_models["video"], "priority": 5}])
    assert _tier(client, "video-generation")["status"] == "active"
    assert _put(client, "video-generation", []).status_code == 200
    assert _tier(client, "video-generation")["status"] == "unassigned"


def test_role_is_persisted(client, fixture_models):
    _put(client, "medium", [{"model_id": fixture_models["text"],
                             "priority": 41, "role": "review"}])
    ours = [m for m in _tier(client, "medium")["models"]
            if m["model_id"] == fixture_models["text"]]
    assert ours and ours[0]["role"] == "review"


def test_write_invalidates_the_resolver_cache(client, fixture_models, monkeypatch):
    calls: list = []
    monkeypatch.setattr("core.tier_resolver.invalidate_tier_cache",
                        lambda: calls.append(1))
    _put(client, "medium", [{"model_id": fixture_models["text"], "priority": 51}])
    assert calls, "a stale cache would leave workers routing to the old model"


def test_a_disabled_model_still_shows_in_the_admin_list(client, fixture_models, db):
    """It occupies a priority slot, so hiding it would confuse the admin."""
    _put(client, "medium", [{"model_id": fixture_models["text"], "priority": 61}])
    db.execute(text("UPDATE llm_models SET enabled = FALSE WHERE id = CAST(:i AS uuid)"),
               {"i": fixture_models["text"]})
    db.commit()
    ours = [m for m in _tier(client, "medium")["models"]
            if m["model_id"] == fixture_models["text"]]
    assert ours and ours[0]["model_enabled"] is False


# ── /tiers/resolved ─────────────────────────────────────────────────────────


def test_resolved_reports_all_eight(client):
    body = client.get(f"{BASE}/resolved").json()
    assert len(body["resolved"]) == 8


def test_resolved_reports_unresolved_rather_than_500(client):
    """An unassigned tier is a normal state and the whole point of this
    endpoint is to show it."""
    entries = client.get(f"{BASE}/resolved").json()["resolved"]
    for e in entries:
        assert e["status"] in ("resolved", "unresolved")
        if e["status"] == "unresolved":
            assert e["reason"]


def test_resolved_accepts_a_single_tier(client):
    body = client.get(f"{BASE}/resolved", params={"tier": "complex"}).json()
    assert len(body["resolved"]) == 1 and body["resolved"][0]["tier"] == "complex"


def test_resolved_rejects_an_unknown_tier(client):
    assert client.get(f"{BASE}/resolved", params={"tier": "deep"}).status_code == 400


def test_resolved_echoes_the_constraints(client):
    body = client.get(f"{BASE}/resolved", params={"no_cloud_egress": True}).json()
    assert body["constraints"]["no_cloud_egress"] is True


def _tier(client, name: str) -> dict:
    return next(t for t in client.get(BASE).json()["tiers"] if t["tier"] == name)
