# Changelog

All notable changes to AiNxt Enterprise are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) —
see [`VERSIONING.md`](VERSIONING.md) for the stability tiers, the deprecation
process and the support window.

A deprecation is announced here in the minor release that introduces it, stays
functional for at least one further minor release, and is removed no earlier
than the release named in its entry.

---

## [Unreleased]

### Deprecated

- **Legacy model aliases** — `core/tiers.py::LEGACY_INBOUND_ALIASES`.

  **What is deprecated.** The 47 provider- and SKU-shaped model hints the
  platform accepts at its inbound client boundaries — `opus`, `haiku`,
  `sonnet`, `gpt`, `gpt-5.4`, `gemini`, `deep`, `solution`, `local`,
  `local:<id>` and 37 others. 24 of them translate to a capability tier; the
  other 23 name a concrete model and are passed through as an explicit pick.

  They are accepted at three places, and nowhere else:

  | Surface | Where | Counter label |
  |---|---|---|
  | CLI | `routers/messages_compat_router.py:342` | `surface="cli"` |
  | IDE plugins | `routers/ide_router.py:439` | `surface="ide"` |
  | SDK / API keys | `core/config.py:518` | `surface="ainxt-api"` |

  **What to use instead.** One of the eight capability tiers in
  `core.tiers.Tier` — `mini`, `simple`, `medium`, `complex`, `image-input`,
  `image-output`, `video-generation`, `intent-classification` — or a concrete
  model id from `GET /ainxt/v1/api/all-models`. A tier describes the capability
  you need; the administrator decides which model serves it. That is the whole
  point of the change: a client that asks for `opus` breaks when the operator
  switches provider, and a client that asks for `complex` does not.

  **When it is removed.** Two minor releases after the release that dates this
  section, and only if the usage gate below reads zero. Nothing is removed on
  a schedule alone.

  **How to read the usage gate.** Every translation increments the Prometheus
  counter `ainxt_legacy_model_alias_total`, labelled by `alias` and `surface`.
  The gate is:

  ```promql
  increase(ainxt_legacy_model_alias_total[<two release cycles>]) == 0
  ```

  Two things about that query are easy to get wrong, and both produce a
  confident, false "nobody is using it":

  - **The scrape job needs an admin bearer token.**
    `GET /ainxt/v1/api/metrics/prometheus` is admin-only. An unauthenticated
    job does not record zeroes — it records 401s, and the series never appears
    at all.
  - **A single reading of the counter proves nothing.** It is an in-process
    counter: it resets when the gateway restarts and when a worker is recycled
    (`max_requests`). Reading it once, or reading it after a deploy, shows a
    low number for reasons that have nothing to do with client behaviour. Use
    `increase()` over a range, which accounts for resets, from a Prometheus
    with at least the retention the window needs.

  If you operate a fleet, the gate is per-deployment: one client still sending
  `opus` anywhere keeps the shim alive everywhere.

  **Nothing changes yet.** Every alias still works exactly as before. The
  translation happens at the boundary only, is never exposed by the governance
  API, and does not affect routing.

### Removed

- **Model environment variables** — the 94 listed in `core/legacy_env.py::PHASE8_REMOVED_VARS`
  (Phase 8, plus `TIER_GOVERNANCE_ENABLED`). The two-release gate announced for
  them was waived for this release (plan.html D104).

  **What is gone.** Variables that named a model (`OPENAI_CODING_MODEL`,
  `CLAUDE_PRIMARY_MODEL`, …), its display label (`*_DISPLAY`), a per-feature
  override (`CIL_INTENT_MODEL`, `FACTORY_MODEL`, `SDLC_TIER_<TIER>_MODEL`,
  `SDLC_CLI_*_MODEL`, `AINXT_MODEL_*`, …), a per-SKU switch (`ENABLE_OPUS`,
  `VEO_ENABLED`, `BLOCKED_MODELS_EXTRA`, …) and the governance flag, with their
  `docker-compose.yml` defaults. Nothing reads them; a value left set is ignored.

  **What decides instead.** The models registered in Admin → LLM Providers and
  the tier assignments in Admin → Model Governance → Tiers. Governance is always
  on. `SDLC_MODEL_<STAGE>` now takes a tier name only.

  **Behaviour that changes.**
  - An unassigned tier fails the request with a message naming the Tiers screen;
    nothing falls back to `.env`. `mini`, `simple`, `medium`, `complex` and
    `intent-classification` must be assigned; `doctor.sh` fails until they are.
  - Prices come from the model row (`cost_per_1m_input`/`_output`). Migration
    Part AE1 copies the `.env`-era prices onto the rows, so **start the new
    release once with your old `.env` before deleting the lines**. An unpriced
    paid model bills at the conservative fallback rate; `doctor.sh` lists them.
  - The LLM proxy (`services/llm_proxy`) picks no model and prices nothing: every
    request must name its model (400 otherwise). Upgrade it with the gateway.

  **What to do.** Delete the listed lines from `.env`. The gateway logs any still
  set at startup, and `doctor.sh` fails under "legacy model env vars".

### Added

- `HOD_STATEMENT_LLM_ENABLED` (default `false`): narrate manager/HOD statements
  with the `medium` tier. Replaces the removed `HOD_STATEMENT_LLM_MODEL`.

### Changed

- **Max output tokens come from the provider.** Registering a provider or a model
  now records each model's output limit from the vendor (Anthropic `max_tokens`,
  Gemini `outputTokenLimit`, vLLM `max_model_len`, Ollama `context_length`;
  OpenAI by a rejected probe request). The Agent and Workflow editors, the CLI's
  `max_completion_tokens` and chat compaction read it from the model row. A model
  with no recorded limit gets the platform limit rather than 4096, and Auto gets
  the smallest limit among the models you may use. Run **Sync models** on
  existing providers to record limits for rows added before this change.
