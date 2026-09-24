# SPDX-License-Identifier: MIT
# ============================================================
# EMBED SERVICE — configuration
# ============================================================
import os

EMBED_SVC_PORT   = int(os.getenv("EMBED_SVC_PORT", "8001"))
# docker-compose.yml passes OLLAMA_URL explicitly to
# this container (defaulting to the Docker service name "ollama", not
# localhost); a bare-metal/dev run should set it in services/embed_svc/.env
# (see .env.example at the repo root for the documented quickstart value).
OLLAMA_URL       = os.getenv("OLLAMA_URL", "")
OLLAMA_MODEL     = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text:latest")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL     = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")

# ── Stored vector width ──────────────────────────────────────────────────────
# Sent to OpenAI as the `dimensions:` parameter, which asks the API to truncate
# its native output (1536 for text-embedding-3-small, 3072 for -3-large) to
# this width. That is not a preference: it has to equal the width of the
# pgvector columns the results are stored in, or Postgres rejects the insert.
#
# Derived from core.config.EMBED_DIM rather than restated here, because it was
# one of five independent 768 literals across db/migrate.py's DDL,
# db/models.py's Vector(768), and this file that happened to agree. Falls back
# to a local 768 only when core/ is not importable (this service can be run
# with services/embed_svc/ as its root).
#
# Changing it is a REINDEX, not a config change — see EMBED_DIM's comment.
try:
    from core.config import EMBED_DIM as _PLATFORM_EMBED_DIM
except Exception:      # noqa: BLE001 — standalone run without core/ on the path
    _PLATFORM_EMBED_DIM = 768
OPENAI_DIMS      = _PLATFORM_EMBED_DIM
EMBED_DIM        = _PLATFORM_EMBED_DIM

# ── Nomic Embed (custom / any OpenAI-compatible embeddings endpoint) ─────────
# Set NOMIC_EMBED_URL to point at a self-hosted or remote embedding gateway
# instead of local Ollama.  The endpoint must accept the OpenAI /v1/embeddings
# request format:
#   POST <NOMIC_EMBED_URL>/v1/embeddings
#   Authorization: Bearer <NOMIC_EMBED_API_KEY>
#   { "model": "<NOMIC_EMBED_MODEL>", "input": ["text1", "text2", ...] }
#
# Example:
#   NOMIC_EMBED_URL=https://<YOUR_EMBEDDING_SERVICE_URL>/nomicembed
#   NOMIC_EMBED_API_KEY=12345
#   NOMIC_EMBED_MODEL=nomic-embed-text-v1.5
#
# NOMIC_EMBED_DIMS — expected vector dimension.  nomic-embed-text-v1.5 returns
#   768-dim vectors.  Used only for the startup probe dimension check.
NOMIC_EMBED_URL     = os.getenv("NOMIC_EMBED_URL", "").rstrip("/")
NOMIC_EMBED_API_KEY = os.getenv("NOMIC_EMBED_API_KEY", "")
NOMIC_EMBED_MODEL   = os.getenv("NOMIC_EMBED_MODEL", "nomic-embed-text-v1.5")
# Native output width of NOMIC_EMBED_MODEL, used only by the startup probe to
# verify the configured endpoint actually returns vectors the platform can
# store. Defaults to EMBED_DIM because a value that differs from it cannot be
# persisted — the probe failing loudly at startup is the point.
NOMIC_EMBED_DIMS    = int(os.getenv("NOMIC_EMBED_DIMS", str(_PLATFORM_EMBED_DIM)))
NOMIC_EMBED_TIMEOUT = float(os.getenv("NOMIC_EMBED_TIMEOUT", "60.0"))
# Batch size for Nomic calls — some gateways may have a lower limit than OpenAI.
# Default 64 matches Ollama batch size; lower if the gateway returns 413/429.
NOMIC_EMBED_BATCH   = int(os.getenv("NOMIC_EMBED_BATCH", "64"))

# same env var/convention as core.config.REDIS_HOST.
REDIS_HOST       = os.getenv("REDIS_HOST", "")
REDIS_PORT       = int(os.getenv("REDIS_PORT", "6379"))
REDIS_EMBED_DB   = 7          # dedicated DB for embedding cache
EMBED_CACHE_TTL  = 3600       # 1 hour

BATCH_SIZE       = 64         # max texts per Ollama call
BATCH_WAIT_MS    = 50         # ms to wait before firing partial batch
QUEUE_MAXSIZE    = 1000       # back-pressure limit per provider
OLLAMA_TIMEOUT   = 120.0      # seconds
OPENAI_TIMEOUT   = 30.0
# ── Multi-instance Ollama pool ────────────────────────────────────────────────
# Set OLLAMA_URLS to a comma-separated list of Ollama base URLs to spread load
# across multiple instances running on different ports (or different servers).
#
# Example (3 instances on same Ubuntu server):
#   OLLAMA_URLS=http://localhost:11434,http://localhost:11435,http://localhost:11436
#
# Fallback: if OLLAMA_URLS is not set, uses OLLAMA_URL (single instance, no
# hardcoded localhost default — see above).
# OLLAMA_WORKERS auto-defaults to number of instances so each instance gets
# one dedicated sub-batch per accumulation cycle.
_raw_ollama_urls = os.getenv("OLLAMA_URLS", OLLAMA_URL).split(",")
OLLAMA_URLS    = [u.strip().rstrip("/") for u in _raw_ollama_urls if u.strip()]
OLLAMA_URL     = OLLAMA_URLS[0]   # backward-compat alias (health checks, logs)
OLLAMA_WORKERS = int(os.getenv("OLLAMA_WORKERS", str(len(OLLAMA_URLS))))

# ── Document Parse Service (Docling + PaddleOCR) ─────────────────────────────
# When PARSE_SVC_ENABLED=1, the /parse endpoint is active and Docling/PaddleOCR
# models are loaded at startup. This offloads heavy ML parsing from the gateway
# process to the embed server — exactly mirroring how embedding was offloaded.
#
# On the embed server's .env (services/embed_svc/.env):
#   PARSE_SVC_ENABLED=1
#   USE_DOCLING_PARSER=1
#   DOCLING_ARTIFACTS_PATH=/abs/path/to/docling-models
#   PADDLEOCR_MODELS_PATH=/abs/path/to/paddleocr_models
#     (structure: paddleocr_models/det/, paddleocr_models/rec/, paddleocr_models/cls/)
#     (leave empty to allow PaddleOCR to auto-download PP-OCRv4 models on first use)
#
# On the gateway's .env:
#   PARSE_SVC_URL=http://<embed-server>:9002
#   (remove USE_DOCLING_PARSER / DOCLING_ARTIFACTS_PATH / PADDLEOCR_MODELS_PATH)
PARSE_SVC_ENABLED      = os.getenv("PARSE_SVC_ENABLED", "0").strip().lower() in ("1", "true", "on", "yes")
USE_DOCLING_PARSER     = os.getenv("USE_DOCLING_PARSER", "0")   # "1" | "shadow" | "0"
DOCLING_ARTIFACTS_PATH = os.getenv("DOCLING_ARTIFACTS_PATH", "")
PADDLEOCR_MODELS_PATH  = os.getenv("PADDLEOCR_MODELS_PATH", "")
PARSE_TIMEOUT          = float(os.getenv("PARSE_TIMEOUT", "270.0"))  # seconds per parse call
# PARSE_TIMEOUT must be LESS than the gateway's PARSE_SVC_TIMEOUT (default 300s)
# so the embed service finishes and returns a response before the gateway gives up.
# Rule: embed PARSE_TIMEOUT < gateway PARSE_SVC_TIMEOUT
#   embed  = 270s (default)
#   gateway = 300s (default)  ← always keep 30s gap minimum

