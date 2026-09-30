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
