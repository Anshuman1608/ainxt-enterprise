# SPDX-License-Identifier: MIT
# ============================================================
# FEATURE REGISTRY — the declared catalogue of assignable platform features
# ============================================================
#
# Which platform features an admin may assign a model to. Declared HERE, in
# code, rather than typed into a form:
#
#   * a free-text feature_key field produces an unusable admin screen and
#     silent typo-misses (assign "skills.generat" and nothing happens, with no
#     error anywhere), and
#   * the catalogue has to stay in step with the call sites that actually make
#     the LLM calls, which only code can guarantee.
#
# db/migrate.py Part AD1 upserts these into the `feature_registry` table, so
# re-seeding is idempotent and adding a feature is a one-line change here plus
# a migration re-run. The admin's *assignments* live in `feature_model_config`
# and are never touched by seeding.
#
# GRANULARITY: one key per (module, capability class). A module that
# deliberately uses BOTH a cheap/local tier and a cloud workhorse tier gets two
# keys — e.g. agents/advanced_reasoning.py has 6 call sites on the local tier
# and 4 on the Anthropic workhorse, and collapsing them would mean an admin
# assigning a cloud model to "advanced reasoning" silently moved the six cheap
# local passes onto it too.
#
# default_capability: the provider-neutral capability name
# (models/model_router.py _HINT_MAP) that the platform falls back to when an
# admin has assigned nothing. It is a LIVE fallback, consulted before the call
# site's own literal, so it is only ever seeded where the capability resolves
# to the EXACT SAME tier as the literal those call sites pass today:
#
#     local-only -> TIER_SIMPLE     == literal "simple" / "local"
#     fast       -> TIER_HAIKU      == literal "haiku"
#     balanced   -> TIER_COMPLEX    == literal "complex" / "claude"
#     expert     -> TIER_SOLUTION   == literal "solution"
#
# Features whose call sites use "medium" or "gpt" (TIER_MEDIUM, OpenAI) are
# left at None on purpose: no capability name maps to TIER_MEDIUM, so seeding
# the nearest one ("balanced") would switch them from OpenAI to Anthropic the
# moment this table landed. None means the resolver falls through to the call
# site's literal, which is the correct platform default for them.
# tests/core/test_feature_registry.py enforces both of those rules.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# core/rag_acl.py's ladder, most permissive first. CONFIDENTIAL and above are
# pinned to a local model at runtime by ModelRouter.route()'s privacy floor.
DATA_CLASSIFICATIONS = ("PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED", "PCI_SENSITIVE")

# The provider-neutral capability vocabulary. Kept in step with
# models/model_router.py's _HINT_MAP by tests/core/test_feature_registry.py.
CAPABILITIES = ("fast", "balanced", "expert", "vision", "long-context", "local-only")


@dataclass(frozen=True)
class FeatureSpec:
    """One assignable feature. Mirrors the feature_registry table's columns."""

    feature_key: str
    display_name: str
    category: str
    owning_module: str
    description: str = ""
    default_capability: Optional[str] = None
    requires_vision: bool = False
    requires_tools: bool = False
    requires_streaming: bool = False
    min_context_tokens: Optional[int] = None
    max_data_classification: str = "INTERNAL"


def _f(*args, **kwargs) -> FeatureSpec:
    return FeatureSpec(*args, **kwargs)


# ── The catalogue ────────────────────────────────────────────────────────────
# Grouped by category, which is also how the admin screen groups its rows.

FEATURES: tuple[FeatureSpec, ...] = (
    # ── Reasoning ────────────────────────────────────────────────────────────
    _f("reasoning.advanced", "Advanced reasoning — deep passes", "Reasoning",
       "agents.advanced_reasoning", default_capability="balanced",
       description="Multi-step reasoning, self-critique and synthesis passes.",
       requires_tools=True, min_context_tokens=64000),
    _f("reasoning.advanced_support", "Advanced reasoning — support passes", "Reasoning",
       "agents.advanced_reasoning", default_capability="local-only",
       description="The cheap scaffolding passes around a reasoning run "
                   "(scoring, pruning, formatting). Deliberately separate so "
                   "assigning a cloud model to the deep passes does not move "
                   "these onto it too."),
    _f("reasoning.orchestrate", "Agent orchestration", "Reasoning",
       "agents.orchestrator", default_capability="balanced",
       description="Plans and sequences multi-agent runs.", requires_tools=True),
    _f("reasoning.route", "Agent routing", "Reasoning",
       "agents.router_agent", default_capability="local-only",
       description="Picks which agent should handle a request. High volume, "
                   "short prompts."),

    # ── Chat ─────────────────────────────────────────────────────────────────
    _f("chat.respond", "Chat — answer generation", "Chat",
       "workers.chat_worker", default_capability="balanced",
       description="The main conversational answer path.",
       requires_tools=True, requires_streaming=True, min_context_tokens=128000),
    _f("chat.summarize", "Chat — history summarisation", "Chat",
       "memory.chat_summarizer", default_capability="local-only",
       description="Condenses older turns to keep a conversation inside the "
                   "context window."),
    _f("chat.title", "Chat — thread titling", "Chat",
       "routers.threads_router",
       description="Generates a short title for a conversation."),
    _f("context.compress", "Context compression", "Chat",
       "core.context_manager", default_capability="local-only",
       description="Compresses prompt context when a turn would overflow."),

    # ── Knowledge & retrieval ────────────────────────────────────────────────
    _f("retrieval.query_rewrite", "Query rewriting / expansion", "Knowledge",
       "models.query_rewriter", default_capability="local-only",
       description="Rephrases a question to improve retrieval recall."),
    _f("kb.graph", "Knowledge-graph extraction", "Knowledge",
       "workers.knowledge_graph_worker", default_capability="balanced",
       description="Extracts entities and relations from indexed documents.",
       min_context_tokens=128000),

    # ── Documents ────────────────────────────────────────────────────────────
    _f("docs.generate", "Document generation", "Docs",
       "agents.doc_generator_agent", default_capability="balanced",
       description="Writes long-form documents from a brief.",
       min_context_tokens=128000),
    _f("docs.outline", "Document outlining / section drafting", "Docs",
       "agents.doc_generator_agent", default_capability="fast",
       description="The cheap per-section passes inside a document build."),
    _f("docs.revise", "Document revision", "Docs",
       "services.doc_reviser", default_capability="balanced",
       description="Applies a requested change to an existing document."),
    _f("docs.intent", "Document intent classification", "Docs",
       "models.doc_intent", default_capability="local-only",
       description="Classifies what a user wants done to a document."),
    _f("presentations.generate", "Presentation generation", "Docs",
       "routers.presenton_router", default_capability="balanced",
       description="Builds slide decks."),

    # ── Skills ───────────────────────────────────────────────────────────────
    _f("skills.generate", "Skill generation", "Skills",
       "routers.skills_router", default_capability="balanced",
       description="Generates a new skill definition from a description.",
       requires_tools=True),
    _f("skills.synthesize", "Skill synthesis from usage", "Skills",
       "services.skill_synthesis", default_capability="balanced",
       description="Proposes new skills by mining observed usage."),

    # ── SDLC ─────────────────────────────────────────────────────────────────
    # The 17 SDLC *stages* keep their existing per-stage env mechanism
    # (core/model_registry.sdlc_stage_hint via SDLC_MODEL_<STAGE>), which
    # already accepts any provider's concrete model id. These coarse keys cover
    # the pipeline code that does NOT go through sdlc_stage_hint.
    _f("sdlc.pipeline", "SDLC pipeline — stage execution", "SDLC",
       "agents.sdlc_pipeline",
       description="Pipeline stages not covered by SDLC_MODEL_<STAGE>. Per-stage "
                   "overrides still win over anything assigned here.",
       requires_tools=True, min_context_tokens=128000),
    _f("sdlc.patch", "SDLC — patch generation", "SDLC",
       "agents.sdlc_patch_engine", default_capability="balanced",
       description="Produces code patches.", min_context_tokens=128000),
    _f("sdlc.review", "SDLC — review gates", "SDLC",
       "agents.review_engine", default_capability="local-only",
       description="Pre- and post-code review gates."),
    _f("security.code_gate", "Secure-code gate", "SDLC",
       "workers.secure_code_gate_worker", default_capability="balanced",
       description="Scans generated code for security issues before it lands."),
    _f("sandbox.self_heal", "Sandbox self-healing", "SDLC",
       "sandbox.self_healing_engine", default_capability="expert",
       description="Diagnoses and repairs a failing sandbox run."),
    _f("ide.complete", "IDE completions / chat", "SDLC",
       "routers.ide_router",
       description="Serves the IDE extension's completion and chat requests.",
       requires_tools=True, requires_streaming=True),

    # ── Workspace ────────────────────────────────────────────────────────────
    _f("cowork.tasks", "Buddy — task execution", "Workspace",
       "workers.cowork_task_worker", default_capability="balanced",
       description="Runs scheduled and ad-hoc Buddy tasks.", requires_tools=True),
    _f("projects.assist", "Workspace assistance", "Workspace",
       "routers.projects_router",
       description="Answers questions scoped to a project workspace."),
    _f("meetings.summarize", "Meeting summarisation", "Workspace",
       "workers.meeting_worker", default_capability="balanced",
       description="Summarises meeting transcripts and extracts actions.",
       min_context_tokens=128000),
    _f("workflows.build", "Workflow authoring", "Workspace",
       "tools.n8n_autonomous_builder", default_capability="balanced",
       description="Builds automation workflows from a description.",
       requires_tools=True),

    # ── Integrations ─────────────────────────────────────────────────────────
    _f("teams.triage", "Teams message triage", "Integrations",
       "services.teams_adapter", default_capability="balanced",
       description="Classifies and responds to Microsoft Teams messages."),
    _f("broadcast.compose", "Email broadcast composition", "Integrations",
       "routers.broadcast_router", default_capability="balanced",
       description="Drafts broadcast email copy."),
    _f("mcp.document_revise", "MCP — document revision tool", "Integrations",
       "connectors.mcp_bridge", default_capability="balanced",
       description="The revise-document tool exposed to external MCP clients."),
    _f("agents.build", "Agent builder", "Integrations",
       "agents.agent_builder",
       description="Builds user-defined agents. Note: an agent's own "
                   "agents_pg.preferred_model still wins over this."),
)


# ── Lookup helpers ───────────────────────────────────────────────────────────

FEATURES_BY_KEY: dict[str, FeatureSpec] = {f.feature_key: f for f in FEATURES}


def get_feature(feature_key: str) -> Optional[FeatureSpec]:
    """The declared spec for a feature key, or None if it is not declared."""
    return FEATURES_BY_KEY.get(feature_key)


def feature_keys() -> tuple[str, ...]:
    return tuple(FEATURES_BY_KEY)


def categories() -> tuple[str, ...]:
    """Declared categories, in first-declaration order (the UI's group order)."""
    seen: list[str] = []
    for f in FEATURES:
        if f.category not in seen:
            seen.append(f.category)
    return tuple(seen)
