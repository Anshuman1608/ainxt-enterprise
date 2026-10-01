# SPDX-License-Identifier: MIT
"""Environment variables Phase 8 removed (plan.html §I.2, §I.3, §I.6).

Each one named a model, a display label, or a per-SKU switch that the admin
registry now owns. Nothing reads them; this list lets the gateway warn and
doctor.sh fail when a deployment still sets one. Deliberately excluded: the §I.5 Keep
rows, LOCAL_HIDDEN_MODELS, LOCAL_MODEL_IDS* (Phase 7), DOWNGRADE_MODEL (under
investigation) and SDLC_MODEL_<STAGE> (narrowed to tier names, not removed).
"""

from __future__ import annotations

import os
import threading

from core.logger import logger

PHASE8_REMOVED_VARS: tuple[str, ...] = (
    # §I.2 model identity → admin configuration
    "CLAUDE_PRIMARY_MODEL", "CLAUDE_HAIKU", "CLAUDE_OPUS_MODEL", "CLAUDE_OPUS_46_MODEL",
    "CLAUDE_OPUS_48_MODEL", "CLAUDE_OPUS_5_MODEL", "CLAUDE_SONNET_5_MODEL",
    "OPENAI_SIMPLE_MODEL", "OPENAI_CODING_MODEL", "OPENAI_PRIMARY_MODEL", "OPENAI_LATEST_MODEL",
    "OPENAI_TERA_MODEL", "OPENAI_LUNA_MODEL", "OPENAI_OSS_MODEL", "OPENAI_IMAGE_MODEL",
    "OPENAI_DEEP_RESEARCH", "OPENAI_DEEP_RESEARCH_MINI",
    "GEMINI_TEXT_MODEL", "GEMINI_CODING_LITE_MODEL", "GEMINI_IMAGE_MODEL", "GEMINI_VISION_MODEL",
    "VEO_MODEL", "LOCAL_LLM_MODEL_NAME", "LOCAL_VISION_MODELS",
    "PRIMARY_VISION_PROVIDER", "FALLBACK_VISION_PROVIDER",
    "LOCAL_SIMPLE_MODELS", "LOCAL_MEDIUM_MODELS", "LOCAL_COMPLEX_MODELS", "CHAT_FALLBACK_CHAIN",
    "CLAUDE_HAIKU_DISPLAY", "CLAUDE_OPUS_48_DISPLAY", "CLAUDE_OPUS_5_DISPLAY",
    "CLAUDE_OPUS_DISPLAY", "CLAUDE_PRIMARY_DISPLAY", "CLAUDE_SONNET_5_DISPLAY",
    "GEMINI_CODING_LITE_DISPLAY", "GEMINI_DISPLAY", "GEMINI_IMAGE_DISPLAY", "GEMINI_TEXT_DISPLAY",
    "OPENAI_CODING_DISPLAY", "OPENAI_LATEST_DISPLAY", "OPENAI_LUNA_DISPLAY",
    "OPENAI_OSS_DISPLAY", "OPENAI_SIMPLE_DISPLAY", "OPENAI_TERA_DISPLAY",
    "LOCAL_LLM_DISPLAY", "VEO_DISPLAY",
    # §I.3 per-feature model overrides → tier requests
    "CIL_INTENT_MODEL", "DOC_INTENT_MODEL", "GENERAL_CHAT_MODEL", "ENHANCE_MODEL_HINT",
    "ENRICH_MODEL", "PPT_LLM_MODEL",
    "CODEWIKI_MAIN_MODEL", "CODEWIKI_CLUSTER_MODEL", "CODEWIKI_FALLBACK_MODEL",
    "HOD_STATEMENT_LLM_MODEL", "MANAGER_STATEMENT_LLM_MODEL",
    "FACTORY_MODEL", "ABSTUDIO_AGENT_DEFAULT_MODEL", "TRIAGE_MODEL", "VERIFIER_MODEL",
    "SWARM_AGGREGATOR_MODEL", "SWARM_ORCHESTRATOR_MODEL", "ABSTUDIO_FALLBACK_LLM_MODEL",
    "AINXT_MODEL_DEFAULT", "AINXT_MODEL_SIMPLE", "AINXT_MODEL_MEDIUM", "AINXT_MODEL_COMPLEX",
    "AINXT_MODEL_LOCAL", "AINXT_MODEL_LOCAL_MINI",
    "SDLC_TIER_SIMPLE_MODEL", "SDLC_TIER_MEDIUM_MODEL", "SDLC_TIER_COMPLEX_MODEL",
    "SDLC_TIER_DEEP_MODEL", "SDLC_TIER_SOLUTION_MODEL",
    "SDLC_CLI_CLASSIFY_MODEL", "SDLC_CLI_PLAN_MODEL", "SDLC_CLI_IMPLEMENT_MODEL",
    "BUDDY_FORCED_MODEL", "BUDDY_MODEL_LOCKED", "KB_FOLLOWUP_CONDENSE_MODEL_CHAIN",
    # §I.4 capability metadata held in env
    "MODELS_WITHOUT_TEMPERATURE",
    # §I.6 per-SKU switches → one availability decision
    "ENABLE_OPUS", "ENABLE_CHAT_OPUS", "ENABLE_CLI_OPUS_48", "ENABLE_CLI_OPUS_5",
    "ENABLE_SONNET_5", "ENABLE_GPT56_TERA", "ENABLE_GPT56_LUNA", "VEO_ENABLED",
    "BLOCKED_MODELS_EXTRA",
    # Phase 10 (flag half, D106): governance is always on
    "TIER_GOVERNANCE_ENABLED",
)


def legacy_vars_set(environ=None) -> list[str]:
    """The listed variables this process has a non-empty value for, in list order."""
    env = os.environ if environ is None else environ
    return [v for v in PHASE8_REMOVED_VARS if (env.get(v) or "").strip()]


_warned = False
_lock = threading.Lock()


def warn_legacy_env_once(environ=None) -> list[str]:
    """Log one line naming the set variables, once per process. Returns them."""
    global _warned
    found = legacy_vars_set(environ)
    with _lock:
        if _warned or not found:
            return found
        _warned = True
    logger.warning(
        "Model env vars removed in Phase 8 are still set and are IGNORED; delete them "
        "(models live in Admin → LLM Providers / Model Governance): %s",
        ", ".join(found),
    )
    return found
