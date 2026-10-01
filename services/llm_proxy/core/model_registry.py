# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt LLM PROXY — MODEL POLICY
# ============================================================
#
# The proxy has no model defaults and no prices (Phase 8, D111). Every request
# names its model: the backend resolves it from the admin's tier assignments.
# Cost is computed by the backend (core.model_registry.rates_for, D105).
#
# Kept: the egress deny-list of retired models (U2).
# ============================================================

BLOCKED_MODELS = {

    # Claude — retired/old models always blocked (mirrors root core/model_registry.py)
    "claude-opus-4-6",   # retired — superseded by Opus 4.7/4.8
    "claude-opus-4-5",
    "claude-opus-4",
    "claude-opus-3",
    "claude-sonnet-4-5", # retired — superseded by Sonnet 4.6

    # OpenAI — blocked variants
    "gpt-5.2-pro",
    "gpt-5.2",   # retired — replaced by gpt-5.4

}


def require_model(model, what: str) -> str:
    """The model the caller named, or ValueError: the proxy never picks one."""
    m = (model or "").strip()
    if not m:
        raise ValueError(f"{what}: no model named — the backend resolves it from the tier")
    if m in BLOCKED_MODELS:
        raise ValueError(f"{what}: blocked model {m!r}")
    return m
