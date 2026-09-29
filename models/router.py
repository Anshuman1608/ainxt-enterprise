# SPDX-License-Identifier: MIT
# ============================================================
# ENTERPRISE INTENT ROUTER
# ============================================================

import hashlib

from core.config import RDB_CACHE
from core.kv import get_kv
from core.prompts import INTENT_CLASSIFIER_PROMPT
from core.logger import logger


# KV cache (DB=0). Backend selected via REDIS_CLIENT_CONFIG_DB0.
redis_client = get_kv(RDB_CACHE, decode_responses=True)

CACHE_TTL = 86400 * 7


def _cache_key(question):

    key = hashlib.sha256(
        question.strip().lower().encode()
    ).hexdigest()

    return f"intent:{key}"


def classify_intent(question: str) -> str:

    if not question:
        return "general"

    try:

        cache_key = _cache_key(question)

        cached = redis_client.get(cache_key)

        if cached:
            return cached


        prompt = INTENT_CLASSIFIER_PROMPT.format(
            question=question
        )

        # Phase 6 §N.1 step 2 / §D.2 — one label from a two-value
        # vocabulary, which is what intent-classification names. Grouped with
        # the CIL classifier so a single admin setting governs the latency and
        # cost of every classification the platform does. legacy_hint keeps
        # the pre-migration local routing with governance off (D15).
        from core.tiers import Tier
        from models.model_router import model_router
        raw = model_router.generate(
            prompt, tier=Tier.INTENT_CLASSIFICATION, legacy_hint="simple",
        ).strip().upper()

        if raw == "CODE":
            intent = "code"
        else:
            intent = "general"

        redis_client.setex(
            cache_key,
            CACHE_TTL,
            intent
        )

        logger.info(f"Intent classified: {intent}")

        return intent

    except Exception as e:

        logger.error(f"Intent classifier failed: {e}")

        return "general"