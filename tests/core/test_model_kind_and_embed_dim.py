# SPDX-License-Identifier: MIT
# ============================================================
# llm_models.model_kind + the stored-embedding-width invariant.
#
# Two things, both about retrieval being as configurable as generation:
#
#   1. model_kind brings embedding and reranking models INTO the provider
#      registry. Until it existed the registry covered generation only, so an
#      admin could swap the chat model from a dropdown but had to edit
#      services/embed_svc/.env and restart a container to change the embedding
#      model — "model-agnostic" was not true of retrieval.
#
#      The risk this introduces is leakage: every existing picker reads
#      get_enabled_models(), so an embedding model added to the registry would
#      appear in the chat dropdown unless that function filters. It defaults to
#      kind="generation" for exactly that reason, and the tests below pin it.
#
#   2. EMBED_DIM. The audit called services/embed_svc/config.py's
#      `OPENAI_DIMS = 768` a hardcoded value that should become a per-model
#      property. It is not: 768 is the width of the pgvector COLUMNS the
#      results are stored in, repeated as five independent literals across
#      db/migrate.py's DDL, db/models.py's Vector(768) and the embed service.
#      Postgres rejects a vector of the wrong width, and a pgvector index built
#      at one width cannot serve queries at another — so it is a schema
#      invariant, and the fix is to state it once, not to make it configurable.
# ============================================================

from __future__ import annotations

import pytest


# ── the stored-width invariant ───────────────────────────────────────────────

def test_embed_dim_is_the_single_source_of_the_stored_width() -> None:
    from core.config import EMBED_DIM

    assert isinstance(EMBED_DIM, int) and EMBED_DIM > 0


def test_the_orm_vector_column_derives_from_embed_dim() -> None:
    import db.models as m
    from core.config import EMBED_DIM

    if not m._PGVECTOR_AVAILABLE:
        pytest.skip("pgvector not installed — the Text fallback has no width")
    assert m._VECTOR_TYPE.dim == EMBED_DIM


def test_the_migration_ddl_matches_embed_dim() -> None:
    import pathlib
    import re

    from core.config import EMBED_DIM

    # Every `vector(N)` in the migration must be the platform width. A mismatch
    # here means some table stores vectors the embedder cannot produce.
    src = pathlib.Path("db/migrate.py").read_text()
    widths = {int(w) for w in re.findall(r"vector\((\d+)\)", src, re.I)}
    assert widths, "no vector(N) DDL found — this check would pass trivially"
    assert widths == {EMBED_DIM}, (
        f"db/migrate.py declares vector columns at {sorted(widths)} but "
        f"core.config.EMBED_DIM is {EMBED_DIM}"
    )


def test_the_embed_service_truncates_to_embed_dim(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import EMBED_DIM

    # OPENAI_DIMS is sent as OpenAI's `dimensions:` parameter, which truncates
    # its native output (1536 for -3-small, 3072 for -3-large) to this width.
    # If it ever diverged from EMBED_DIM the insert would fail.
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
    import importlib

    cfg = importlib.reload(importlib.import_module("services.embed_svc.config"))
    assert cfg.OPENAI_DIMS == EMBED_DIM
    assert cfg.EMBED_DIM == EMBED_DIM
    assert cfg.NOMIC_EMBED_DIMS == EMBED_DIM


# ── model_kind ───────────────────────────────────────────────────────────────

def test_model_kind_defaults_to_generation_on_the_orm() -> None:
    from db.models import LLMModel

    col = LLMModel.__table__.columns["model_kind"]
    assert col.default.arg == "generation"
    assert col.nullable is False


def test_the_admin_api_accepts_only_the_three_kinds() -> None:
    from routers.llm_provider_admin_router import MODEL_KINDS, ModelCreate

    assert MODEL_KINDS == ("generation", "embedding", "rerank")
    assert ModelCreate(model_id="m", display_name="M").model_kind == "generation"
    for kind in MODEL_KINDS:
        assert ModelCreate(model_id="m", display_name="M", model_kind=kind).model_kind == kind
    # Case and whitespace are normalised; anything else is refused.
    assert ModelCreate(model_id="m", display_name="M",
                       model_kind=" EMBEDDING ").model_kind == "embedding"
    for bad in ("nonsense", "embeddings", "generate", "chat"):
        with pytest.raises(Exception):
            ModelCreate(model_id="m", display_name="M", model_kind=bad)


def test_the_admin_kinds_match_the_migration_check_constraint() -> None:
    import pathlib
    import re

    from routers.llm_provider_admin_router import MODEL_KINDS

    src = pathlib.Path("db/migrate.py").read_text()
    m = re.search(r"CHECK \(model_kind IN \(([^)]+)\)\)", src)
    assert m, "no model_kind CHECK constraint found in the migration"
    sql_kinds = tuple(v.strip().strip("'") for v in m.group(1).split(","))
    # A value the API accepts but the CHECK rejects is a 500 on save.
    assert set(sql_kinds) == set(MODEL_KINDS)


# ── leakage: the reason get_enabled_models has a default ─────────────────────

def _rows() -> list[dict]:
    def row(mid, kind):
        return {
            "id": f"uuid-{mid}", "model_id": mid, "display_name": mid,
            "capabilities": {}, "model_kind": kind, "is_default": False,
            "sort_order": 0, "provider_id": "p", "provider_slug": "p",
            "provider_name": "P", "family": "openai", "base_url": None,
        }
    return [row("gen-model", "generation"),
            row("embed-model", "embedding"),
            row("rerank-model", "rerank")]


@pytest.fixture
def stub_registry(monkeypatch: pytest.MonkeyPatch):
    import core.llm_provider_registry as reg
    monkeypatch.setattr(reg, "_read_cache", lambda: _rows())
    return reg


def test_generation_is_the_default_so_pickers_are_unaffected(stub_registry) -> None:
    # Every pre-existing caller uses the no-argument or channel-only form and
    # means "models I can send a prompt to". Without this default, registering
    # an embedding model would put it in the chat dropdown.
    ids = [m["model_id"] for m in stub_registry.get_enabled_models()]
    assert ids == ["gen-model"]


@pytest.mark.parametrize("kind, expected", [
    ("generation", ["gen-model"]),
    ("embedding",  ["embed-model"]),
    ("rerank",     ["rerank-model"]),
])
def test_get_models_by_kind_selects_one_kind(
    kind: str, expected: list[str], stub_registry,
) -> None:
    ids = [m["model_id"] for m in stub_registry.get_models_by_kind(kind)]
    assert ids == expected


def test_kind_none_returns_every_kind(stub_registry) -> None:
    ids = sorted(m["model_id"] for m in stub_registry.get_enabled_models(kind=None))
    assert ids == ["embed-model", "gen-model", "rerank-model"]


def test_a_row_predating_the_migration_counts_as_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.llm_provider_registry as reg

    # A process running this code against a database that has not had Part AD4
    # applied yet gets rows with no model_kind at all. Those must remain
    # visible to the generation pickers, not silently vanish from them.
    legacy = _rows()[0]
    legacy.pop("model_kind")
    monkeypatch.setattr(reg, "_read_cache", lambda: [legacy])
    assert [m["model_id"] for m in reg.get_enabled_models()] == ["gen-model"]


def test_model_lookups_are_generation_scoped(stub_registry) -> None:
    # get_model / get_model_by_uuid feed route() step 1a and the feature
    # resolver, both of which send prompts — so an embedding model must not
    # resolve through them.
    assert stub_registry.get_model("gen-model") is not None
    assert stub_registry.get_model("embed-model") is None
    assert stub_registry.get_model_by_uuid("uuid-gen-model") is not None
    assert stub_registry.get_model_by_uuid("uuid-embed-model") is None


def test_cli_style_catalogue_exposes_the_limit_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AgentStudio's modelMaxTokens.js reads these instead of its static table.
    import core.llm_provider_registry as reg

    rows = _rows()
    rows[0]["capabilities"] = {"context_window": 200000, "max_output_tokens": 8192}
    monkeypatch.setattr(reg, "_read_cache", lambda: rows)

    entry = reg.get_cli_style_models()[0]
    assert entry["context_window"] == 200000
    assert entry["max_output_tokens"] == 8192


def test_cli_style_falls_back_to_reserved_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `reserved_output` is the key some existing capability rows already use.
    import core.llm_provider_registry as reg

    rows = _rows()
    rows[0]["capabilities"] = {"reserved_output": 4096}
    monkeypatch.setattr(reg, "_read_cache", lambda: rows)

    assert reg.get_cli_style_models()[0]["max_output_tokens"] == 4096


def test_cli_style_limits_are_none_when_unrecorded(stub_registry) -> None:
    # None, never 0 — a caller must be able to tell "no limit recorded" from
    # "a limit of zero" and fall back to its own default.
    entry = stub_registry.get_cli_style_models()[0]
    assert entry["context_window"] is None
    assert entry["max_output_tokens"] is None
