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

- **Model environment variables** — the 93 listed in `core/legacy_env.py::PHASE8_REMOVED_VARS`.

  **What is deprecated.** Variables that name a model (`OPENAI_CODING_MODEL`,
  `CLAUDE_PRIMARY_MODEL`, `GEMINI_TEXT_MODEL`, …), its display label
  (`*_DISPLAY`), a per-feature model override (`CIL_INTENT_MODEL`,
  `FACTORY_MODEL`, `SDLC_TIER_<TIER>_MODEL`, …) or a per-SKU switch
  (`ENABLE_OPUS`, `ENABLE_SONNET_5`, `VEO_ENABLED`, …), and the matching
  `docker-compose.yml` defaults.

  **What to use instead.** Register providers and models in Admin → LLM
  Providers, and assign them to tiers in Admin → Model Governance. Infrastructure,
  credentials and policy variables (`LLM_PROXY_URL`, `*_API_KEY`,
  `PRIVACY_FLOOR_ENFORCE`, `LOCAL_HIDDEN_MODELS`, …) are not affected.

  **How to tell whether you use them.** The gateway logs one warning at startup
  naming every listed variable it was started with, and `doctor.sh` reports the
  same list under "legacy model env vars".

  **When it is removed.** Two minor releases after the release that dates this
  section, on the same terms as the aliases above.

  **Nothing changes yet.** Every variable is still read exactly as before.

### Changed

- **Max output tokens come from the provider.** Registering a provider or a model
  now records each model's output limit from the vendor (Anthropic `max_tokens`,
  Gemini `outputTokenLimit`, vLLM `max_model_len`, Ollama `context_length`;
  OpenAI by a rejected probe request). The Agent and Workflow editors, the CLI's
  `max_completion_tokens` and chat compaction read it from the model row. A model
  with no recorded limit gets the platform limit rather than 4096, and Auto gets
  the smallest limit among the models you may use. Run **Sync models** on
  existing providers to record limits for rows added before this change.
