# SPDX-License-Identifier: MIT
# ============================================================
# ACTIVE EMBEDDING MODEL — identity and provenance
# ============================================================
#
# Which embedding model this deployment's vectors were produced by.
#
# WHY THIS IS NOT A DROPDOWN
#   The provider registry now carries embedding and rerank models
#   (llm_models.model_kind), so an admin can REGISTER them — but neither is
#   assignable at runtime the way a generation model is, and for two different
#   architectural reasons:
#
#   * Embedding: every stored vector was produced by one specific model, and
#     vectors from a different model are not comparable with it even at the
#     same width (core.config.EMBED_DIM). Changing it requires re-embedding
#     every indexed chunk, so it is a migration, not a setting.
#   * Reranking: services/embed_svc/reranker.py loads a HuggingFace
#     CrossEncoder at IMPORT time and warms it up, constrained to an
#     allowlist. Switching it means loading a different multi-hundred-MB model
#     into the embed service's memory — a service restart, not a config flip.
#     (The architecture audit's §4.8 claim that "reranking has no such
#     constraint and can be switched live" assumed an API-style model; it does
#     not hold for a locally-loaded CrossEncoder.)
#
#   So both stay deployment-time settings, configured by env var. What was
#   genuinely missing is not assignability but PROVENANCE and VISIBILITY:
#
# WHAT WAS MISSING
#   document_embeddings has 31 columns and not one of them recorded which
#   model produced the vector. Neither did semantic_memory or
#   semantic_answer_cache. That is the actual blocker for ever changing the
#   embedding model safely: a reindex cannot target what it cannot identify,
#   and a half-finished reindex leaves two models' vectors in one column with
#   no way to tell them apart — silently wrong search results rather than an
#   error. The `embed_model` column those three tables now carry is written
#   from here, so every vector says what made it.

from __future__ import annotations

from core.logger import logger

# Recorded verbatim in <table>.embed_model. "<provider>:<model>" rather than
# just the model id, because the same model name served by Ollama and by an
# OpenAI-compatible endpoint can produce different vectors, and because
# `provider` alone is what the /embed API takes.
#
# Treat the result as an OPAQUE tag, compared only for equality. Model names
# can themselves contain a colon ("nomic-embed-text:latest" gives the tag
# "ollama:nomic-embed-text:latest"), so it does not round-trip into parts by
# splitting on ":" — and it does not need to. The only question asked of it is
# "was this row produced by the model running now?".
_UNKNOWN = "unknown"


def active_embedding_model() -> str:
    """The "<provider>:<model>" tag for vectors written right now.

    Derived from the same core.config constants the two /embed callers already
    share (models/hybrid_search.py for query-time, workers/index_worker.py for
    index-time), so the tag cannot disagree with the vectors it labels.

    Never raises and never returns empty: an unresolvable configuration yields
    "<provider>:unknown", which is still more useful than NULL — it records
    that the provider was known and the model was not.
    """
    try:
        from core.config import EMBED_PROVIDER
        provider = (EMBED_PROVIDER or "").strip().lower() or _UNKNOWN
    except Exception as exc:  # noqa: BLE001 — provenance must not break indexing
        logger.warning(f"[embedding_model] provider lookup failed: {exc}")
        return f"{_UNKNOWN}:{_UNKNOWN}"

    try:
        model = _model_for_provider(provider)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[embedding_model] model lookup failed for {provider!r}: {exc}")
        model = _UNKNOWN

    return f"{provider}:{model or _UNKNOWN}"


def _model_for_provider(provider: str) -> str:
    """The model id the embed service will use for `provider`.

    Mirrors services/embed_svc/config.py, which is the process that actually
    calls the provider. Read from the environment rather than imported from
    that module because the embed service is a separate deployable and its
    config module is not importable from the gateway in every layout.
    """
    import os

    if provider == "openai":
        return os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
    if provider == "nomic":
        return os.getenv("NOMIC_EMBED_MODEL", "nomic-embed-text-v1.5")
    # "ollama" and anything else the embed service treats as the default branch.
    return os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text:latest")


def describe_retrieval_models() -> dict:
    """Read-only description of what retrieval is using, for the admin API.

    `changeable` is False for both with the reason attached, so the UI can show
    the operator what their vectors were built with — the thing they must know
    before ever changing it — without offering a control that would corrupt the
    index.
    """
    import os

    from core.config import EMBED_DIM

    provider = ""
    try:
        from core.config import EMBED_PROVIDER
        provider = EMBED_PROVIDER
    except Exception:  # noqa: BLE001
        pass

    return {
        "embedding": {
            "provider": provider or _UNKNOWN,
            "model": _model_for_provider((provider or "").strip().lower()),
            "tag": active_embedding_model(),
            "dimensions": EMBED_DIM,
            "configured_by": "EMBED_PROVIDER + OLLAMA_EMBED_MODEL / "
                             "OPENAI_EMBED_MODEL / NOMIC_EMBED_MODEL",
            "changeable": False,
            "reason": (
                "Every stored vector was produced by this model and vectors "
                "from a different model are not comparable with it, even at "
                "the same width. Changing it requires re-embedding every "
                "indexed chunk, so it is a migration rather than a setting."
            ),
        },
        "rerank": {
            "model": os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-large"),
            "variant": os.getenv("RERANKER_VARIANT", "") or None,
            "configured_by": "RERANKER_MODEL / RERANKER_VARIANT",
            "changeable": False,
            "reason": (
                "The embed service loads this CrossEncoder at import time and "
                "warms it up. Switching it means loading a different "
                "multi-hundred-MB model into that service's memory, so it "
                "takes a restart rather than a config change."
            ),
        },
    }
