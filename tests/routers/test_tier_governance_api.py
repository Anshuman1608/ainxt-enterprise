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


@pytest.fixture(autouse=True)
def _preserve_tier_assignments():
    """Put the real tier assignments back after every test in this module.

    Most tests here have to WRITE to a tier to prove anything, and PUT is
    whole-list replacement — so a test that assigns its own fixture model to
    `medium` destroys the assignment migration Part AD1 seeded there. The
    damage is invisible: every test still passes, the suite still goes green,
    and the deployment quietly loses a tier that some later run, or some
    later engineer reading the table, has no way to trace back to here.

    Autouse and unconditional rather than opt-in, because remembering to ask
    for it is exactly the step that gets missed — it already was, in the
    Phase 3 tests this fixture was added alongside.

    Uses its own session: it outlives the `db` fixture and must not depend on
    a session another fixture may have closed.
    """
    from db.database import SessionLocal

    snap = SessionLocal()
    try:
        rows = snap.execute(text(
            "SELECT tier, model_id, priority, role, enabled, created_by "
            "FROM llm_tier_models WHERE org_id = 'default'"
        )).all()
    finally:
        snap.close()

    yield

    restore = SessionLocal()
    try:
        restore.execute(text("SET CONSTRAINTS uq_tier_priority DEFERRED"))
        restore.execute(text("DELETE FROM llm_tier_models WHERE org_id = 'default'"))
        for r in rows:
            restore.execute(text(
                "INSERT INTO llm_tier_models (tier, model_id, priority, role, enabled, "
                "org_id, created_by) VALUES (:t, :m, :p, :r, :e, 'default', :by)"
            ), {"t": r[0], "m": str(r[1]), "p": r[2], "r": r[3], "e": r[4], "by": r[5]})
        restore.commit()
    finally:
        restore.close()


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


# ── Phase 4: GET /tiers/{tier}/candidates ───────────────────────────────────
#
# The endpoint exists so the admin screen's "+ Add model" dropdown and the PUT
# guardrail above cannot disagree. The load-bearing test is the last one in
# this block, which asserts that equivalence directly rather than trusting
# that both call the same helper today.


@pytest.fixture
def visible(fixture_models):
    """fixture_models, with the registry cache dropped so they are visible.

    /candidates reads through get_enabled_models(), which is Redis-cached, so
    rows inserted directly by the fixture are invisible until the cache is
    invalidated — exactly as they would be for a real admin write, which is
    why every admin write calls this.
    """
    from core.llm_provider_registry import invalidate_cache
    invalidate_cache()
    yield fixture_models
    invalidate_cache()


def _candidates(client, tier):
    r = client.get(f"{BASE}/{tier}/candidates")
    assert r.status_code == 200, r.text
    return r.json()["candidates"]


def test_candidates_reports_the_tiers_modality_requirement(client):
    r = client.get(f"{BASE}/image-output/candidates")
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "image-output"
    assert body["modality_requirement"] == "image-out"


def test_candidates_rejects_an_unknown_tier(client):
    assert client.get(f"{BASE}/deep/candidates").status_code == 400


def test_candidates_offers_a_text_model_for_a_text_tier(client, visible):
    ids = {c["model_id"] for c in _candidates(client, "medium")}
    assert visible["text"] in ids


def test_candidates_excludes_a_disabled_model(client, visible):
    """A disabled model must never be offered — assigning it is a 422."""
    ids = {c["model_id"] for c in _candidates(client, "medium")}
    assert visible["disabled"] not in ids


def test_candidates_excludes_a_model_of_a_disabled_provider(client, visible, db):
    db.execute(text("UPDATE llm_providers SET enabled = FALSE WHERE id = CAST(:p AS uuid)"),
               {"p": visible["provider_id"]})
    db.commit()
    from core.llm_provider_registry import invalidate_cache
    invalidate_cache()
    try:
        ids = {c["model_id"] for c in _candidates(client, "medium")}
        assert visible["text"] not in ids
    finally:
        db.execute(text("UPDATE llm_providers SET enabled = TRUE WHERE id = CAST(:p AS uuid)"),
                   {"p": visible["provider_id"]})
        db.commit()
        invalidate_cache()


def test_candidates_excludes_a_text_model_from_a_modality_tier(client, visible):
    """The check that matters: a text model must not be offered for video."""
    ids = {c["model_id"] for c in _candidates(client, "video-generation")}
    assert visible["text"] not in ids


def test_candidates_includes_a_capable_model_for_a_modality_tier(client, visible):
    ids = {c["model_id"] for c in _candidates(client, "video-generation")}
    assert visible["video"] in ids


def test_candidates_returns_an_empty_list_rather_than_404(client, monkeypatch):
    """No image model is a normal deployment state, not an error.

    404 would make the screen show a failure where the honest answer is "this
    deployment cannot do image output" — the state §J.2 renders as Unassigned.
    """
    import core.llm_provider_registry as reg
    monkeypatch.setattr(reg, "get_enabled_models", lambda channel=None: [])
    r = client.get(f"{BASE}/image-output/candidates")
    assert r.status_code == 200
    assert r.json()["candidates"] == []


def test_candidates_returns_the_row_uuid_not_the_provider_model_string(client, visible):
    """The two are both called model_id in llm_models; the PUT takes the UUID."""
    c = next(c for c in _candidates(client, "medium") if c["model_id"] == visible["text"])
    assert uuid.UUID(c["model_id"])
    assert c["api_model_id"] != c["model_id"]


def test_candidates_carries_what_the_picker_displays(client, visible):
    c = next(c for c in _candidates(client, "medium") if c["model_id"] == visible["text"])
    assert c["provider_name"].startswith("TierTest")
    assert c["family"] == "openai_compatible"
    assert c["modality"] == ["text"]
    assert c["privacy_class"] == "external"


@pytest.mark.parametrize("tier", ["medium", "video-generation"])
def test_every_candidate_is_accepted_and_every_omission_rejected(
        client, visible, tier):
    """THE D7 INVARIANT, asserted rather than asserted-in-a-comment.

    The dropdown is only trustworthy if the set it offers is exactly the set
    the PUT accepts. Both sides are checked here: each candidate assigns
    cleanly, and each fixture model the endpoint left out is refused with 422.
    """
    offered = {c["model_id"] for c in _candidates(client, tier)}
    ours = {v for k, v in visible.items() if k != "provider_id"}

    for model_id in ours & offered:
        r = _put(client, tier, [{"model_id": model_id, "priority": 1}])
        assert r.status_code == 200, f"{model_id} was offered but rejected: {r.text}"

    for model_id in ours - offered:
        r = _put(client, tier, [{"model_id": model_id, "priority": 1}])
        assert r.status_code == 422, f"{model_id} was omitted but accepted"

    _put(client, tier, [])


# ── Phase 4: governance_active ──────────────────────────────────────────────


def test_governance_is_reported_inactive_by_default(client, monkeypatch):
    """Phases 3 and 4 change no routing, and the screen must say so."""
    monkeypatch.delenv("TIER_GOVERNANCE_ENABLED", raising=False)
    assert client.get(BASE).json()["governance_active"] is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_governance_active_when_the_flag_is_set(client, monkeypatch, raw):
    monkeypatch.setenv("TIER_GOVERNANCE_ENABLED", raw)
    assert client.get(BASE).json()["governance_active"] is True


@pytest.mark.parametrize("raw", ["", "0", "false", "off", "no", "maybe"])
def test_anything_not_clearly_true_reads_as_inactive(client, monkeypatch, raw):
    """Fail safe: an unparseable value must not claim the runtime is live."""
    monkeypatch.setenv("TIER_GOVERNANCE_ENABLED", raw)
    assert client.get(BASE).json()["governance_active"] is False


# ── Phase 4 exit criterion ──────────────────────────────────────────────────


def test_an_assignment_changes_what_the_tier_resolves_to(client, visible):
    """THE PHASE 4 EXIT CRITERION.

    "An admin can populate all 8 tiers with multiple models and see
    /tiers/resolved change immediately" (plan.html §N). The screen's entire
    value rests on that round trip: an admin who corrects an assignment and
    sees no confirmation has no way to tell whether the correction took.

    Uses video-generation because no registry in practice has a video-capable
    model, so the unresolved → resolved → unresolved transition is
    unambiguous — but the tier is cleared explicitly rather than assumed
    empty, so this does not depend on what the seed happened to find.
    """
    def _status():
        r = client.get(f"{BASE}/resolved", params={"tier": "video-generation"})
        assert r.status_code == 200, r.text
        return r.json()["resolved"][0]

    assert _put(client, "video-generation", []).status_code == 200
    assert _status()["status"] == "unresolved"

    assert _put(client, "video-generation",
                [{"model_id": visible["video"], "priority": 1}]).status_code == 200

    after = _status()
    assert after["status"] == "resolved"
    assert after["model_id"].startswith("__tiertest_video_")

    # And clearing it puts the tier back to reporting the feature unavailable,
    # rather than leaving a stale resolution behind.
    assert _put(client, "video-generation", []).status_code == 200
    assert _status()["status"] == "unresolved"


def test_reordering_changes_which_model_resolves(client, visible, db):
    """Priority order is the decision, so reordering must change the answer."""
    second = str(uuid.uuid4())
    db.execute(text(
        "INSERT INTO llm_models (id, provider_id, model_id, display_name, "
        "capabilities, enabled, is_default, sort_order, source) VALUES "
        "(:id, :pid, :mid, 'TierTest text2', "
        "'{\"modality\": [\"text\"], \"privacy_class\": \"external\"}'::jsonb, "
        "TRUE, FALSE, 9999, 'manual')"
    ), {"id": second, "pid": visible["provider_id"], "mid": f"__tiertest_text2_{second[:8]}"})
    db.commit()
    from core.llm_provider_registry import invalidate_cache
    invalidate_cache()

    def _resolved():
        r = client.get(f"{BASE}/resolved", params={"tier": "medium"})
        return r.json()["resolved"][0]

    try:
        _put(client, "medium", [
            {"model_id": visible["text"], "priority": 1},
            {"model_id": second, "priority": 2},
        ])
        first_choice = _resolved()["model_id"]

        _put(client, "medium", [
            {"model_id": second, "priority": 1},
            {"model_id": visible["text"], "priority": 2},
        ])
        assert _resolved()["model_id"] != first_choice
    finally:
        _put(client, "medium", [])
        db.execute(text("DELETE FROM llm_tier_models WHERE model_id = CAST(:m AS uuid)"),
                   {"m": second})
        db.execute(text("DELETE FROM llm_models WHERE id = CAST(:m AS uuid)"), {"m": second})
        db.commit()
        invalidate_cache()
