# Model-Agnostic LLM Architecture Audit

**Purpose:** Enterprise-architecture review of where the platform is tightly coupled to specific LLM providers (Anthropic / OpenAI / Gemini / local), and what is required so an admin can configure, **per module/feature**, which configured LLM handles that feature's calls.

**Method:** Full-repo audit across backend (`gateway.py`, `services/`, `models/`, `routers/`, `agents/`, `AgentStudio/`, `db/`) and frontend (`ai-ui/`, `AgentStudio/frontend/`). All findings below are backed by file:line citations gathered by direct reading and repo-wide search — not speculation.

**Date:** 2026-09-23 · **Revision 2** (re-verified against source)

> **Revision 2 note.** Every finding in revision 1 was re-checked against the source tree. **Eight of twelve verified exactly as written.** **Four needed correction** (§3.2, §3.4, §3.9, §3.11), each marked **CORRECTED** with what was wrong — note that §3.4 is a special case: its core claim (four duplicate gateway stacks) is confirmed, but the *evidence* it cited for drift does not hold and has been replaced. Nine additional findings were discovered and added as §3.13–§3.21, marked **NEW**. The most consequential addition is §3.13 (the tier vocabulary is itself vendor-branded), which is a **prerequisite** for the feature→model mapping rather than cleanup. §3.14, §3.15, and §3.16 are live defects that get materially worse the moment admins begin assigning models, and have been moved into Phase 0. Sizing in §8 has been revised downward for Phase 2 and upward for Phase 1 in light of §4.0.

---

## 1. Executive Summary

The platform is in a **better starting position than a typical "hardcoded to one vendor" system**, but the model-agnostic story is incomplete in a specific, fixable way:

- ✅ There **is** a genuine DB-backed, admin-CRUD, runtime-editable "LLM Providers" registry (`llm_providers`/`llm_models` tables, `routers/llm_provider_admin_router.py`, `ai-ui/src/components/LLMProviderConfig.jsx`). An admin can add/remove/enable arbitrary provider accounts and models, including any OpenAI-compatible endpoint, without a redeploy.
- ✅ Most feature code calls a central `ModelRouter.generate(prompt, model_hint=<tier>)` (`models/model_router.py`) rather than talking to vendor SDKs directly — tiers, not raw model IDs, are the dominant vocabulary in business logic.
- ✅ **`ModelRouter.route()` already accepts an arbitrary admin-registered `model_id` as `model_hint`** (`models/model_router.py:1666-1700`, step 1a), resolved from `core.llm_provider_registry` *before* the hardcoded `_HINT_MAP` is consulted. This is the single most important de-risking fact in this audit — see §1.1.
- ❌ **There is no concept of "module" or "feature" anywhere in the schema or code.** Nothing maps "the Coach module" or "Skills generation" or "SDLC code review" to a specific provider/model that an admin can change at runtime. The only per-feature differentiation today is a **hardcoded tier-name string literal compiled into each call site** (`model_hint="complex"`, `model_hint="haiku"`, etc.).
- ❌ **The tier vocabulary is itself vendor-branded** (`TIER_HAIKU`, `TIER_SONNET_5`, `TIER_OPUS_5`, `TIER_GEMINI`, `TIER_TERA`, `TIER_LUNA`; `_HINT_MAP` keys `"claude"`, `"sonnet"`, `"opus"`, `"gpt"`). A feature→model mapping built on today's tiers inherits the vendor lock it is meant to remove (§3.13).
- ❌ **The provider-dispatch/adapter layer is quadruplicated** across `gateway_*.py` (root), `services/llm_proxy/`, `services/managed_endpoint_direct_llm.py`, and `AgentStudio/backend/app/core/llm_handler.py` — and has measurably drifted (§3.16, §3.17).
- ❌ **~20+ files bypass the router/registry entirely**, importing a specific gateway singleton or provider SDK directly.
- ❌ **Three parallel, inconsistent tiering vocabularies** exist simultaneously (§3.3).
- ❌ **Admin-registered models silently bypass budget enforcement** (§3.14) and are **misrouted to the local endpoint by AgentStudio** (§3.15). Both are live defects today, not future risks.
- ❌ **Embeddings and reranking sit entirely outside the provider registry** (§3.19) — an admin cannot assign an embedding model at all.
- ❌ A promising, purpose-built solution already exists **but is unwired**: `router/policy.py` + `profiles/routing.py` + `profiles/schema.py` implement exactly the "policy that picks a model given constraints" abstraction this project needs — but it has **zero live callers** today (§3.12).

**Bottom line:** the hard infrastructure work (credential vaulting, provider CRUD, model discovery, a central router, and — critically — registry-model resolution inside `route()`) is already done. What's missing is the **one layer that actually matters for this ask**: a `feature → model/provider` mapping that is (a) a first-class, admin-editable, DB-backed concept, (b) expressed in a **capability vocabulary rather than vendor product names**, and (c) actually consulted by the **76** call sites that today hardcode a tier literal.

### 1.1 The fact that most changes the plan

`models/model_router.py:1666-1700` — `route()` step 1a resolves **an arbitrary admin-registered `model_id` passed as `model_hint`**, checked *before* `_HINT_MAP` precisely so an exact registry match beats a coincidental alias collision. The in-code comment documents a live bug this fixed (selecting `claude-sonnet-5` was dispatching `claude-opus-5`).

The practical consequence: **the call-site migration is a single-argument substitution requiring no router changes.**

```python
# before
model_router.generate(prompt, model_hint="complex")
# after
model_router.generate(prompt, model_hint=resolve_feature_model("skills.generate"))
```

`resolve_feature_model()` returns a plain string that `route()` already knows how to resolve — a registry `model_id`, a capability tier, or a legacy alias. Phase 2 is therefore mechanical and scriptable, and Phase 3 (gateway consolidation) is **not** a prerequisite for shipping the requested capability.

The same mechanism is already proven in production by `agents_pg.preferred_model`, which is passed straight through as `model_hint` at `agents/agent_builder.py:1386-1394`.

---

## 2. Current State — What Already Works

| Capability | Status | Key files |
|---|---|---|
| Admin can register a provider account (Anthropic/OpenAI/Gemini/any OpenAI-compatible endpoint/Ollama) | ✅ Working, DB-backed, runtime-editable | `db/models.py:2784` (`LLMProvider`), `routers/llm_provider_admin_router.py`, `ai-ui/src/components/LLMProviderConfig.jsx` |
| Admin can register/discover/enable models under a provider, set a single global default | ✅ Working (but default is **global only**, not per-feature) | `db/models.py:2816` (`LLMModel`), same router/UI as above |
| Credentials are vaulted, not hardcoded | ✅ Working | `credential_vault` table, `resolve_credential()` in `core/llm_provider_registry.py` |
| Department/user can be allow/denied specific models | ✅ Working, DB-backed | `dept_model_permissions`/`user_model_permissions`, `routers/model_governance_router.py`, `ai-ui/src/components/ModelGovernance.jsx` — **note: this is an access-control gate, not a "which model does feature X use" assignment** |
| `ModelRouter.route()` resolves an arbitrary registry `model_id` given as `model_hint` | ✅ Working — **the hook point the feature resolver plugs into** | `models/model_router.py:1666-1700` (step 1a) |
| Feature code calls a tier (`"haiku"`, `"complex"`, `"solution"`, …) instead of a raw model ID | ✅ Mostly true, ⚠️ but the tier names are vendor product names (§3.13) | `models/model_router.py` |
| SDLC stages resolve their model from env, accepting a concrete model id of any provider | ✅ Working — **the closest existing precedent for the feature resolver** | `core/model_registry.py:sdlc_stage_hint()` (`SDLC_MODEL_<STAGE>`) |
| Circuit breakers / fallback chains per built-in vendor | ✅ Working for the ~15 built-in tiers; ❌ **not** for `TIER_REGISTRY` (admin-added) models | `core/circuit_breaker.py`, `ModelRouter._try_*` methods |
| Per-workflow-node model choice (AgentStudio) | ⚠️ Working, but scoped to one workflow's own JSON, not a platform-wide feature registry | `AgentStudio/frontend/src/features/workflows/editor/ConfigPanel.jsx` |
| Per-agent preferred model (user-built agents) | ⚠️ Working, DB-backed, and correctly passed to `route()` — but the column is a bare `String(50)` commented `auto\|claude\|gpt\|ollama` with **no FK to `llm_models`**, so it can silently hold a deleted or vendor-name value | `agents_pg.preferred_model` (`db/models.py:311`), `agents/agent_builder.py:1385-1404` |
| Admin can assign an **embedding** or **reranking** model | ❌ Not possible — env-only, outside the registry entirely (§3.19) | `services/embed_svc/config.py` |
| Per-feature cost / usage visibility | ❌ Not possible — `model_usages` is not keyed by feature (§3.21) | `services/llm_spend/` |

---

## 3. Findings — Tight Coupling & Hardcoding Inventory

Each finding carries a verification status: **CONFIRMED** (re-verified exactly as written in revision 1), **CORRECTED** (revision 1 was wrong or overstated; corrected here), or **NEW** (discovered in revision 2).

### 3.1 No "module/feature" entity exists — the central gap · CONFIRMED

Nothing in the schema (`db/models.py`, 94 ORM classes) represents "a feature" or "a module." The closest concepts are department, user, provider, and model. A repo-wide search for `feature_model`, `feature_key`, `FeatureModelConfig`, and `FeatureRegistry` returns **zero** hits outside this `docs/` directory. There is no table like:

```
feature_model_config(feature_key, model_id, fallback_model_id, tier_override, updated_by, updated_at)
```

Every other finding in this document is a downstream consequence of this one gap — even where routing is "abstracted" via tiers, the tier assigned to a feature is still a compile-time constant.

### 3.2 Hardcoded tier literals at **76** call sites · CORRECTED

> **Correction.** Revision 1 said "~40+ call sites" and claimed *"None of these read from an env var, DB row, or per-tenant config."* The count is **76** (`model_hint="` occurrences outside `tests/` and `docs/`), and the blanket claim is **false for the SDLC path** — see below.

The dominant pattern across the codebase is:

```python
model_router.generate(prompt, model_hint="complex")   # routers/skills_router.py:303,352,418,482
model_router.generate(prompt, model_hint="haiku")      # routers/chat_router.py:2220
model_router.generate(prompt, model_hint="claude")     # routers/broadcast_router.py:437
model_router.generate(prompt, model_hint="medium")     # routers/threads_router.py:225, ide_router.py:509
```

Distribution of the 76 literals (top files): `agents/advanced_reasoning.py` (10), `gateway.py` (6), `routers/skills_router.py` (4), `agents/doc_generator_agent.py` (4), `workers/doc_worker.py` (3), `workers/cowork_task_worker.py` (3), `workers/chat_worker.py` (3), `services/teams_adapter.py` (3), `agents/sdlc_pipeline/_core.py` (3), then ~30 files with 1–2 each.

**Important exception — the SDLC path is already partly solved and should be the template, not a migration target.** `core/model_registry.py:sdlc_stage_hint()` resolves each stage through `SDLC_MODEL_<STAGE>` env vars and, per its own docstring, *"accepts EITHER a router tier name OR a concrete model id of any provider. A concrete id is returned verbatim: the model router resolves it via the admin-configured provider registry … so a harness with no Anthropic provider can pin any stage to its own model."* That is precisely the shape §4.2 generalises — swap the env-var backing store for a DB row and it becomes the feature resolver.

For the remaining sites, changing which model powers Skills generation or Teams triage means editing Python and redeploying.

### 3.3 Three parallel, inconsistent tiering vocabularies · CONFIRMED

| Vocabulary | Where | Scope |
|---|---|---|
| `_HINT_MAP` / `TIER_*` constants | `models/model_router.py:698-791` (map at `:720`) | ~35 aliases (`simple/mini/medium/complex/sonnet/haiku/vision/gemini/solution/opus/opus-5/tera/luna/...`) |
| `AINXT_TIER_MAP` | `core/config.py:440-467` (map at `:452`) | Separate vocabulary for "ainxt-api" routing (`simple/medium/complex/local/local_mini/auto/default/claude/sonnet/gpt/gemini`) |
| `SDLC_STAGE_MODEL_DEFAULTS` | `core/model_registry.py:382-437` | Per-SDLC-stage (17 stages: classify/locate/coder/fixer/…), env-overridable via `SDLC_MODEL_<STAGE>`, constrained to `{haiku, medium, complex, solution, deep}` |

A "tier name" means a different thing depending on which of these three resolvers a given code path happens to call. Concretely: `"claude"` maps to `TIER_COMPLEX` in `_HINT_MAP` but to `AINXT_MODEL_COMPLEX` (an in-house vLLM model id) in `AINXT_TIER_MAP`. Any per-module admin config surface must pick (or reconcile) one canonical vocabulary — otherwise the admin UI will show a tier name whose actual effect differs by module. See §3.13 and §4.0 for what that canonical vocabulary must be.

### 3.4 Quadruplicated gateway/dispatch implementations · CONFIRMED (drift evidence CORRECTED)

The same "call vendor X's API" logic exists in four independently maintained places:

1. **Root gateways** — `gateway_claude.py` (643 lines), `gateway_openai.py` (662), `gateway_gemini.py` (1055), `gateway_generic_openai.py` (100), `gateway_local_llm.py` (667), `gateway_ollama.py` (36, deprecated shim); 3,163 lines total. No shared base class — `models/provider_protocol.py` (84 lines) is a non-enforced `typing.Protocol` whose own docstring documents "KNOWN SIGNATURE DRIFT" across all four real gateways, and which has **zero importers anywhere in the repo** (the only reference is a passing mention in a `gateway_generic_openai.py` comment).
2. **`services/llm_proxy/`** — a near-duplicate copy: `gateway_claude.py` (1042 lines), `gateway_openai.py` (594), `gateway_gemini.py` (1007), plus a 3940-line `main.py` with ~10 of its own `if provider == "claude"/"openai"/"gemini"` branches; 6,584 lines total.
3. **`services/managed_endpoint_direct_llm.py`** (477 lines) — a third implementation, used when `LLM_PROXY_URL` is unset: raw `anthropic.Anthropic(...)` and `openai.OpenAI(...)` instantiation (lazily imported at `:226`), its own `if provider == "claude": ... else: ...` dispatch, and a locally-forked copy of the shared message converter (`_oai_messages_to_anthropic_local`, `:94`) that its own comment describes as "a LOCAL fork, not imported."
4. **`AgentStudio/backend/app/core/llm_handler.py`** (2171 lines) — a fourth, fully independent client factory (`BaseLLMClient`, `OpenAIClient`, `ClaudeProxyClient`, `GeminiProxyClient`, `ClaudeDirectClient`, `FallbackLLMClient`) with its own hardcoded vendor base URLs and credential resolvers.

> **Correction.** Revision 1 cited "`services/llm_proxy/core/model_registry.py:118-126` maintains its own hardcoded blocklist of retired model IDs, separate from the main platform's" as the evidence of drift. **That is not supported.** The two static blocklists were diffed and are **identical in content** (root `core/model_registry.py:316-330` vs proxy `services/llm_proxy/core/model_registry.py:114-128` — same five Claude ids, same two OpenAI ids). The real drift is different, and worse: see **§3.16** (the proxy copy silently drops three governance kill-switches) and **§3.17** (the proxy copy is missing a blank-model guard the root already fixed). Those are the citations to use.

Any model-agnostic refactor, or even a routine "fix a Claude retry bug," today has to be applied in up to four places or the implementations silently drift — as §3.16 and §3.17 demonstrate they already have.

### 3.5 Direct bypass of the router/registry (~20 files) · CONFIRMED

Confirmed by direct read (not just grep), these import a specific gateway singleton or vendor SDK directly instead of going through `model_router`/`llm_provider_registry`:

- `agents/react_orchestrator.py:1189-1226,1450,1560` — instantiates `ClaudeGateway()`/`OpenAIGateway()`/`GeminiGateway()` fresh and implements its **own** independent fallback loop, parallel to `ModelRouter`'s. (Verified: `_try_claude()` at `:1189` does `from gateway_claude import ClaudeGateway; gw = ClaudeGateway()`.)
- `agents/agent_builder.py:1433` — `from gateway_claude import claude_gateway`, hardcoded to Claude "by design."
- `agents/brd_fsd_pipeline.py:101` — hardwired to "Claude Sonnet" for FSD generation, per its own docstring.
- `routers/coach_router.py:945-953,72` — hardcoded to OpenAI only. Note the docstring at `:948-951`, which claims the function *"Uses the existing OpenAIGateway.generate() streaming API (never calls the provider SDK directly)"* — vendor lock presented as an abstraction. Avoiding the raw SDK is not the same as being provider-agnostic.
- `routers/messages_compat_router.py:692,1247,1507` — imports all three gateway singletons directly; duplicates the same per-provider tool-call translation logic found in `gateway.py` and `services/llm_proxy/` a **third** time. Its `_normalise_model()` at `:336-367` is a literal `if m in ("claude","sonnet","complex","claude-sonnet-4-6")` chain across every vendor.
- `routers/chat_router.py:724,921,1349`, `routers/admin_router.py:212`, `routers/ide_router.py:255` — direct `gateway_gemini`/`gateway_local_llm` imports.
- `workers/doc_worker.py:5340,5358` — direct `gateway_gemini`/`gateway_openai` imports for image generation, gated by raw env checks.
- `models/local_model.py` — six separate direct imports of `gateway_local_llm`.
- `core/document_parser.py:368` — vision hardcoded to Gemini (`from gateway_gemini import generate_with_image`).
- `sandbox/doc_critic.py:170-171` — reaches into `model_router._get_gemini()` (a private accessor) directly for vision, bypassing the tier-hint API.

Inside `gateway.py` itself, at least 6 separate locations (lines ~10595-10634, 11744-11779, 12161-12307, 12376-12387, 13025/13094, 15241-15273) implement their own `if tier == X` / `if provider == "openai"` branches instead of calling one `model_router.generate()`/`.stream()` entry point — `model_router` is often used only as a gateway-object factory, not as the actual call site.

### 3.6 `ModelRouter` itself is not a clean dispatch table · CONFIRMED

`models/model_router.py` (3381 lines) is the intended central dispatcher, but internally it's a hand-written `if/elif` chain on tier name, **duplicated in at least 3 places** (`_tier_label()` at `:1102`, `_dispatch()` at `:1844`, `_dispatch_stream()` at `:2300` — 14 branches each, plus smaller duplicate clusters), rather than a `{tier: (family, model)}` table driven purely by the DB registry. Tier→vendor mapping is hardcoded in these branches; only `TIER_REGISTRY` (admin-added models) is DB-driven, and that path has **no cross-vendor fallback and no circuit breaker** — an admin-added model that fails just errors out, unlike the resilient built-in tiers. The code comment at `:708-714` states this explicitly: *"No cross-vendor fallback (unlike every other tier above)."*

### 3.7 Fixed 5-family enum — extension requires code changes · CONFIRMED

Provider `family` is a closed enum: `anthropic | openai | gemini | openai_compatible | ollama` (`db/models.py:2787-2791`, `routers/llm_provider_admin_router.py:79-92`). `openai_compatible` is a real escape hatch for any OpenAI-shaped API (OpenRouter, Together, vLLM, LiteLLM, self-hosted) — but a genuinely different protocol (e.g., Bedrock, Vertex-native, Cohere) requires: a new family value, a new `gateway_*.py` module, and a new branch in `ModelRouter._resolve_registry_gateway()` (`models/model_router.py:1459-1518`) — a code change, not an admin action.

### 3.8 Interface inconsistencies across the 4 real gateways · CONFIRMED

All four signatures were re-read and match this table verbatim, including the Gemini argument-order drift:

| Aspect | Claude | OpenAI | Gemini | Local |
|---|---|---|---|---|
| `generate()` signature | `(prompt, model=CLAUDE_MODEL, temperature=0, max_tokens=32000, stream=True)` `:401` | `(prompt, model=None, precleared=False, precleared_findings=None)` `:163` | `(prompt, precleared=False, precleared_findings=None, model=None)` `:124` — note argument-order drift | `(prompt, model=None, tier="simple", *, max_tokens=None, disable_reasoning=False)` `:414` |
| Streaming shape | Anthropic SSE event types | `chunk.choices[0].delta.content` | `response.candidates[0].content.parts[].thought` | OpenAI-shaped + reasoning-content fallback buffer |
| Tool schema | Anthropic `input_schema` (de facto internal standard) | Private converter `_anthropic_to_openai_tools()` | Private converter `_anthropic_to_gemini_tool()` | N/A |
| Cache economics | 10%/125% read/write | 50% automatic | 25% explicit | 0% (KV-cache) |
| Compliance gate | **None** (assumes pre-cleared) | `compliance_engine.validate_input()` w/ bypass | Same as OpenAI | None |
| Errors | Plain string returned | Plain string returned | Plain string returned | Raises in one method, string elsewhere |

`ModelRouter._filter_kwargs_for()` (`models/model_router.py:1520`) exists specifically to paper over this drift using `inspect.signature()` — a strong signal that a real shared interface (ABC/Protocol enforced, not advisory) is overdue. Note the compliance-gate row: **provider choice today silently changes whether a second compliance/PII pass runs on a given turn** — a correctness/compliance risk, not just a code-quality one, and it must be normalized before "admin picks any provider per feature" is safe to ship.

### 3.9 Cross-cutting duplicated logic (drift risk) · CORRECTED

> **Correction.** Revision 1 said the cloud-prefix tuple is "duplicated verbatim in 3 files." It is in **5 locations across 4 files**, plus a 6th divergent variant. More importantly, revision 1 filed this under "drift risk" — the budget-enforcement consequence is a live financial-control defect and is broken out separately as **§3.14**.

- **Cloud-vendor-prefix tuple** `("gpt-","claude-","gemini-","openai/","anthropic/","google/","azure/")` duplicated verbatim in **5 locations**: `middleware/budget_middleware.py:44`, `workers/chat_worker.py:1439`, `gateway.py:5408`, `gateway.py:10927`, `routers/ide_router.py:313`. A **6th, divergent** variant exists at `routers/messages_compat_router.py:381` with a different member list (`"claude","gpt","o1","o3","o4","gemini","local","ollama"`) — so two call paths classify the same model id differently.
- **"Is this a local/in-house model" heuristic** independently re-implemented in **8+ places**, each with a *different* substring list: `agents/agent_builder.py:40` (`local`/`ollama`/`llama`), `routers/projects_router.py:387` (`ollama`/`local`/`llama`), `routers/memory_router.py:27` (`llama`/`ollama`/`local`), `routers/coach_router.py:72` (`glm-`/`qwen`/`llama`), `models/model_router.py:961-962` (`local`/`kimi`/`glm`/`qwen`/`deepseek`/`llama`/`gemma`/`mistral`), `AgentStudio/backend/app/core/llm_handler.py:621` (`local`/`llama`/`ollama`), `AgentStudio/backend/app/core/governance.py:177,200`, `AgentStudio/backend/app/cli_runtime/runner.py:225` (`kimi-`/`glm-`/`qwen`/`mistral`/`mixtral`/`gemma`/`deepseek`), `ai-ui/src/components/Coach.jsx:1564`. The comment at `governance.py:177` concedes the approach is unsound: *"a pure substring heuristic misses them."*

Both should be replaced by one helper reading the DB `family` column, which is authoritative and already populated.

### 3.10 Concrete provider/model mismatch bug (found, not hypothetical) · CONFIRMED

`services/feedback_processor.py:337`, `models/classifier.py:64`, `models/hybrid_retriever.py:90` **and** `:670` — **4 call sites across 3 files** — all send a request payload like:

```python
json={"provider": "claude", "prompt": prompt, "model": cli_model_for_tier("haiku")}
```

`"provider"` is hardcoded to `"claude"` while `"model"` is resolved dynamically from the tier registry. **If an admin repoints the `haiku` tier to a non-Anthropic model (which the existing admin UI already allows), these 4 call sites will send a mismatched provider/model pair and the request will fail or hit the wrong vendor.** This is a live bug today, made worse by any move toward more admin-driven model reassignment, and should be fixed as part of this work regardless of scope.

### 3.11 Frontend hardcoded model catalogs · CORRECTED (scope narrowed, severity re-pointed)

> **Correction.** Revision 1 claimed 6+ frontend catalogs that "will silently go stale." That overstates the `ai-ui/` side. `Chat.jsx`, `Office.jsx`, and `ModelGovernance.jsx` **all fetch `GET /all-models` live** and consult a `dynamicMeta` map built from that response **before** the static table, falling through to a string-derived `providerMeta()` for anything unknown. `ModelGovernance.jsx:9-12` says so in a comment: *"This is a capability display map, NOT a list of defaults … Models not in this map fall through to providerMeta() … so unknown/custom models still render correctly."* Those literals are cosmetic fallbacks, correctly designed.

The genuine defect is narrower but sharper, and sits in **AgentStudio's frontend**, where the literals feed request parameters rather than labels:

- `AgentStudio/frontend/src/utils/modelMaxTokens.js:42-72` — a hardcoded `MODEL_MAX_TOKENS` table keyed by literal model ID, consumed by `ConfigPanel.jsx:18` via `getMaxTokensForModel()`. An admin-added model absent from this table gets a **wrong or default token budget** on every request, not merely a wrong label.
- `AgentStudio/frontend/src/config/models.js` (`LEGACY_NODE_MODEL`), `AgentStudio/backend/workflow_factory/pipeline.py:194-217` — hardcoded allowlists of literal model IDs for workflow authoring.

`ai-ui/`'s static maps should still be consolidated in Phase 4 for consistency, but they are **cosmetic**, and the AgentStudio token table is **functional** — prioritise accordingly.

### 3.12 Unwired policy-engine scaffolding (the intended long-term answer) · CONFIRMED

`router/policy.py` (163 lines) and `profiles/routing.py` (151) + `profiles/schema.py` (77) implement a `DomainProfile` → `RoutingPolicy` (quality/cost/latency weights, privacy floor, budget cap) → `choose(candidates, ...)` pure-function design — explicitly documented in both files as **not wired into `models/model_router.py` yet** and confirmed to have **zero production callers** (the only importers are `tests/router_policy/test_policy.py` and `tests/profiles/test_routing.py`). `models/model_router.py:893` references `RoutingPolicy.privacy_floor` in a comment only.

**Additional:** a *second*, separate `DomainProfile` exists at `cil/policy.py:30` (119 lines), unrelated to `profiles/schema.py:68`. Any promotion of this scaffolding must reconcile the two, or the platform will ship two things called `DomainProfile` that mean different things.

This is architecturally the right shape for "an admin sets a policy per domain/module and the router picks the best available model under it" — it should be the target design, not a fifth parallel mechanism invented from scratch.

### 3.13 The tier vocabulary is itself vendor-branded · NEW · **blocking**

The tier constants at `models/model_router.py:693-715` are, in substantial part, **vendor product names rather than capability descriptions**:

```
TIER_HAIKU = "haiku"        TIER_SONNET_5 = "sonnet-5"    TIER_OPUS_5  = "opus-5"
TIER_OPUS_48 = "opus-4-8"   TIER_GEMINI   = "gemini"      TIER_TERA    = "tera"
TIER_LUNA    = "luna"       TIER_MINI     = "mini"        TIER_DEEP    = "deep"
```

and `_HINT_MAP` (`:720`) keys include `"claude"`, `"sonnet"`, `"opus"`, `"gpt"`, `"gemini"`, `"gpt-5-mini"`, `"gpt-5.4"`, `"claude-opus-4-8"`. The same leakage appears in `AINXT_TIER_MAP` (`core/config.py:466-469`, keys `claude`/`sonnet`/`gpt`/`gemini`), in `SDLC_STAGE_MODEL_DEFAULTS`' allowed set `{haiku, medium, complex, solution, deep}` (`core/model_registry.py:380`), and in `agents_pg.preferred_model`'s `auto|claude|gpt|ollama` comment (`db/models.py:311`).

**Why this blocks the feature→model work:** the admin UI's per-feature dropdown has to offer *something* as the "leave it to the platform" option. If that something is `"haiku"`, then an admin who has configured only OpenRouter and Ollama is being asked to choose an Anthropic product name for a feature that will never touch Anthropic. Worse, `connectors/mcp_bridge.py:912-999` is documented as deliberately model-agnostic yet still exposes this Claude-ish tier vocabulary to external tool consumers.

**Required:** a capability vocabulary — e.g. `fast` / `balanced` / `deep` / `vision` / `long-context` / `local-only` — with every current vendor-branded tier demoted to a backward-compatible **alias** in `_HINT_MAP`. This is cheap (it is an aliasing change, not a dispatch change) but it must land in **Phase 1**, because `feature_registry.default_capability` and the admin dropdown are both defined in terms of it. Building the feature table on today's tier names bakes the vendor lock into the new schema.

### 3.14 Budget enforcement is bypassed for admin-registered models · NEW · **live defect**

`middleware/budget_middleware.py:48-56`:

```python
def _is_inhouse_model(hint: str) -> bool:
    """Budget enforcement is SKIPPED for in-house models — they carry no external API cost."""
    h = (hint or "").lower().strip()
    if not h or h in _AUTO_HINTS:
        return False
    return not any(h.startswith(p) for p in _CLOUD_PREFIXES)
```

The classification is **"not one of seven known cloud prefixes ⇒ in-house ⇒ free ⇒ skip budget checks."** Every model an admin registers through the `openai_compatible` family whose id does not begin with `gpt-`/`claude-`/`gemini-`/`openai/`/`anthropic/`/`google/`/`azure/` is therefore treated as free and **exempted from budget enforcement entirely** — including genuinely paid cloud models such as `mistralai/mixtral-8x7b-instruct`, `meta-llama/llama-3.1-70b-instruct`, `deepseek-chat`, or any OpenRouter id in `vendor/model` form for a vendor outside that list.

This is the direct, already-shipped consequence of pairing an open provider registry with a closed prefix heuristic, and it worsens sharply once admins can point high-traffic features at arbitrary registered models. The fix is the same shared registry-backed helper as §3.9: ask the DB for the model's `family` and provider, do not parse its name. **Phase 0.**

### 3.15 AgentStudio infers provider from model-name prefix and misroutes · NEW · **live defect**

`AgentStudio/backend/app/core/llm_handler.py:270-295`:

```python
def _classify_model(model_name: str) -> str:
    """Return "anthropic" | "openai" | "gemini" | "local" for model_name."""
    if name.startswith("claude"):  return "anthropic"
    if name.startswith("gemini"):  return "gemini"
    if (name.startswith("gpt") or name.startswith("o1") or name.startswith("o3")
        or name.startswith("openai/") or name.startswith("openai-")):
        return "openai"
    return "local"          # ← everything else
```

The `else → "local"` default is deliberate and documented (in-house GPU model ids follow no convention), but it is unsound once the model catalogue is admin-extensible. An admin-registered OpenRouter id such as `anthropic/claude-sonnet-4-6` matches **none** of these branches — `"anthropic/"` is not `"claude"` — and is therefore routed to `LOCAL_LLM_BASE_URL` (LiteLLM), which will 404 or, worse, silently serve a different model. The same applies to any Bedrock-style id, any renamed deployment, and every Cohere/Mistral/Qwen id served from a paid endpoint.

This is the same class of defect as §3.10 (provider inferred independently of the model's actual registry row), in a second codebase. It belongs in **Phase 0**, not Phase 3 — the fix is to look up `family` from `llm_models`, which does not require the full gateway consolidation.

### 3.16 The llm_proxy registry silently drops three governance kill-switches · NEW · **compliance**

Root `core/model_registry.py` gates model availability on four independent switches:

| Switch | Root behaviour | Line |
|---|---|---|
| `ENABLE_OPUS` | blocks `CLAUDE_OPUS_MODEL`, `CLAUDE_OPUS_48_MODEL` | `:332-334` |
| `ENABLE_CLI_OPUS_48` | blocks `CLAUDE_OPUS_48_MODEL` | `:443-444` |
| `ENABLE_CLI_OPUS_5` | blocks `CLAUDE_OPUS_5_MODEL` | `:447-448` |
| `ENABLE_SONNET_5` | blocks `CLAUDE_SONNET_5_MODEL` | `:451-452` |

The 142-line proxy copy `services/llm_proxy/core/model_registry.py` implements **only** `ENABLE_OPUS` (`:131-134`) and `BLOCKED_MODELS_EXTRA` (`:136-142`). The last three switches do not exist in it at all.

Because `services/llm_proxy/gateway_claude.py:433` performs a bare `from core.model_registry import BLOCKED_MODELS`, **which of the two registries is enforced depends on the proxy process's `sys.path`** — the trimmed copy if the service runs with `services/llm_proxy/` as its root, the full one if it runs from the repo root. A deployment that sets `ENABLE_CLI_OPUS_5=false` may therefore still serve Opus 5 through the proxy path while correctly refusing it on the direct path.

This is a governance/compliance finding, not a maintainability one, and it is the **real** evidence for §3.4's drift claim.

### 3.17 The llm_proxy Claude gateway is missing a blank-model guard the root already fixed · NEW

Root `gateway_claude.py:103-118` carries a 14-line comment explaining a production outage and its fix:

```python
class ClaudeGateway:
    from core.model_registry import BLOCKED_MODELS
    # ... CLAUDE_MODEL is blank for any deployment configured purely through the
    # "LLM Providers" admin screen ... BLOCKED_MODELS can itself contain "" ...
    # Without the `CLAUDE_MODEL and` guard, that made "" match, raising this
    # exception at CLASS-DEFINITION time — meaning gateway_claude.py could never
    # be imported at all, so EVERY Claude model failed with "no gateway available".
    if CLAUDE_MODEL and CLAUDE_MODEL in BLOCKED_MODELS:
        raise Exception("Blocked Claude model attempted")
```

The proxy copy at `services/llm_proxy/gateway_claude.py:432-436` still has the **unguarded** original:

```python
class ClaudeGateway:
    from core.model_registry import BLOCKED_MODELS
    if CLAUDE_MODEL in BLOCKED_MODELS:          # ← no `CLAUDE_MODEL and` guard
        raise Exception("Blocked Claude model attempted")
```

The trigger is conditional — it needs `""` to be present in whichever `BLOCKED_MODELS` resolves. With the trimmed proxy registry and default `ENABLE_OPUS=true`, `""` is not added, so it does not fire today; it fires if `ENABLE_OPUS=false` with a blank `CLAUDE_OPUS_MODEL`, or if the proxy resolves the **root** registry (where `ENABLE_CLI_OPUS_5` is off by default and adds a blank `CLAUDE_OPUS_5_MODEL`). Latent, exactly the drift §3.4 predicts, and a one-line fix.

### 3.18 `generate_with_model()` validates a model then discards it · NEW

`gateway_openai.py:113-121` (root) **and** `services/llm_proxy/gateway_openai.py:66-73` (proxy) are identical:

```python
def generate_with_model(self, prompt, model):
    from core.model_registry import BLOCKED_MODELS
    if model in BLOCKED_MODELS:
        raise Exception(f"Blocked model attempted: {model}")
    return self.generate(prompt)      # ← `model` is never forwarded
```

The `model` argument is governance-checked and then dropped; `self.generate()` falls back to its own default. A caller asking for a specific OpenAI model gets whatever the gateway default is. This is a shared bug in both copies rather than drift, and it undercuts any per-feature model assignment that routes through this method.

### 3.19 Embeddings and reranking sit entirely outside the provider registry · NEW

The `llm_providers`/`llm_models` registry covers **generation only**. Embedding and reranking model selection lives in a separate, env-only path with no DB representation, no admin UI, and no governance:

- `services/embed_svc/config.py:12-30` — `OLLAMA_EMBED_MODEL` (default `nomic-embed-text:latest`), `OPENAI_EMBED_MODEL` (default `text-embedding-3-small`), `OPENAI_DIMS = 768` **hardcoded**, plus `NOMIC_EMBED_URL`/`NOMIC_EMBED_API_KEY`/`NOMIC_EMBED_MODEL` for any OpenAI-compatible embeddings endpoint.
- `services/embed_svc/reranker.py`, `core/kb_retrieval.py`, `models/hybrid_retriever.py`, `models/coverage_gate.py` — reranker selection, likewise env-only.

"Make the platform model-agnostic" is not complete while an admin can swap the generation model from a dropdown but must edit `.env` and restart a container to change the embedding model. This needs its **own** assignment flow rather than a row in the same table, for a hard reason: **changing an embedding model invalidates every stored vector.** The flow must be migration-aware (new model ⇒ reindex job ⇒ atomic cutover), and the hardcoded `OPENAI_DIMS = 768` must become a per-model property since dimensionality varies by model.

### 3.20 A fifth model catalogue: spend tracking · NEW

`services/llm_spend/approved_models.py` (322 lines) builds its tracking allowlist as the union of *(1)* model ids present in `core.model_registry` **env defaults** and *(2)* ids observed in `ainxt.model_usages` over a trailing 90 days. It does **not** consult `llm_models`. An admin-registered model is therefore bucketed as `'other'` in `llm_spend_daily` until it has organically accumulated usage — so the first weeks of spend on a newly assigned model are unattributed. Minor on its own, but it compounds §3.21.

### 3.21 No per-feature cost or usage telemetry · NEW

`model_usages` records model and user, but there is no `feature_key` dimension anywhere in the telemetry path. The moment admins can assign models per feature, the first question they will ask is *"what is this feature costing me on this model versus the alternative?"* — and today there is no way to answer it. Adding `feature_key` to the usage write path is a small change that must land **with** Phase 1, not after it; retrofitting it later means the comparison data does not exist for the period that matters most (immediately after rollout).

---

## 4. Target Architecture

### 4.0 Prerequisite: a capability vocabulary (do this first)

Before any feature table exists, replace the vendor-branded tier names of §3.13 with capability names, keeping every existing name as an alias:

| New canonical capability | Meaning | Aliases retained in `_HINT_MAP` |
|---|---|---|
| `fast` | cheapest/lowest-latency acceptable model | `haiku`, `mini`, `gpt-5-mini`, `simple` |
| `balanced` | default workhorse | `medium`, `coding`, `complex`, `sonnet`, `claude`, `gpt` |
| `deep` | highest-capability reasoning / review gates | `solution`, `opus`, `opus-5`, `opus-4-8`, `deep` |
| `vision` | image/visual input required | `vision`, `gemini-3.1-flash-image` |
| `long-context` | large-window requirement | `tera`, context-size promotions |
| `local-only` | must not leave the perimeter | `local`, `local_mini`, `gpt-oss` |

This is an aliasing change inside `_HINT_MAP`, not a dispatch change — `_dispatch()`'s branches are untouched. `AINXT_TIER_MAP` and `SDLC_STAGE_MODEL_DEFAULTS` adopt the same six names so §3.3's three vocabularies converge. `feature_registry.default_capability` and the admin dropdown's "platform default" option are then expressible without naming a vendor.

### 4.1 New first-class concept: "Feature" / "Module"

Introduce a `feature_registry` table, **code-seeded rather than free-text** — the admin picks from a declared list of real features. A free-form key field produces an unusable screen and silent typo-misses.

```sql
CREATE TABLE feature_registry (
    feature_key       VARCHAR PRIMARY KEY,   -- e.g. "sdlc.code_review", "coach.rewrite", "skills.generate"
    display_name      VARCHAR NOT NULL,
    category          VARCHAR,               -- grouping for the admin UI, e.g. "SDLC", "Chat", "Docs"
    description       TEXT,
    owning_module     VARCHAR,               -- e.g. "agents", "routers.coach", "sandbox"
    default_capability VARCHAR,              -- §4.0 vocabulary, NOT a vendor tier name
    requires_vision   BOOLEAN DEFAULT FALSE,
    requires_tools    BOOLEAN DEFAULT FALSE,
    requires_streaming BOOLEAN DEFAULT FALSE,
    min_context_tokens INTEGER,
    max_data_classification VARCHAR,         -- highest sensitivity this feature may process
    created_at        TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE feature_model_config (
    feature_key       VARCHAR REFERENCES feature_registry(feature_key),
    org_id            VARCHAR DEFAULT 'default',   -- reuse the existing multi-tenant column pattern
    model_id          UUID REFERENCES llm_models(id),
    fallback_model_ids JSONB DEFAULT '[]',         -- ordered cascade, not a single fallback
    capability_override VARCHAR,                   -- alternative: pin a capability instead of a model
    enabled           BOOLEAN DEFAULT TRUE,
    updated_by        VARCHAR,
    updated_at        TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (feature_key, org_id)
);
```

Note `model_id UUID` — `llm_models.id` is `UUID(as_uuid=False)` (`db/models.py:2838`), not an integer. `fallback_model_ids` is an ordered JSONB array rather than a single column, because §4.7 needs a cascade and a one-deep fallback would have to be widened immediately.

This mirrors the existing, proven pattern of `dept_model_permissions` — same conventions (`require_admin`, cache-invalidate-on-write via the Redis KV pattern in `core/llm_provider_registry.py`), just a new axis.

### 4.2 Resolution order (new canonical precedence)

One resolution chain in one new module, `core/feature_model_resolver.py`:

```python
def resolve_feature_model(feature_key: str, *, org_id: str = "default",
                          data_classification: str | None = None) -> str | None:
    ...
```

Precedence, highest first:

1. **Privacy floor.** If `data_classification` is CONFIDENTIAL or above, return the local pin and stop. This already exists as `route()` step 0 (`models/model_router.py:1610-1631`) and must not be bypassable by feature config.
2. **Env break-glass** — `AINXT_FEATURE_MODEL_<KEY>`, mirroring the proven `SDLC_MODEL_<STAGE>` pattern (§3.2). Lets an operator reroute a feature during an incident without DB access.
3. **Per-org feature override** (`feature_model_config` at the caller's `org_id`).
4. **Default feature config** (`feature_model_config` at `org_id='default'`).
5. **`feature_registry.default_capability`**, resolved through the §4.0 capability table.
6. **`None`** — caller's existing literal stands, so unmigrated features keep working.

**The return value is a plain string** — a registry `model_id`, a capability name, or a legacy alias — because `route()` step 1a already resolves all three (§1.1). The resolver never raises: an unknown feature returns `None` and the call site's current behaviour is preserved, which is what makes the migration safe to land incrementally.

Cache with an in-process TTL plus the existing `invalidate_cache()` hook so this stays cheap on the hot path.

### 4.3 Wire up the existing (unwired) policy engine instead of inventing a new one

Promote `router/policy.py` + `profiles/routing.py`/`schema.py` from scaffolding to production:

- `feature_model_config.capability_override` can optionally reference a `RoutingPolicy` (quality/cost/latency weights + privacy floor) instead of a fixed model — letting an admin say "for this feature, prefer cheap+fast unless the privacy floor requires local" rather than pinning one model.
- **First reconcile the two `DomainProfile` definitions** (`profiles/schema.py:68` and `cil/policy.py:30`, §3.12) — shipping both under one name guarantees confusion.
- This reuses work already designed for exactly this purpose rather than a fifth bespoke mechanism.

### 4.4 Unify the gateway/adapter layer (collapse 4 → 1)

Define one enforced interface (an ABC or a runtime-checked `Protocol`, not the current advisory one) that every gateway implements identically:

```python
class LLMGateway(Protocol):
    async def generate(self, prompt, *, model, max_tokens=None, temperature=None, stream=True, precleared=False) -> LLMResponse: ...
    async def generate_with_tools(self, ...) -> LLMResponse: ...
    def normalize_error(self, exc) -> LLMError: ...   # typed errors, not bare strings
```

- Consolidate `gateway_*.py` (root) and `services/llm_proxy/gateway_*.py` into one shared package imported by both, eliminating the duplicate-maintenance risk evidenced by §3.16 and §3.17.
- Fix §3.18 (`generate_with_model` dropping its `model` argument) as part of normalising the signatures.
- Retire `services/managed_endpoint_direct_llm.py`'s raw-SDK path — including its forked `_oai_messages_to_anthropic_local` — in favour of the shared gateways.
- Migrate `AgentStudio/backend/app/core/llm_handler.py` to consume the shared gateways + `core/llm_provider_registry.py` instead of its own client factory (largest single item, ~2171 lines to retire/rewrite). Its `_classify_model()` heuristic is fixed earlier, in Phase 0 (§3.15), because it is a live defect and must not wait for this.
- Normalize the compliance-gate inconsistency (§3.8) as part of this unification — every gateway must call the compliance layer identically, since provider choice will now be admin-configurable per feature and must not silently change compliance behaviour.

### 4.5 Admin UI: Feature → Model assignment screen

New screen (`ai-ui/src/components/FeatureModelConfig.jsx`, alongside `LLMProviderConfig.jsx` and `ModelGovernance.jsx`), backed by `routers/feature_model_config_router.py` (same CRUD conventions as `llm_provider_admin_router.py`):

- One row per registered feature, grouped by `category`.
- Model dropdown sourced from the live catalogue and **filtered by the feature's declared `requires_vision`/`requires_tools`/`min_context_tokens`**. Offering a text-only model for a vision feature is the single largest source of "I assigned it and it broke"; filter it out in the UI *and* validate server-side before save.
- Ordered fallback chain, not a single fallback.
- An explicit **"inherit platform default"** state, distinct from "unset" — the admin must be able to see which features are deliberately defaulted.
- A resolved-effective-model preview (showing which precedence rule in §4.2 won) and a test-invoke button.
- Features whose `max_data_classification` is CONFIDENTIAL or above are marked **local-only**, with cloud models **disabled in the dropdown** rather than silently overridden at runtime by the privacy floor. Silent override is correct behaviour but terrible UX: the admin must not believe an assignment took effect when it cannot.
- Per-feature cost/usage sparkline from the §3.21 telemetry, so the choice is informed.

### 4.6 Frontend: single source of truth for model metadata

Consolidate on the live `/all-models` catalogue. Priority order per §3.11: **first** `AgentStudio/frontend/src/utils/modelMaxTokens.js` and `config/models.js` (these feed request parameters and are functionally wrong for admin-added models), **then** the `ai-ui/` display maps in `ModelGovernance.jsx`, `Chat.jsx`, `Code.jsx`, `KbChat.jsx`, `Office.jsx` (cosmetic fallbacks behind a live fetch; consolidate for consistency, not correctness). Serve `max_tokens` and capability flags from `llm_models.capabilities`, which already exists for exactly this purpose.

### 4.7 Resilience: per-provider-row circuit breakers

Extend `core/circuit_breaker.py`'s `get_breaker(name)` to key by DB `provider_id`/`model_id` rather than only hardcoded vendor-family names, and give `TIER_REGISTRY`/admin-added models the same fallback-cascade treatment as built-in tiers, driven by `feature_model_config.fallback_model_ids`. Until this lands, every per-feature assignment onto an admin-added model creates a new single point of failure (§3.6).

### 4.8 Embeddings: a separate, migration-aware assignment flow

Per §3.19, extend the registry with an embedding/reranking model type, but **do not** expose it as an ordinary dropdown:

- Add `model_kind` (`generation` | `embedding` | `rerank`) to `llm_models`, and store dimensionality in `capabilities` (replacing the hardcoded `OPENAI_DIMS = 768`).
- Changing the assigned embedding model must enqueue a reindex job and cut over atomically on completion; the UI must state plainly that the change is a background migration, not an instant switch.
- Reranking has no such constraint and can be switched live.

---

## 5. New Features To Implement (summary list)

1. **Capability vocabulary** (§4.0) — alias-only change to `_HINT_MAP`/`AINXT_TIER_MAP`/`SDLC_STAGE_MODEL_DEFAULTS`. **Prerequisite for 2–4.**
2. `feature_registry` + `feature_model_config` DB tables (+ migration following `db/migrate.py` conventions).
3. `core/feature_model_resolver.py` — canonical resolution chain (§4.2).
4. `routers/feature_model_config_router.py` — admin CRUD API (mirrors `llm_provider_admin_router.py`).
5. `ai-ui/src/components/FeatureModelConfig.jsx` — admin UI screen (§4.5).
6. A lightweight **feature registration mechanism** — a decorator or explicit `register_feature("sdlc.code_review", default_capability="deep", ...)` call at import time, so `feature_registry` stays in sync with code without hand-maintained seed rows.
7. **`feature_key` added to the usage/spend telemetry path** (§3.21) — must land with item 2, not after.
8. Migration of the 76 hardcoded `model_hint="literal"` call sites to `resolve_feature_model(feature_key)`.
9. **A CI guard rejecting new `model_hint="<literal>"` outside the resolver** — without it the 76 sites regrow faster than they are migrated.
10. Enforced shared gateway interface (`LLMGateway` Protocol/ABC with typed errors) + consolidation of the 4 duplicate implementations into 1.
11. Fixes for the live defects: §3.10 (provider/model mismatch), §3.14 (budget bypass), §3.15 (AgentStudio misroute), §3.16 (proxy kill-switch gap), §3.17 (proxy blank-model guard), §3.18 (`generate_with_model` drops its argument).
12. Shared registry-backed helpers replacing the 5-way duplicated cloud-prefix tuple and the 8-way duplicated "is local model" heuristic (§3.9), sourced from the DB `family` column rather than string parsing.
13. Wiring `router/policy.py`/`profiles/routing.py` into `ModelRouter` as an optional, flag-gated policy-driven mode (§4.3), after reconciling the duplicate `DomainProfile`.
14. Per-provider-row circuit breakers + fallback cascades for admin-added (`TIER_REGISTRY`) models (§4.7).
15. Frontend model-catalog consolidation (§4.6), AgentStudio token table first.
16. Normalized compliance gating across all gateways (§4.4) — required before this is safe to ship.
17. **Embedding/rerank model assignment with a reindex-aware migration flow** (§4.8).

---

## 6. File Impact Matrix (by phase)

### Phase 0 — Live defect fixes (no architecture change, do first, low risk)

Revision 2 expands this phase: §3.14/§3.15/§3.16 are defects **already affecting admin-registered models today**, and all three get sharply worse once per-feature assignment ships.

| File | Change |
|---|---|
| `services/feedback_processor.py:337` | Derive `provider` from the resolved model's family instead of hardcoding `"claude"` (§3.10) |
| `models/classifier.py:64` | Same fix |
| `models/hybrid_retriever.py:90,670` | Same fix (**two** sites, not one) |
| `middleware/budget_middleware.py:44-56` | Replace the prefix heuristic with a registry lookup — paid admin-registered models currently skip budget enforcement (§3.14) |
| `AgentStudio/backend/app/core/llm_handler.py:270-295` | `_classify_model()` must resolve `family` from `llm_models`, not parse the model name (§3.15) |
| `services/llm_proxy/core/model_registry.py` | Add the missing `ENABLE_CLI_OPUS_48` / `ENABLE_CLI_OPUS_5` / `ENABLE_SONNET_5` gates, or import the root registry (§3.16) |
| `services/llm_proxy/gateway_claude.py:432-436` | Add the `CLAUDE_MODEL and` guard the root already has (§3.17) |
| `gateway_openai.py:113-121`, `services/llm_proxy/gateway_openai.py:66-73` | Forward `model` to `self.generate()` instead of discarding it (§3.18) |

### Phase 1 — Capability vocabulary + schema + resolver + admin API/UI (additive)

| File | Change |
|---|---|
| `models/model_router.py:693-791` | **§4.0 capability vocabulary** — add `fast/balanced/deep/vision/long-context/local-only`, demote vendor names to aliases. Dispatch branches unchanged |
| `core/config.py:452-470`, `core/model_registry.py:380-410` | Adopt the same six capability names so the three vocabularies converge (§3.3) |
| `db/models.py` | Add `FeatureRegistry`, `FeatureModelConfig` ORM classes (pattern: `LLMProvider`/`LLMModel` at `:2784-2847`; note `id` is `UUID(as_uuid=False)`) |
| `db/migrate.py` | New migration creating the two tables + seed rows for known features |
| `core/feature_model_resolver.py` (new) | Resolution chain (§4.2), cached, mirrors `core/llm_provider_registry.py`'s invalidation pattern |
| `routers/feature_model_config_router.py` (new) | Admin CRUD, mirrors `routers/llm_provider_admin_router.py` |
| `ai-ui/src/components/FeatureModelConfig.jsx` (new) | Admin UI screen (§4.5) |
| `ai-ui/src/App.jsx` or router config | Add sidebar entry/route |
| usage/spend write path | Add `feature_key` dimension (§3.21) |

### Phase 2 — Call-site migration (mechanical, incremental, scriptable)

Per §1.1 each site is a one-argument substitution with **no router change required**.

| Directory | Files (representative; 76 literals total) |
|---|---|
| `agents/` | `advanced_reasoning.py` (10), `doc_generator_agent.py` (4), `sdlc_pipeline/_core.py` (3), `sdlc_patch_engine.py`, `review_engine.py`, `orchestrator.py`, `sdlc_context.py`, `sdlc_state_machine.py`, `router_agent.py`, `agent_builder.py` |
| `routers/` | `skills_router.py` (4), `ide_router.py` (2), `chat_router.py`, `presenton_router.py`, `broadcast_router.py`, `threads_router.py`, `projects_router.py`, `kb_ask_router.py`, `cowork_tasks_router.py`, `messages_compat_router.py` |
| `workers/` | `doc_worker.py` (3), `cowork_task_worker.py` (3), `chat_worker.py` (3), `secure_code_gate_worker.py`, `meeting_worker.py`, `knowledge_graph_worker.py` |
| `services/` | `teams_adapter.py` (3), `doc_reviser.py`, `skill_synthesis.py` |
| `models/`, `memory/`, `core/`, `sandbox/`, `cil/` | `router.py`, `query_rewriter.py`, `doc_intent.py`, `postgres_memory.py`, `chat_summarizer.py`, `context_manager.py`, `self_healing_engine.py`, `connectors/mcp_bridge.py` |
| `gateway.py` | 6 literals, plus the 6 inline `if tier ==` branches noted in §3.5 |
| CI | Add the anti-regression lint from §5.9 |

### Phase 3 — Gateway consolidation (highest effort, highest long-term payoff; **not** a blocker for shipping)

| File(s) | Change |
|---|---|
| `models/provider_protocol.py` | Promote from advisory `Protocol` (84 lines, zero importers) to enforced interface; add typed `LLMError` |
| `gateway_claude.py`, `gateway_openai.py`, `gateway_gemini.py`, `gateway_local_llm.py`, `gateway_generic_openai.py` | Normalize signatures, streaming shape, cache-accounting hooks, compliance-gate calls |
| `services/llm_proxy/gateway_{claude,openai,gemini}.py`, `main.py` | Replace with imports of the shared root gateways; remove duplicate dispatch logic and the divergent registry copy |
| `services/managed_endpoint_direct_llm.py` | Replace raw-SDK calls with shared gateway calls; delete the forked `_oai_messages_to_anthropic_local` |
| `AgentStudio/backend/app/core/llm_handler.py` | Rewrite to consume shared gateways + `core/llm_provider_registry.py` instead of an independent client factory |
| `agents/react_orchestrator.py:1189-1226,1450,1560` | Replace ad hoc `ClaudeGateway()/OpenAIGateway()/GeminiGateway()` instantiation + custom fallback loop with `model_router` calls |
| `agents/agent_builder.py:1433`, `agents/brd_fsd_pipeline.py:101`, `routers/coach_router.py:945-953`, `routers/messages_compat_router.py:336-367,692,1247,1507`, `routers/chat_router.py:724,921,1349`, `routers/admin_router.py:212`, `routers/ide_router.py:255`, `workers/doc_worker.py:5340,5358`, `models/local_model.py`, `core/document_parser.py:368`, `sandbox/doc_critic.py:170-171` | Replace direct gateway imports with `model_router`/feature-resolver calls |

### Phase 4 — Cross-cutting cleanup

| File(s) | Change |
|---|---|
| `middleware/budget_middleware.py:44`, `workers/chat_worker.py:1439`, `gateway.py:5408`, `gateway.py:10927`, `routers/ide_router.py:313`, `routers/messages_compat_router.py:381` | Replace the 5 duplicated cloud-prefix tuples + 1 divergent variant with one registry-backed helper (the budget-specific fix lands earlier, in Phase 0) |
| `agents/agent_builder.py:40`, `routers/projects_router.py:387`, `routers/memory_router.py:27`, `routers/coach_router.py:72`, `models/model_router.py:961-962`, `AgentStudio/backend/app/core/llm_handler.py:621`, `AgentStudio/backend/app/core/governance.py:177,200`, `AgentStudio/backend/app/cli_runtime/runner.py:225`, `ai-ui/src/components/Coach.jsx:1564` | Replace the 8-way duplicated "is local model" heuristic with one helper backed by the DB `family` flag |
| `AgentStudio/frontend/src/utils/modelMaxTokens.js:42-72`, `config/models.js`, `AgentStudio/backend/workflow_factory/pipeline.py:194-217` | **Functional** — serve `max_tokens`/capabilities from `llm_models.capabilities` |
| `ai-ui/src/components/{ModelGovernance,Chat,Code,KbChat,Office}.jsx` | **Cosmetic** — consolidate the display-fallback maps onto the shared `/all-models` hook |
| `services/llm_spend/approved_models.py` | Source the allowlist from `llm_models` rather than env defaults + observed usage (§3.20) |
| `db/models.py:311` (`agents_pg.preferred_model`) | Migrate from bare `String(50)` to an FK/validated reference so it cannot hold a deleted model |

### Phase 5 — Resilience & policy engine

| File(s) | Change |
|---|---|
| `core/circuit_breaker.py` | Key breakers by provider/model DB id, not hardcoded vendor name |
| `models/model_router.py:1459-1518` (`_resolve_registry_gateway`) | Add fallback-cascade support for `TIER_REGISTRY` models |
| `router/policy.py`, `profiles/routing.py`, `profiles/schema.py`, `cil/policy.py` | Reconcile the duplicate `DomainProfile`, then wire into `ModelRouter` as an optional, flag-gated resolution mode |

### Phase 6 — Embeddings & reranking (new in revision 2)

| File(s) | Change |
|---|---|
| `db/models.py`, `db/migrate.py` | Add `model_kind` (`generation`/`embedding`/`rerank`) to `llm_models`; move dimensionality into `capabilities` |
| `services/embed_svc/config.py:12-30` | Read the assigned embedding model from the registry; remove the hardcoded `OPENAI_DIMS = 768` |
| `services/embed_svc/reranker.py`, `core/kb_retrieval.py`, `models/hybrid_retriever.py`, `models/coverage_gate.py` | Resolve the reranker from the registry |
| reindex worker (new) | Migration-aware embedding cutover (§4.8) |

---

## 7. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Migrating 76 call sites is broad and touches many owners' code | Phase 2 is naturally incremental and each site is a one-argument change (§1.1). The resolver returns `None` for unknown features, so migrated and unmigrated sites coexist safely. Add the §5.9 CI guard so the count only goes down |
| The capability rename (§4.0) touches the hottest routing map in the platform | It is alias-only — every existing string keeps working; `_dispatch()` is untouched. Ship it with a test asserting every pre-existing `_HINT_MAP` key still resolves to the same tier |
| Gateway consolidation (Phase 3) touches the highest-traffic code path | Land behind a feature flag / shadow mode; keep old gateways importable during transition; scope the AgentStudio `llm_handler.py` rewrite as its own project with its own test pass. **Phase 3 is not required to ship the requested capability** |
| Compliance-gate normalization (§4.4) could change behavior for existing traffic | Explicitly consult compliance/security stakeholders before changing which provider paths get PII scanning — a policy decision, not just a code change |
| Admin misconfiguration (pointing a vision-dependent feature at a text-only model) | `feature_registry.requires_vision/requires_tools/min_context_tokens` (§4.1), enforced **both** by dropdown filtering and server-side validation before save |
| Admin assigns a cloud model to a feature that processes CONFIDENTIAL data | The privacy floor (§4.2 step 1) silently wins at runtime, which is correct but invisible. The UI must disable cloud models for such features (§4.5) so the admin is never misled about what took effect |
| `TIER_REGISTRY` (admin-added models) has no fallback/circuit breaker | Phase 5 must land before or alongside wide Phase 2 migration onto non-built-in models, to avoid creating new single points of failure |
| Admins choose models blind without cost data | `feature_key` telemetry (§3.21) ships **with** Phase 1, not after — otherwise the comparison data for the rollout period never exists |
| Changing an embedding model silently invalidates the vector index | Phase 6 treats embedding reassignment as a migration with a reindex job and atomic cutover, never an instant dropdown change (§4.8) |
| Three existing tiering vocabularies can't be deleted overnight | The new resolver (§4.2) treats existing tier names as a fallback layer (steps 5–6), not a replacement — old `model_hint="complex"` call sites keep working before migration |

---

## 8. Rough Sizing (T-shirt, directional only)

| Phase | Scope | Size | Δ from rev 1 |
|---|---|---|---|
| 0 — Live defect fixes | 8 files (was 3) | S–M (1-2 days) | ↑ — §3.14/§3.15/§3.16/§3.17/§3.18 added |
| 1 — Capability vocabulary + schema + resolver + admin UI | ~10 new/changed files | M–L (1-1.5 weeks) | ↑ — §4.0 vocabulary and §3.21 telemetry added |
| 2 — Call-site migration | 76 literals, mechanical, scriptable | M (~1 week, parallelizable) | ↓ — §1.1 shows no router change is needed |
| 3 — Gateway consolidation | ~15 files, includes AgentStudio rewrite | L-XL (3-4 weeks, highest risk) | — |
| 4 — Cross-cutting cleanup | ~18 files | M (1 week) | — |
| 5 — Resilience & policy engine | ~5 files, architecturally significant | M (1-2 weeks) | — |
| 6 — Embeddings & reranking | ~6 files + reindex worker | M (1-2 weeks) | new |

**Recommended sequencing:** 0 → 1 → 2 (can start once the resolver lands) → 5 (before wide migration onto admin-added models) → 3 → 4 → 6.

Phases **0, 1, 2, and 5** alone deliver the user-facing capability requested. Phase 3 (gateway consolidation) is the long-term maintainability payoff that prevents the four implementations drifting further — valuable, but explicitly **not** a blocker, since `route()` already resolves registry models without it (§1.1). Phase 6 is required before "model-agnostic" is true of retrieval as well as generation.

---

## Appendix A — Key Files Reference

**Existing abstraction (extend, don't replace):**
`core/llm_provider_registry.py`, `core/model_registry.py`, `models/model_router.py`, `routers/llm_provider_admin_router.py`, `ai-ui/src/components/LLMProviderConfig.jsx`, `db/models.py:2784-2847`

**The hook point the whole plan depends on:**
`models/model_router.py:1666-1700` — `route()` step 1a, registry `model_id` resolution from `model_hint`

**Closest existing precedents to generalise:**
`core/model_registry.py:sdlc_stage_hint()` (env-backed per-stage model selection accepting any provider's model id); `agents/agent_builder.py:1385-1404` (`preferred_model` passed straight through as `model_hint`)

**Governance pattern to mirror for feature-level config:**
`routers/model_governance_router.py`, `ai-ui/src/components/ModelGovernance.jsx`, `dept_model_permissions`/`user_model_permissions` tables

**Unwired policy engine to promote to production (reconcile the duplicate `DomainProfile` first):**
`router/policy.py`, `profiles/routing.py`, `profiles/schema.py`, `cil/policy.py`

**Gateway implementations to consolidate:**
`gateway_claude.py`, `gateway_openai.py`, `gateway_gemini.py`, `gateway_generic_openai.py`, `gateway_local_llm.py`, `gateway_ollama.py`, `services/llm_proxy/gateway_{claude,openai,gemini}.py`, `services/llm_proxy/main.py`, `services/managed_endpoint_direct_llm.py`, `AgentStudio/backend/app/core/llm_handler.py`

**Bypass points to fix:**
`agents/react_orchestrator.py`, `agents/agent_builder.py`, `agents/brd_fsd_pipeline.py`, `routers/coach_router.py`, `routers/messages_compat_router.py`, `routers/chat_router.py`, `routers/admin_router.py`, `routers/ide_router.py`, `workers/doc_worker.py`, `models/local_model.py`, `core/document_parser.py`, `sandbox/doc_critic.py`

**Live defects (fix regardless of scope — Phase 0):**
`services/feedback_processor.py:337`, `models/classifier.py:64`, `models/hybrid_retriever.py:90,670` (§3.10) · `middleware/budget_middleware.py:44-56` (§3.14) · `AgentStudio/backend/app/core/llm_handler.py:270-295` (§3.15) · `services/llm_proxy/core/model_registry.py` (§3.16) · `services/llm_proxy/gateway_claude.py:432-436` (§3.17) · `gateway_openai.py:113-121` + `services/llm_proxy/gateway_openai.py:66-73` (§3.18)

**Outside the registry entirely (Phase 6):**
`services/embed_svc/config.py`, `services/embed_svc/reranker.py`, `core/kb_retrieval.py`, `models/hybrid_retriever.py`, `models/coverage_gate.py`
