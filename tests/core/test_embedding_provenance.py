# SPDX-License-Identifier: MIT
# ============================================================
# Embedding provenance — core/embedding_model.py.
#
# document_embeddings has 31 columns and not one of them recorded which model
# produced the vector; neither did semantic_memory or semantic_answer_cache.
# That, not assignability, is the blocker for ever changing the embedding
# model: a reindex cannot target what it cannot identify, and a half-finished
# one leaves two models' vectors in a single column with no way to tell them
# apart — silently wrong search results rather than an error.
#
# The architecture audit asked for embedding and rerank to become admin
# dropdowns. Neither can be, and for two DIFFERENT reasons, both recorded on
# describe_retrieval_models():
#
#   * Embedding: vectors from a different model are not comparable even at the
#     same width, so a change is a reindex, not a setting.
#   * Reranking: the audit said this one "has no such constraint and can be
#     switched live". That assumed an API-style model. services/embed_svc/
#     reranker.py loads a HuggingFace CrossEncoder at IMPORT time and warms it
#     up, so switching it means loading a different multi-hundred-MB model into
#     that service's memory — a restart.
# ============================================================

from __future__ import annotations

import pytest

from core.embedding_model import active_embedding_model, describe_retrieval_models


@pytest.fixture(autouse=True)
def clean_embed_env(monkeypatch: pytest.MonkeyPatch):
    for var in ("OLLAMA_EMBED_MODEL", "OPENAI_EMBED_MODEL", "NOMIC_EMBED_MODEL",
                "RERANKER_MODEL", "RERANKER_VARIANT"):
        monkeypatch.delenv(var, raising=False)


def _provider(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    import core.config as cfg
    monkeypatch.setattr(cfg, "EMBED_PROVIDER", name)


# ── the tag ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("provider, env, model, expected_default", [
    ("ollama", "OLLAMA_EMBED_MODEL", "my-embedder", "nomic-embed-text:latest"),
    ("openai", "OPENAI_EMBED_MODEL", "text-embedding-3-large", "text-embedding-3-small"),
    ("nomic",  "NOMIC_EMBED_MODEL",  "nomic-embed-text-v2",    "nomic-embed-text-v1.5"),
])
def test_the_tag_tracks_the_configured_provider_and_model(
    provider: str, env: str, model: str, expected_default: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _provider(monkeypatch, provider)

    # Default, when the deployment has not overridden the model.
    assert active_embedding_model() == f"{provider}:{expected_default}"

    # And the override is what gets recorded against the vectors.
    monkeypatch.setenv(env, model)
    assert active_embedding_model() == f"{provider}:{model}"


def test_the_provider_is_part_of_the_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    # The same model name served by Ollama and by an OpenAI-compatible endpoint
    # does not necessarily produce the same vectors, so the provider has to be
    # part of the identity — not just the model id.
    monkeypatch.setenv("OLLAMA_EMBED_MODEL", "shared-name")
    monkeypatch.setenv("OPENAI_EMBED_MODEL", "shared-name")
    _provider(monkeypatch, "ollama")
    a = active_embedding_model()
    _provider(monkeypatch, "openai")
    b = active_embedding_model()
    assert a != b


def test_the_tag_is_opaque_not_parseable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Model names can contain a colon, so the tag does not round-trip by
    # splitting on ":". It is only ever compared for equality.
    _provider(monkeypatch, "ollama")
    monkeypatch.setenv("OLLAMA_EMBED_MODEL", "nomic-embed-text:latest")
    assert active_embedding_model() == "ollama:nomic-embed-text:latest"


def test_the_tag_is_never_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # A NULL column means "unknown provenance" and a reindex must re-embed it.
    # An empty STRING would be indistinguishable from a real tag, so this
    # always returns something, even unconfigured.
    _provider(monkeypatch, "")
    tag = active_embedding_model()
    assert tag and ":" in tag


def test_the_tag_survives_a_broken_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.config as cfg

    class _Boom:
        def __get__(self, *a):
            raise RuntimeError("config unreadable")

    # Provenance must never be the reason indexing fails.
    monkeypatch.delattr(cfg, "EMBED_PROVIDER", raising=False)
    assert active_embedding_model()


# ── the read-only description ────────────────────────────────────────────────

def test_neither_retrieval_model_is_advertised_as_changeable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _provider(monkeypatch, "ollama")
    d = describe_retrieval_models()

    for kind in ("embedding", "rerank"):
        assert d[kind]["changeable"] is False, kind
        # The UI shows this to the operator, so it has to say WHY — otherwise
        # a read-only field looks like a missing feature.
        assert d[kind]["reason"], kind
        assert d[kind]["configured_by"], kind


def test_the_description_reports_the_stored_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.config import EMBED_DIM

    _provider(monkeypatch, "ollama")
    assert describe_retrieval_models()["embedding"]["dimensions"] == EMBED_DIM


def test_the_reranker_default_matches_the_embed_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pathlib
    import re

    _provider(monkeypatch, "ollama")
    reported = describe_retrieval_models()["rerank"]["model"]

    # A drifted default here would tell an operator their reranker is something
    # other than what the embed service actually loaded.
    src = pathlib.Path("services/embed_svc/reranker.py").read_text()
    actual = re.search(r'_DEFAULT_MODEL\s*=\s*"([^"]+)"', src).group(1)
    assert reported == actual


def test_the_description_reflects_a_reranker_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _provider(monkeypatch, "ollama")
    monkeypatch.setenv("RERANKER_MODEL", "BAAI/bge-reranker-base")
    monkeypatch.setenv("RERANKER_VARIANT", "tinybert")
    d = describe_retrieval_models()["rerank"]
    assert d["model"] == "BAAI/bge-reranker-base"
    assert d["variant"] == "tinybert"


# ── the column the tag is written to ─────────────────────────────────────────

def test_the_migration_covers_every_vector_table() -> None:
    """Part AD5 must add embed_model to all three tables that hold vectors.

    Asserted against the loop the migration actually uses rather than against
    literal SQL per table — it adds the column via an f-string over a tuple of
    table names, so the table names never appear adjacent to "ALTER TABLE".
    A table missing from that tuple would hold vectors with no provenance.
    """
    import pathlib
    import re

    src = pathlib.Path("db/migrate.py").read_text()
    body = re.search(
        r"def _part_ad5_embedding_provenance_2026_09_24\(\):(.*?)\n\ndef ",
        src, re.S,
    )
    assert body, "Part AD5 not found"
    body = body.group(1)

    assert "ADD COLUMN IF NOT EXISTS" in body
    assert "embed_model VARCHAR(128)" in body
    for table in ("document_embeddings", "semantic_memory", "semantic_answer_cache"):
        assert table in body, f"{table} is not covered by Part AD5"


def test_the_vector_tables_are_the_ones_the_orm_declares() -> None:
    """The three tables above must be every table with a vector column.

    A fourth vector table added later without provenance would be invisible to
    a reindex, so this fails rather than letting the list drift.
    """
    import db.models as m

    if not m._PGVECTOR_AVAILABLE:
        pytest.skip("pgvector not installed — no Vector columns to enumerate")

    from pgvector.sqlalchemy import Vector

    vector_tables = {
        t.name for t in m.Base.metadata.tables.values()
        if any(isinstance(c.type, Vector) for c in t.columns)
    }
    # semantic_memory / semantic_answer_cache have no ORM class (raw SQL in
    # store/semantic_cache.py), so only document_embeddings shows up here.
    assert vector_tables <= {
        "document_embeddings", "semantic_memory", "semantic_answer_cache",
    }, f"vector table(s) without provenance coverage: {sorted(vector_tables)}"


def test_the_orm_column_is_nullable_with_no_default() -> None:
    from db.models import DocumentEmbedding

    col = DocumentEmbedding.__table__.columns["embed_model"]
    # Rows written before the column existed have genuinely unknown
    # provenance. Defaulting them to the current configuration would assert
    # something untrue — exactly the claim a reindex must not rely on.
    assert col.nullable is True
    assert col.default is None
    assert col.server_default is None


@pytest.mark.parametrize("path, table", [
    ("workers/index_worker.py", "document_embeddings"),
    ("store/semantic_cache.py", "semantic_memory"),
    ("store/semantic_cache.py", "semantic_answer_cache"),
])
def test_every_insert_path_stamps_the_column(path: str, table: str) -> None:
    import pathlib
    import re

    src = pathlib.Path(path).read_text()
    stmt = re.search(
        rf"INSERT INTO (?:ainxt\.)?{table}\b(.*?)(?:ON CONFLICT|\"\"\")",
        src, re.S,
    )
    assert stmt, f"no INSERT INTO {table} found in {path}"
    body = stmt.group(1)
    assert "embed_model" in body, (
        f"{path}'s INSERT INTO {table} does not stamp embed_model, so its "
        f"vectors would have unknown provenance"
    )
