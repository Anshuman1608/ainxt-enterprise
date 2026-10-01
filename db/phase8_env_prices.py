# SPDX-License-Identifier: MIT
"""Migration Parts AC1 and AE1's inputs: the .env-era models and prices (Phase 8).

The only place the removed model env vars are still named in application code,
and only so their prices survive the removal. Excluded from the legacy-env-refs
ratchet; delete with Part AE1 once every deployment has run it.
"""

from __future__ import annotations

import os

# The prices core/model_registry.MODEL_COST_PER_1M held before Phase 8, keyed by
# the env var that named the model.
ENV_PRICES = (
    ("OPENAI_SIMPLE_MODEL",       0.15,   0.60),
    ("OPENAI_CODING_MODEL",       2.50,  15.00),
    ("OPENAI_LATEST_MODEL",       5.00,  30.00),
    ("OPENAI_TERA_MODEL",         2.00,  12.00),
    ("OPENAI_LUNA_MODEL",         0.20,   1.20),
    ("OPENAI_OSS_MODEL",          0.0,    0.0),
    ("OPENAI_DEEP_RESEARCH_MINI", 2.00,  10.00),
    ("OPENAI_DEEP_RESEARCH",     15.00,  60.00),
    ("CLAUDE_PRIMARY_MODEL",      3.00,  15.00),
    ("CLAUDE_HAIKU",              0.80,   4.00),
    ("CLAUDE_OPUS_MODEL",        15.00,  75.00),
    ("CLAUDE_OPUS_48_MODEL",     15.00,  75.00),
    ("CLAUDE_OPUS_5_MODEL",      15.00,  75.00),
    ("CLAUDE_SONNET_5_MODEL",     3.00,  15.00),
    ("GEMINI_TEXT_MODEL",         0.30,   1.20),
    ("GEMINI_CODING_LITE_MODEL",  0.10,   0.40),
    ("GEMINI_IMAGE_MODEL",        0.30,  30.00),
)


def env_prices(environ=None) -> dict:
    """model id → (input, output) per 1M, as the pre-Phase-8 table resolved under this env."""
    env = os.environ if environ is None else environ
    out = {}
    for var, cin, cout in ENV_PRICES:
        mid = (env.get(var) or "").strip()
        if mid:
            out[mid] = (cin, cout)   # a later entry wins, as in the old dict literal
    return out



def env_video_price(environ=None) -> tuple:
    """(the env-pinned video model id or "", its per-second price)."""
    env = os.environ if environ is None else environ
    try:
        rate = float(env.get("VEO_COST_PER_SECOND") or "0.40")
    except ValueError:
        rate = 0.40
    return (env.get("VEO_MODEL") or "").strip(), rate


# Env var → (family, tier_tags, billing_tier), used ONLY by migration Part AC1's
# one-time backfill (db/migrate.py), read straight from the environment. This is the one deliberate hardcoded mapping in the whole
# LLM-provider-config feature — its entire purpose is migrating deployments
# OFF the env-var/hardcoded-literal model system and into the DB-backed
# llm_providers/llm_models tables that core/llm_provider_registry.py reads.
# New models added after this migration are never added here — they go
# through the admin "LLM Providers" screen instead.
AC1_MODEL_ROLE_TAGS = {
    "CLAUDE_PRIMARY_MODEL":     ("anthropic", ["complex", "claude", "sonnet"], "paid"),
    "CLAUDE_HAIKU":             ("anthropic", ["haiku"], "paid"),
    "CLAUDE_OPUS_MODEL":        ("anthropic", ["solution", "opus"], "paid"),
    "CLAUDE_OPUS_48_MODEL":     ("anthropic", ["opus-4-8", "opus"], "paid"),
    "CLAUDE_OPUS_5_MODEL":      ("anthropic", ["opus-5", "opus"], "paid"),
    "CLAUDE_SONNET_5_MODEL":    ("anthropic", ["sonnet-5"], "paid"),
    "OPENAI_SIMPLE_MODEL":      ("openai", ["simple", "mini"], "paid"),
    "OPENAI_CODING_MODEL":      ("openai", ["medium", "coding"], "paid"),
    "OPENAI_LATEST_MODEL":      ("openai", ["deep", "latest"], "paid"),
    "OPENAI_TERA_MODEL":        ("openai", ["gpt56-tera"], "paid"),
    "OPENAI_LUNA_MODEL":        ("openai", ["gpt56-luna"], "paid"),
    "OPENAI_OSS_MODEL":         ("openai", ["oss"], "free"),
    "GEMINI_TEXT_MODEL":        ("gemini", ["gemini", "coding"], "paid"),
    "GEMINI_CODING_LITE_MODEL": ("gemini", ["gemini-lite"], "paid"),
    "GEMINI_IMAGE_MODEL":       ("gemini", ["vision", "image-gen"], "paid"),
    "VEO_MODEL":                ("gemini", ["video"], "paid"),
    # LOCAL_LLM_MODEL_NAME deliberately excluded: unlike every other constant
    # here (which default to "" and are skipped when unset), it defaults to
    # the literal placeholder string "local-llm" — not a real, callable
    # Ollama/local-proxy model name — so seeding it always created a bogus
    # "local-llm" model row that admins could select but that could never
    # actually be dispatched. Real local models come from the admin's
    # "Sync installed models" / "Pull a new model" actions instead.
}
