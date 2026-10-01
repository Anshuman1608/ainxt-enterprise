# SPDX-License-Identifier: MIT
"""Migration Part AE1's input: the .env-era price of each model (Phase 8).

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
