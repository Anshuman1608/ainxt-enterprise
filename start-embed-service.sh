#!/usr/bin/env bash
#
# AiNxt embed-svc starter — standalone, re-runnable at any time.
#
#   bash ./start-embed-service.sh
#
# Builds/starts the "embed-svc" and "kb-worker" containers (Knowledge Base
# document parsing, embedding, and reranking — see docs/KB_SETUP.md) and
# waits for embed-svc to report healthy.
#
# This is the embed-svc bring-up logic extracted out of kb-setup.sh's
# interactive questionnaire, so it can be (re)run on its own — by
# kb-setup.sh right after it writes your model choices to .env, by
# start-index-worker.sh (index-worker needs embed-svc reachable), or by
# hand any time you just want to (re)start embed-svc without answering the
# KB questionnaire again. It reads its configuration back out of .env
# (EMBED_PROVIDER, USE_DOCLING_PARSER, EMBED_SVC_PORT) rather than asking —
# run ./kb-setup.sh first if none of that is set yet.
#
# Deliberately non-interactive (no TTY requirement): this script is meant to
# be called from other scripts as well as run directly.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# ── Colors / helpers (mirrors kb-setup.sh/install.sh) ────────────────────────
if [[ -t 1 ]]; then
  B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[0m'
  GRN=$'\033[32m'; YLW=$'\033[33m'; RED=$'\033[31m'; CYN=$'\033[36m'
else
  B=""; DIM=""; R=""; GRN=""; YLW=""; RED=""; CYN=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s%s\n' "$CYN" "$R" "$B" "$*" "$R"; }
ok()   { printf '  %s✓%s %s\n' "$GRN" "$R" "$*"; }
warn() { printf '  %s!%s %s\n' "$YLW" "$R" "$*"; }
die()  { printf '\n  %s✗ %s%s\n\n' "$RED" "$*" "$R" >&2; exit 1; }

[[ -f .env ]] || die \
"No .env found in $(pwd).
  Run ./install.sh first — it creates .env and starts the base app."

command -v docker >/dev/null 2>&1 || die "Docker is required but was not found."
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "Docker Compose was not found. Install Docker Desktop, or the 'docker-compose-plugin' package."
fi

# ── Read existing configuration back out of .env ────────────────────────────
_env_get() {
  grep -E "^${1}=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true
}

# Mirrors kb-setup.sh's set_env — needed here too so that whichever script
# brings embed-svc up FIRST (this one, called directly by
# start-index-worker.sh, or kb-setup.sh's own questionnaire) leaves the SAME
# explicit values in .env. Without this, a direct ./start-index-worker.sh run
# (skipping kb-setup.sh's questionnaire entirely) left EMBED_PROVIDER/
# USE_DOCLING_PARSER unset in .env — this script only held ollama/"" as
# in-memory defaults for its OWN download-step decisions, never wrote them
# back. kb-setup.sh run afterwards then had nothing in .env to detect, so it
# re-asked from scratch as if nothing had been configured, and — worse —
# docker-compose.yml's OWN default for USE_DOCLING_PARSER is "1" (enabled),
# so the container was already running WITH Docling on while this script's
# unset-means-skip logic below never downloaded its models for it.
set_env() {
  local k="$1" v="$2"
  if grep -qE "^${k}=" .env; then
    # encoding='utf-8' explicitly: Python's open() otherwise defaults to the
    # OS locale encoding, which on Windows is usually cp1252/cp437, not
    # UTF-8 — a .env with any non-ASCII byte (e.g. a pasted key or comment)
    # would then fail to decode, or get silently mis-encoded on write.
    python3 - "$k" "$v" <<'PY'
import re, sys
k, v = sys.argv[1], sys.argv[2]
s = open('.env', encoding='utf-8').read()
s = re.sub(rf'^{re.escape(k)}=.*$', f'{k}={v}', s, flags=re.M)
open('.env', 'w', encoding='utf-8').write(s)
PY
  else
    printf '%s=%s\n' "$k" "$v" >> .env
  fi
}

EMBED_PROVIDER="$(_env_get EMBED_PROVIDER)"
EMBED_PROVIDER="${EMBED_PROVIDER:-ollama}"
set_env EMBED_PROVIDER "$EMBED_PROVIDER"
# Must match docker-compose.yml's own `${USE_DOCLING_PARSER:-1}` default —
# otherwise a run that reaches embed-svc through this script (rather than
# kb-setup.sh's questionnaire, which always writes an explicit 0 or 1) leaves
# .env silently disagreeing with what the container actually started with.
USE_DOCLING_PARSER="$(_env_get USE_DOCLING_PARSER)"
USE_DOCLING_PARSER="${USE_DOCLING_PARSER:-1}"
set_env USE_DOCLING_PARSER "$USE_DOCLING_PARSER"
EMBED_SVC_PORT="$(_env_get EMBED_SVC_PORT)"
EMBED_SVC_PORT="${EMBED_SVC_PORT:-8001}"
set_env EMBED_SVC_PORT "$EMBED_SVC_PORT"

# install.sh's use_container_service_names() points OLLAMA_URL at
# host.docker.internal when it detected a host-installed Ollama already
# answering on 11434 (host_ollama_usable()) — the SAME test the gateway
# itself uses — and otherwise at the bundled `ollama` compose service.
# embed-svc's own runtime calls (services/embed_svc/embedder.py) already
# correctly follow whichever of those OLLAMA_URL ends up as. The model-pull
# step below has to follow the SAME target: pulling into the bundled
# ainxt-ollama container while embed-svc is actually configured to call a
# host Ollama downloads the model into an instance nothing talks to, and
# every embed request 404s ("model not found") against the host Ollama that
# never got it — confirmed via live testing.
OLLAMA_URL_VAL="$(_env_get OLLAMA_URL)"
OLLAMA_PORT_VAL="$(_env_get OLLAMA_PORT)"; OLLAMA_PORT_VAL="${OLLAMA_PORT_VAL:-11434}"
USING_HOST_OLLAMA=0
[[ "$OLLAMA_URL_VAL" == *"host.docker.internal"* ]] && USING_HOST_OLLAMA=1

_ollama_has_model() {
  local model="$1"
  if [[ "$USING_HOST_OLLAMA" == "1" ]]; then
    if command -v ollama >/dev/null 2>&1; then
      ollama list 2>/dev/null | grep -q "^${model}"
    else
      curl -fsS -m 5 "http://127.0.0.1:${OLLAMA_PORT_VAL}/api/tags" 2>/dev/null | grep -q "\"${model}"
    fi
  else
    docker exec ainxt-ollama ollama list 2>/dev/null | grep -q "^${model}"
  fi
}

_ollama_pull() {
  local model="$1"
  if [[ "$USING_HOST_OLLAMA" == "1" ]]; then
    if command -v ollama >/dev/null 2>&1; then
      ollama pull "$model" >/dev/null 2>&1
    else
      # No CLI on PATH (e.g. Ollama.app without the ollama binary installed)
      # — same daemon, reached via its own HTTP API instead. stream:false so
      # this call blocks until the pull finishes rather than returning NDJSON
      # progress chunks we'd otherwise have to parse.
      curl -fsS -m 600 -X POST "http://127.0.0.1:${OLLAMA_PORT_VAL}/api/pull" \
        -H 'Content-Type: application/json' \
        -d "{\"name\":\"${model}\",\"stream\":false}" >/dev/null 2>&1
    fi
  else
    docker exec ainxt-ollama ollama pull "$model" >/dev/null 2>&1
  fi
}

# The bundled `ollama` compose service is only added to the list — and so
# only built/pulled/started — when it's actually going to be used: embed-svc
# is configured for Ollama embeddings AND no host-installed Ollama is
# already serving OLLAMA_URL. docker-compose.yml's embed-svc no longer
# declares `depends_on: [ollama]` for exactly this reason — that hard
# dependency used to force Compose to pull ollama/ollama:latest and start
# the bundled container on every `--profile embed up`, even with a host
# Ollama already running and even when EMBED_PROVIDER wasn't "ollama" at
# all. Confirmed via live testing: a fresh `docker compose down -v` followed
# by Knowledge Base setup re-pulled the image despite `ollama --version`
# already working locally.
up_services=(embed-svc kb-worker)
if [[ "$EMBED_PROVIDER" == "ollama" && "$USING_HOST_OLLAMA" != "1" ]]; then
  up_services+=(ollama)
fi

step "Starting the Knowledge Base model server (embed-svc)"
say "  ${DIM}First build downloads the chosen models — this can take several minutes.${R}"
if ! "${COMPOSE[@]}" --profile embed up -d --build "${up_services[@]}"; then
  die "embed-svc failed to start. Retry later with: ${COMPOSE[*]} --profile embed up -d --build ${up_services[*]}"
fi
ok "embed-svc and kb-worker started"

if [[ "$EMBED_PROVIDER" == "ollama" ]]; then
  # Downloaded models persist (in the host's own Ollama storage, or the
  # ollama_data volume for the bundled container) across restarts and across
  # which script started things first — checking for the model already being
  # present (rather than always re-pulling) means a second run reports the
  # true state instead of printing "downloading" every time.
  if _ollama_has_model nomic-embed-text; then
    ok "nomic-embed-text already downloaded"
  else
    if [[ "$USING_HOST_OLLAMA" == "1" ]]; then
      step "Downloading nomic-embed-text (~280MB) into your host Ollama"
    else
      step "Downloading nomic-embed-text (~280MB)"
    fi
    if _ollama_pull nomic-embed-text; then
      ok "nomic-embed-text ready"
    else
      if [[ "$USING_HOST_OLLAMA" == "1" ]]; then
        warn "pull failed — run 'ollama pull nomic-embed-text' on this machine later"
      else
        warn "pull failed — run 'docker exec ainxt-ollama ollama pull nomic-embed-text' later"
      fi
    fi
  fi
fi

if [[ "$USE_DOCLING_PARSER" == "1" ]]; then
  # embed_models_cache is a persistent volume (see docker-compose.yml) —
  # models downloaded by an earlier run/entry-point are still there. Checking
  # for that first avoids re-announcing a ~150MB download that already
  # happened, no matter which script triggered it originally.
  # MSYS_NO_PATHCONV: under Git Bash on Windows, the literal /home/appuser/...
  # path embedded in this argument gets rewritten to a Windows path before it
  # reaches docker.exe, so the check would always report "not downloaded"
  # even when it is. Harmless on Linux/macOS/WSL2 — see doctor.sh for the
  # same pattern against /opt/kafka/....
  if MSYS_NO_PATHCONV=1 docker exec ainxt-embed-svc sh -c '[ -d /home/appuser/.cache/docling/models ] && [ -n "$(ls -A /home/appuser/.cache/docling/models 2>/dev/null)" ]' >/dev/null 2>&1; then
    ok "Docling parsing models already downloaded"
  else
    step "Downloading Docling parsing models (layout, table, OCR — ~150MB)"
    docker exec ainxt-embed-svc docling-tools models download >/dev/null 2>&1 \
      && ok "Docling models ready" \
      || warn "download failed — run 'docker exec ainxt-embed-svc docling-tools models download' later"
  fi
fi

step "Waiting for embed-svc to become healthy"
waited=0; limit=300
until curl -fsS -m 5 "http://localhost:${EMBED_SVC_PORT}/health" >/dev/null 2>&1; do
  if (( waited >= limit )); then
    die "embed-svc did not become healthy in ${limit}s — check: ${COMPOSE[*]} logs embed-svc"
  fi
  sleep 5; waited=$((waited+5))
done
ok "embed-svc healthy after ${waited}s"
