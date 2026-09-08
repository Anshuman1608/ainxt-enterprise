# AiNxt Enterprise — Quick Reference

This page is for engineers who already have AiNxt running and need quick answers. If you're setting up for the first time, start with [`docs/GETTING_STARTED.md`](GETTING_STARTED.md).

---

## Everyday commands

| Command | What it does |
|---|---|
| `docker compose logs -f gateway` | Follow the API logs |
| `docker compose logs -f kafka-consumer` | Follow the event → database writer |
| `docker compose ps` | See what is running |
| `docker compose down` | Stop everything, keep your data |
| `docker compose down -v` | Stop and **delete all data** |
| `docker compose up -d --build` | Rebuild after pulling changes |
| `docker compose run --rm gateway python db/migrate.py` | Run database migrations on their own |
| `./doctor.sh` | Re-check the install and print what is broken |
| `./doctor.sh --json` | Machine-readable output, same exit status |

---

## After changing configuration

When you edit `.env` or any config, only the affected service needs to restart — you do not need to rebuild everything.

| What you changed | Command |
|---|---|
| A value in `.env` (API keys, gateway config, etc.) | `docker compose up -d gateway` |
| `docker-compose.yml` or a Dockerfile | `docker compose up -d --build` |
| Restart everything without rebuilding | `docker compose restart` |
| Check everything is healthy after the change | `./doctor.sh` |

**Which service to restart for common changes:**

| Change | Restart |
|---|---|
| `KAFKA_ENABLED`, `KAFKA_BOOTSTRAP` | `docker compose up -d gateway kafka-consumer` |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, model vars | `docker compose up -d gateway` |
| `POSTGRES_PASSWORD`, `JWT_SECRET`, `AUDIT_SIGNING_KEY` | `docker compose up -d gateway kafka-consumer` |
| Frontend code (`ai-ui/`) | `docker compose up -d --build ai-ui` |
| Python code (`gateway.py`, `routers/`, `core/`) | `docker compose up -d --build gateway` |

---

## Port reference

| Service | Default port | What it serves |
|---|---|---|
| `gateway` | `8000` | The API — all `/ainxt/v1/api/*` routes |
| `ai-ui` | `5173` | The web UI — open this in your browser |
| `postgres` | `5432` | Main database and vector store |
| `redis` | `6379` | Cache, job queues, login-lockout counters |
| `kafka` | `9092` | Message queue (internal Docker network only) |
| `ollama` | `11434` | Local AI model server |
| `embed-svc` | `8001` | Embedding service (optional, off by default) |
| `privacy-svc` | `8002` | PII redaction service |

If a port is already taken on your machine, the installer moves the container to the next free port and records it in `.env`.

---

## Inspecting the database

Connect any PostgreSQL client to `localhost:5432` with the credentials from your `.env` file.

| Client | How to connect |
|---|---|
| **pgAdmin** | Host `localhost`, port `5432`, user/db from `.env` |
| **DBeaver** | New connection → PostgreSQL → same details |
| **psql** (inside WSL) | `psql -h localhost -U postgres -d ainxt_memory` |
| **Docker exec** | `docker exec ainxt-postgres psql -U postgres -d ainxt_memory -c "select count(*) from ainxt.chats;"` |

Key tables to check: `ainxt.chats`, `ainxt.chat_messages`, `ainxt.model_usages`.

---

## The event pipeline (Kafka)

AiNxt uses a message queue (Kafka) to make sure nothing gets lost — think of it like a reliable inbox between services. The installer sets it up for you automatically.

**Why it matters:** For chat turns, audit entries, usage records, and pipeline events, the gateway does not write to the database directly. It publishes an event to Kafka and returns immediately — and a separate consumer (`workers/kafka_consumer.py`) performs the database write. This means:

- If the Kafka consumer is down, the platform keeps answering requests and the UI keeps rendering — but the rows quietly never appear. You notice when you reopen a conversation and the history is empty, or when Analytics shows no spend.
- Running `./doctor.sh` checks both the broker and the consumer as **required** checks.

**Checking the pipeline:**

```bash
docker compose logs -f kafka-consumer    # is it writing?
./doctor.sh                              # broker, consumer and backlog, as required checks
```

**Redis fallback:** With `KAFKA_ENABLED=false`, events are queued to Redis lists under a 7-day TTL. Start a consumer within the week and the backlog is written; leave it and the events expire unread. `./doctor.sh` reports a non-empty backlog.

---

## Environment variable sections

The full template is in `.env.example` (108 variables). The most important sections:

| Section | Key variables | What they control |
|---|---|---|
| **Database** | `POSTGRES_HOST`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Where the main database lives |
| **Redis** | `REDIS_HOST`, `REDIS_PORT` | Cache and job queues |
| **Kafka** | `KAFKA_ENABLED`, `KAFKA_BOOTSTRAP` | Message queue on/off and address |
| **AI providers** | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `LOCAL_LLM_BASE_URL` | Which AI services the platform can call |
| **Auth** | `JWT_SECRET`, `JWT_ALGORITHM` | Token signing |
| **Audit** | `AUDIT_SIGNING_KEY` | Tamper-evident audit log signing |
| **Privacy** | `PRIVACY_SVC_URL`, `PRIVACY_FLOOR_ENFORCE` | PII redaction service address and enforcement mode |
| **Features** | `ENABLE_COACH`, `ENABLE_DISCUSSIONS`, `KAFKA_ENABLED` | Toggle optional features on/off |
| **Storage** | `AINXT_DOCS_DIR` | Where generated documents are stored |

After editing `.env`, restart the affected service — see [After changing configuration](#after-changing-configuration) above.

---

## Running natively (without Docker for the app)

```bash
./install.sh --local
```

Native mode runs the API, the Kafka consumer, and the web UI as normal processes on your machine, with only PostgreSQL, Redis, Kafka, and Ollama in Docker. You get the Vite dev server with hot reload and a `.venv` you can attach a debugger to. Requires Python 3.10+ and Node 18+ locally.

| Command | What it does |
|---|---|
| `./stop-local.sh` | Stop the API and UI (datastores keep running) |
| `tail -f log/gateway.out` | API log |
| `tail -f log/kafka-consumer.out` | Event → database writer log |
| `tail -f log/ai-ui.out` | UI log |
| `source .venv/bin/activate` | Use the virtualenv directly |
| `docker compose stop postgres redis ollama kafka` | Stop the datastores too |

The UI is at **<http://localhost:5173/>** in native mode (no `/portal/` prefix — that only applies to the production build).

---

## Non-interactive install (for CI)

```bash
AINXT_PROVIDER=none ./install.sh --yes
```

`AINXT_PROVIDER` accepts `anthropic`, `openai`, `gemini`, `ollama`, or `none`. If the matching `*_API_KEY` is already exported, the installer reuses it instead of prompting.

---

## Architecture overview

```
┌─────────────────────────────────────────────────────────────┐
│                        Clients                              │
│          ai-ui (React/Vite)   ABStudio (React/Vite)         │
└────────────────────┬────────────────────┬───────────────────┘
                     │                    │
┌────────────────────▼────────────────────▼────────────────────┐
│                  gateway.py  (FastAPI)                       │
│   routers/  │  middleware/  │  agents/  │  guardrails/       │
└──────┬──────────────┬──────────────────┬─────────────────────┘
       │              │                  │
┌──────▼──────┐ ┌─────▼──────┐ ┌─────────▼───────┐
│  PostgreSQL │ │    Redis   │ │  services/      │
│  + pgvector │ │  (cache/   │ │  embed_svc      │
│  (main DB + │ │   queues)  │ │  privacy_svc    │
│  vector DB) │ └────────────┘ │  llm_proxy      │
└──────▲──────┘                └─────────────────┘
       │  rows written here, not by the gateway
┌──────┴───────────────────┐
│ workers/kafka_consumer.py│◄── Kafka broker ◄── gateway publishes
└──────────────────────────┘
```

For the full production topology (multi-server, network zoning, disaster recovery), see [`docs/ENTERPRISE_DEPLOYMENT.md`](ENTERPRISE_DEPLOYMENT.md).

---

## Project structure

```
ainxt-enterprise/
├── gateway.py                  # FastAPI entrypoint
├── requirements.txt            # Python runtime dependencies
├── .env.example                # Environment variable template (108 vars)
├── ai-ui/                      # Main platform UI (React + Vite)
├── Agent Studio/               # Visual agent/workflow builder
├── agents/                     # AI agent definitions and tool registry
├── guardrails/                 # Runtime safety, PII detection
├── core/                       # Config, document parsing, crypto
├── db/                         # SQLAlchemy models, Alembic migrations
├── routers/                    # FastAPI route handlers
├── services/
│   ├── llm_proxy/              # Outbound LLM proxy
│   ├── embed_svc/              # Embedding service
│   ├── privacy_svc/            # PII/privacy service
│   └── discussions_svc/        # Discussions service
├── workers/                    # Background job workers (RQ)
├── connectors/                 # External connectors (Git, Jira, MCP)
├── sandbox/                    # Docker-based isolated code execution
├── tools/                      # Agent tool implementations
└── tests/                      # Test suite
```
