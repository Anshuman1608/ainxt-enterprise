# SPDX-License-Identifier: MIT
"""Phase 3 — llm_tier_models invariants and the Part AD1 seed.

The schema carries three invariants that the API relies on and therefore must
hold even if a future caller bypasses the API entirely:

  * No INSERT can invent a ninth tier. The vocabulary is fixed in code AND in
    the database, and the CHECK is generated from core.tiers.ALL_TIERS so the
    two cannot drift apart.
  * Two models cannot occupy one priority slot in a tier — but a whole-list
    reorder inside one transaction must still be legal, which is why the
    constraint is DEFERRABLE and not merely UNIQUE.
  * Deleting a model removes its tier memberships rather than leaving a
    dangling reference.

And the seed carries two:

  * Idempotent — a second run changes nothing.
  * Never overwrites an operator's assignment. Seeding is a heuristic over a
    registry whose tier_tags are usually empty; the operator's choice is data,
    the seed's choice is a guess.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from core.tiers import ALL_TIERS
from db.database import SessionLocal


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def scratch_model(db):
    """A throwaway enabled model to hang tier rows off."""
    suffix = uuid.uuid4().hex[:8]
    pid, mid = str(uuid.uuid4()), str(uuid.uuid4())
    db.execute(text(
        "INSERT INTO llm_providers (id, name, slug, family, enabled, extra_config) "
        "VALUES (CAST(:id AS uuid), :n, :s, 'openai_compatible', TRUE, '{}'::jsonb)"
    ), {"id": pid, "n": f"SeedTest {suffix}", "s": f"__seedtest_{suffix}"})
    db.execute(text(
        "INSERT INTO llm_models (id, provider_id, model_id, display_name, "
        "capabilities, enabled, is_default, sort_order, source) VALUES "
        "(CAST(:id AS uuid), CAST(:p AS uuid), :m, 'Seed Test', "
        "'{\"modality\": [\"text\"]}'::jsonb, TRUE, FALSE, 9999, 'manual')"
    ), {"id": mid, "p": pid, "m": f"__seedtest_{suffix}"})
    db.commit()

    yield mid

    db.rollback()
    db.execute(text("DELETE FROM llm_tier_models WHERE model_id = CAST(:i AS uuid)"), {"i": mid})
    db.execute(text("DELETE FROM llm_models WHERE provider_id = CAST(:p AS uuid)"), {"p": pid})
    db.execute(text("DELETE FROM llm_providers WHERE id = CAST(:p AS uuid)"), {"p": pid})
    db.commit()


def _insert(db, tier: str, model_id: str, priority: int = 500, org: str = "default"):
    db.execute(text(
        "INSERT INTO llm_tier_models (tier, model_id, priority, org_id) "
        "VALUES (:t, CAST(:m AS uuid), :p, :o)"
    ), {"t": tier, "m": model_id, "p": priority, "o": org})


# ── The tier vocabulary is a database invariant ─────────────────────────────


@pytest.mark.parametrize("tier", [t.value for t in ALL_TIERS])
def test_every_approved_tier_is_accepted(db, scratch_model, tier):
    _insert(db, tier, scratch_model, priority=900)
    db.rollback()


@pytest.mark.parametrize("bad", ["deep", "solution", "local", "local-mini",
                                 "vision", "Mini", "MINI", "", "simple "])
def test_a_ninth_tier_cannot_be_inserted(db, scratch_model, bad):
    """The CHECK, not the API, is what makes this impossible."""
    with pytest.raises(Exception) as exc:
        _insert(db, bad, scratch_model)
        db.flush()
    assert "ck_llm_tier_models_tier" in str(exc.value) or "check constraint" in str(exc.value).lower()
    db.rollback()


def test_the_check_constraint_matches_core_tiers_exactly(db):
    """Generated from ALL_TIERS at migration time; if someone edits the enum
    without re-running the migration, this catches the drift."""
    src = db.execute(text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_llm_tier_models_tier'"
    )).scalar()
    assert src, "ck_llm_tier_models_tier is missing — Part AD1 did not run"
    for t in ALL_TIERS:
        assert f"'{t.value}'" in src
    assert src.count("::text") == len(ALL_TIERS) or src.count(",") == len(ALL_TIERS) - 1


# ── Priority slots ──────────────────────────────────────────────────────────


def test_two_models_cannot_share_a_priority_in_one_tier(db, scratch_model):
    other = str(uuid.uuid4())
    db.execute(text(
        "INSERT INTO llm_models (id, provider_id, model_id, display_name, capabilities, "
        "enabled, is_default, sort_order, source) SELECT CAST(:i AS uuid), provider_id, "
        "'__seedtest_dup', 'dup', capabilities, TRUE, FALSE, 9999, 'manual' "
        "FROM llm_models WHERE id = CAST(:m AS uuid)"
    ), {"i": other, "m": scratch_model})
    _insert(db, "medium", scratch_model, priority=901)
    _insert(db, "medium", other, priority=901)
    with pytest.raises(Exception):
        db.commit()          # deferred: fires at COMMIT, not at INSERT
    db.rollback()


def test_the_same_priority_is_fine_in_different_tiers(db, scratch_model):
    _insert(db, "medium", scratch_model, priority=902)
    _insert(db, "complex", scratch_model, priority=902)
    db.flush()
    db.rollback()


def test_reorder_in_one_transaction_is_legal(db, scratch_model):
    """A straight UNIQUE would reject this mid-transaction. DEFERRABLE is the
    reason PUT /tiers/{tier}/models can replace a whole ordered list."""
    other = str(uuid.uuid4())
    db.execute(text(
        "INSERT INTO llm_models (id, provider_id, model_id, display_name, capabilities, "
        "enabled, is_default, sort_order, source) SELECT CAST(:i AS uuid), provider_id, "
        "'__seedtest_swap', 'swap', capabilities, TRUE, FALSE, 9999, 'manual' "
        "FROM llm_models WHERE id = CAST(:m AS uuid)"
    ), {"i": other, "m": scratch_model})
    _insert(db, "mini", scratch_model, priority=910)
    _insert(db, "mini", other, priority=911)
    db.commit()

    db.execute(text("SET CONSTRAINTS uq_tier_priority DEFERRED"))
    db.execute(text("DELETE FROM llm_tier_models WHERE tier='mini' "
                    "AND model_id IN (CAST(:a AS uuid), CAST(:b AS uuid))"),
               {"a": scratch_model, "b": other})
    _insert(db, "mini", scratch_model, priority=911)
    _insert(db, "mini", other, priority=910)
    db.commit()

    got = dict(db.execute(text(
        "SELECT model_id::text, priority FROM llm_tier_models WHERE tier='mini' "
        "AND model_id IN (CAST(:a AS uuid), CAST(:b AS uuid))"),
        {"a": scratch_model, "b": other}).all())
    assert got == {scratch_model: 911, other: 910}

    db.execute(text("DELETE FROM llm_tier_models WHERE model_id = CAST(:i AS uuid)"), {"i": other})
    db.execute(text("DELETE FROM llm_models WHERE id = CAST(:i AS uuid)"), {"i": other})
    db.commit()


def test_a_model_cannot_be_listed_twice_in_one_tier(db, scratch_model):
    _insert(db, "medium", scratch_model, priority=920)
    # The PK (tier, model_id, org_id) is NOT deferrable, unlike uq_tier_priority,
    # so this raises on the statement itself rather than at flush/commit.
    with pytest.raises(Exception):
        _insert(db, "medium", scratch_model, priority=921)
        db.flush()
    db.rollback()


# ── Referential integrity ───────────────────────────────────────────────────


def test_deleting_a_model_removes_its_tier_memberships(db, scratch_model):
    """ON DELETE CASCADE: the tier survives with its remaining candidates
    rather than keeping a dangling reference."""
    _insert(db, "medium", scratch_model, priority=930)
    db.commit()
    db.execute(text("DELETE FROM llm_models WHERE id = CAST(:i AS uuid)"), {"i": scratch_model})
    db.commit()
    left = db.execute(text(
        "SELECT count(*) FROM llm_tier_models WHERE model_id = CAST(:i AS uuid)"),
        {"i": scratch_model}).scalar()
    assert left == 0


def test_a_tier_row_cannot_reference_a_nonexistent_model(db):
    with pytest.raises(Exception):
        _insert(db, "medium", str(uuid.uuid4()))
        db.flush()
    db.rollback()


# ── §L.5 audit columns ──────────────────────────────────────────────────────


def test_audit_columns_exist_and_are_nullable(db):
    rows = dict(db.execute(text(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'model_usages' "
        "AND column_name IN ('selection_mode', 'requested_tier')"
    )).all())
    assert rows == {"selection_mode": "YES", "requested_tier": "YES"}, (
        "both must be nullable — they are not backfilled, because for a request "
        "that predates tier governance the answer is genuinely unknown"
    )


# ── The Part AD1 seed ───────────────────────────────────────────────────────


def _snapshot(db) -> set:
    return set(db.execute(text(
        "SELECT tier, model_id::text, priority FROM llm_tier_models WHERE org_id='default'"
    )).all())


def test_seed_is_idempotent(db):
    """A second run changes nothing.

    Seeded once first so the property under test is "re-running is a no-op",
    not "the database happened to be full already" — otherwise this passes or
    fails depending on what ran before it.
    """
    from db.migrate import _part_ad1_tier_models_2026_09_25

    _part_ad1_tier_models_2026_09_25()
    db.rollback()
    before = _snapshot(db)

    _part_ad1_tier_models_2026_09_25()
    db.rollback()
    assert _snapshot(db) == before


def test_seed_never_overwrites_an_existing_assignment(db, scratch_model):
    """The operator's choice is data; the seed's is a guess over a registry
    whose tier_tags are usually empty."""
    from db.migrate import _part_ad1_tier_models_2026_09_25

    # Save and restore: this test deletes real seeded rows, and leaving 'mini'
    # unassigned would silently change the starting state of every later test.
    original = db.execute(text(
        "SELECT model_id::text, priority, role FROM llm_tier_models "
        "WHERE tier = 'mini' AND org_id = 'default'"
    )).all()
    db.execute(text("DELETE FROM llm_tier_models WHERE tier = 'mini' AND org_id='default'"))
    _insert(db, "mini", scratch_model, priority=1)
    db.commit()

    try:
        _part_ad1_tier_models_2026_09_25()
        db.rollback()

        rows = db.execute(text(
            "SELECT model_id::text, priority FROM llm_tier_models WHERE tier='mini'"
        )).all()
        assert rows == [(scratch_model, 1)], "the seed replaced an operator assignment"
    finally:
        db.rollback()
        db.execute(text("DELETE FROM llm_tier_models WHERE tier = 'mini' AND org_id='default'"))
        for model_id, priority, role in original:
            db.execute(text(
                "INSERT INTO llm_tier_models (tier, model_id, priority, role, org_id) "
                "VALUES ('mini', CAST(:m AS uuid), :p, :r, 'default')"
            ), {"m": model_id, "p": priority, "r": role})
        db.commit()


def test_seed_never_assigns_a_model_that_cannot_serve_the_tier(db):
    """The same modality check the API enforces, so the seed can never write
    a row that PUT would 422 on."""
    from core.tier_resolver import modality_of
    from core.tiers import MODALITY_REQUIREMENT, Tier

    rows = db.execute(text(
        "SELECT t.tier, m.model_id, m.capabilities FROM llm_tier_models t "
        "JOIN llm_models m ON m.id = t.model_id"
    )).all()
    for tier, model_id, caps in rows:
        need = MODALITY_REQUIREMENT[Tier(tier)]
        assert need in modality_of(caps), (
            f"{model_id} is assigned to '{tier}' but its modality "
            f"{modality_of(caps)} cannot satisfy '{need}'"
        )


def test_seed_never_assigns_a_blocked_model(db):
    from core.model_registry import BLOCKED_MODELS

    assigned = {r[0] for r in db.execute(text(
        "SELECT m.model_id FROM llm_tier_models t JOIN llm_models m ON m.id = t.model_id"
    )).all()}
    assert not (assigned & set(BLOCKED_MODELS))


# ── Phase 3 exit criterion: zero drift from today's routing ────────────────


def test_resolved_tiers_match_todays_effective_resolution(db):
    """The Phase 3 exit criterion (plan.html §N), asserted rather than eyeballed.

    Wherever the legacy chain still names a usable model, `resolve_tier()`
    must return that SAME model — so when Phase 5 inverts the precedence,
    upgrading a deployment changes nothing. This is the guarantee Phase 5
    inherits, which is why it is a test and not a one-off check.

    Where the legacy chain names nothing — the normal case for a family with
    no configured provider — there is nothing to drift from, and the seed's
    registry-default fallback (text tiers) or `unassigned` (modality tiers)
    applies instead. Those cases are covered by the seed tests above.
    """
    import core.model_registry as reg
    from core.tier_resolver import NoEligibleModel, modality_of, resolve_tier
    from core.tiers import MODALITY_REQUIREMENT, Tier

    legacy = {
        Tier.MINI: (reg.OPENAI_SIMPLE_MODEL, "openai", "simple"),
        Tier.SIMPLE: (reg.CLAUDE_HAIKU, "anthropic", "haiku"),
        Tier.INTENT_CLASSIFICATION: (reg.CLAUDE_HAIKU, "anthropic", "haiku"),
        Tier.MEDIUM: (reg.OPENAI_CODING_MODEL, "openai", "medium"),
        Tier.COMPLEX: (reg.CLAUDE_PRIMARY_MODEL, "anthropic", "complex"),
        Tier.IMAGE_INPUT: (reg.GEMINI_IMAGE_MODEL, "gemini", "vision"),
        Tier.IMAGE_OUTPUT: (reg.GEMINI_IMAGE_MODEL, "gemini", "image-gen"),
    }

    caps_by_model = dict(db.execute(text(
        "SELECT m.model_id, m.capabilities FROM llm_models m "
        "JOIN llm_providers p ON p.id = m.provider_id "
        "WHERE m.enabled = TRUE AND p.enabled = TRUE"
    )).all())

    compared = 0
    for tier, (env_value, family, tag) in legacy.items():
        want = (reg._role_model(env_value, family, tag) or "").strip()
        if not want or want not in caps_by_model:
            continue                       # nothing to drift from
        if MODALITY_REQUIREMENT[tier] not in modality_of(caps_by_model[want]):
            continue                       # legacy pick cannot serve this tier
        try:
            got = resolve_tier(tier).model_id
        except NoEligibleModel as exc:      # pragma: no cover - drift
            pytest.fail(f"tier '{tier.value}' resolves to nothing but the legacy "
                        f"chain still names '{want}': {exc}")
        assert got == want, (
            f"DRIFT on tier '{tier.value}': legacy routing uses '{want}' but "
            f"resolve_tier() returns '{got}'. Phase 5 would change behaviour "
            f"on upgrade."
        )
        compared += 1

    assert compared > 0, (
        "no tier could be compared — the registry has no model the legacy "
        "chain resolves to, so this test proved nothing"
    )
