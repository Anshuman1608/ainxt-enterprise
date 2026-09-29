# SPDX-License-Identifier: MIT
# ============================================================
# IMPORTS
# ============================================================

import re
import hashlib

from core.config import RDB_CACHE
from core.kv import get_kv
from core.logger import logger
from core.tiers import Tier


# ============================================================
# LLM FALLBACK (for ambiguous queries where regex confidence < 0.7)
# Routes through LLM proxy on the LLM proxy server — never calls Anthropic SDK directly.
# ============================================================

_LLM_FALLBACK_SYSTEM = (
    "Classify this query into one of: simple (greeting, trivial), "
    "medium (coding, analysis), complex (deep reasoning, multi-step). "
    "Respond with ONLY the tier name."
)

_VALID_TIERS = {"simple", "medium", "complex"}


def _llm_classify(question: str) -> str:
    """Classify a query with an LLM when regex confidence < 0.7.

    Phase 6 §N.1 step 2. This used to open its own httpx stream to
    LLM_PROXY_URL with a hardcoded {"provider": "claude"} body and a model
    from cli_model_for_tier("haiku") — two provider assumptions (R3) in a
    function whose entire job is to emit one of three enum labels.

    It now asks for the intent-classification tier and lets the router decide
    the provider, the transport and the failover. Same proxy underneath, so
    the network path is unchanged; the difference is that an administrator
    can now see and set what serves it, alongside the CIL classifier that
    answers the same kind of question on the same turn.

    `legacy_hint="haiku"` keeps the pre-migration model when governance is
    off (D15). Still falls back to "medium" on any failure — a caller of this
    function gets a tier or it gets "medium", never an exception.
    """
    try:
        from models.model_router import model_router, tier_request

        raw = model_router.generate(
            f"{_LLM_FALLBACK_SYSTEM}\n\nQuery: {question}",
            return_meta=False,
            **tier_request(Tier.INTENT_CLASSIFICATION, "haiku"),
        ) or ""
        # generate() reports runtime failures as a string rather than raising,
        # so an "Error: ..." body is a failure and must not be parsed as a label.
        if raw.startswith("Error"):
            logger.warning(f"LLM classifier unavailable ({raw[:80]!r}) — defaulting to medium")
            return "medium"
        # The model may return "simple.", "MEDIUM" etc — take first word
        label = raw.strip().lower()
        label = label.split()[0].rstrip(".,") if label else ""
        if label in _VALID_TIERS:
            logger.info(f"LLM classifier fallback → {label}")
            return label
        logger.warning(f"LLM classifier returned unexpected label '{label}' — defaulting to medium")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"LLM classifier fallback failed ({e}) — defaulting to medium")
    return "medium"


# ============================================================
# KV CACHE (DB=0)
# Backend selected via REDIS_CLIENT_CONFIG_DB0.
# ============================================================

redis_client = get_kv(RDB_CACHE, decode_responses=True)

CLASSIFIER_CACHE_TTL = 86400 * 7


# ============================================================
# SAFE PATTERNS (ONLY FOR OBVIOUS CASES)
# ============================================================

GREETING_PATTERN = re.compile(
    r"^(?:"
    # Core greetings + optional trailing "there"/"team"/"all" and punctuation.
    r"(?:hi|hii+|hello+|hey+|heya|hiya|yo|hola|namaste|namaskar|howdy|sup|wassup|"
    r"greetings|good\s*(?:morning|afternoon|evening|day)|gm|ge|hey\s*there|hi\s*there|hello\s*there)"
    r"(?:\s+(?:there|team|all|everyone|folks|guys))?"
    r"|"
    # Acknowledgements / thanks / sign-offs that also warrant a light reply.
    r"(?:thanks|thank\s*you|thanks\s*a\s*lot|thankyou|thx|ty|ok|okay|okey|k|kk|"
    r"cool|great|nice|got\s*it|alright|bye|goodbye|see\s*you|cya)"
    r")[\s!.]*$",
    re.IGNORECASE
)

VERY_SIMPLE_PATTERN = re.compile(
    r"^[a-zA-Z0-9\s]{1,30}$"
)

CODE_SIGNAL_PATTERN = re.compile(
    r"\b(class|method|implementation|architecture|repository|api|error|exception)\b",
    re.IGNORECASE
)

# True compound CamelCase — BaseChannel, ISOMsg, TransactionManager.
# Does NOT match plain capitalized words like "Java", "Fibonacci", "Spring".
CLASS_NAME_PATTERN = re.compile(
    r"\b(?:[A-Z]{2,}[a-z][a-zA-Z0-9_]*|[A-Z][a-z]{2,}[A-Z][a-zA-Z0-9_]*)\b"
)

# Questions that ask the AI to WRITE/GENERATE code are always general knowledge,
# regardless of what language or topic is mentioned.
WRITE_CODE_PATTERN = re.compile(
    r"^\s*(write|implement|create|build|make|generate|code|give me|show me|"
    r"can you write|how to write|how to implement|how do i|how to create)\b",
    re.IGNORECASE
)


# ============================================================
# CACHE KEY
# ============================================================

def _cache_key(prefix: str, question: str):

    key = hashlib.sha256(
        question.strip().lower().encode()
    ).hexdigest()

    return f"{prefix}:{key}"


# ============================================================
# COMPLEXITY CLASSIFIER
# ============================================================

def classify_query_complexity(question: str) -> str:
    """
    Production-grade query complexity classification.

    Fast path: regex heuristics with a confidence score.
    Slow path: when regex confidence < 0.7, delegates to Claude Haiku so that
               ambiguous queries are not mis-classified.

    Returns:
        simple
        medium
        complex
    """

    try:

        if not question:
            return "simple"

        cache_key = _cache_key("complexity", question)

        cached = redis_client.get(cache_key)

        if cached:
            return cached

        # Use the confidence-aware path (with LLM fallback) as the single source
        # of truth so that classify_query_complexity and classify_with_confidence_llm
        # are always consistent.
        label, _conf = classify_with_confidence_llm(question)

        redis_client.setex(
            cache_key,
            CLASSIFIER_CACHE_TTL,
            label,
        )

        return label

    except Exception as e:

        logger.error(f"Complexity classification failed: {e}")

        return "medium"


# ============================================================
# DOMAIN CLASSIFIER (SAFE, GENERIC)
# ============================================================

def detect_query_domain(question: str) -> str:
    """
    Generic domain detection.

    Returns:
        general
        code
    """

    try:

        if not question:
            return "general"

        cache_key = _cache_key("domain", question)

        cached = redis_client.get(cache_key)

        if cached:
            return cached

        q = question.strip()

        # "write/implement/create X" → always general (user wants AI to generate,
        # not look it up in the repo). Takes priority over class-name detection.
        if WRITE_CODE_PATTERN.search(q):
            redis_client.setex(cache_key, CLASSIFIER_CACHE_TTL, "general")
            return "general"

        # class name signal → code
        if CLASS_NAME_PATTERN.search(q):

            redis_client.setex(
                cache_key,
                CLASSIFIER_CACHE_TTL,
                "code"
            )

            return "code"


        # code keywords signal
        if CODE_SIGNAL_PATTERN.search(q):

            redis_client.setex(
                cache_key,
                CLASSIFIER_CACHE_TTL,
                "code"
            )

            return "code"


        redis_client.setex(
            cache_key,
            CLASSIFIER_CACHE_TTL,
            "general"
        )

        return "general"


    except Exception as e:

        logger.error(f"Domain classification failed: {e}")

        return "general"


# ============================================================
# CONFIDENCE-AWARE COMPLEXITY CLASSIFIER
# ============================================================

def classify_with_confidence(question: str) -> tuple:
    """
    Returns (label, confidence) where:
        label      — "simple" | "medium" | "complex"
        confidence — float 0.0–1.0 (1.0 = definitive, 0.5 = uncertain)

    Confidence is used by model_router to optionally upgrade the tier
    when the classifier is unsure.
    """
    try:
        if not question:
            return ("simple", 1.0)

        q = question.strip()

        # Definitive simple — greeting
        if GREETING_PATTERN.fullmatch(q):
            return ("simple", 1.0)

        # Very short factual queries — high confidence simple
        if len(q.split()) <= 2 and not CLASS_NAME_PATTERN.search(q):
            return ("simple", 0.9)

        # Definitive complex — class name or code signal present
        if CLASS_NAME_PATTERN.search(q):
            return ("complex", 0.95)

        if CODE_SIGNAL_PATTERN.search(q):
            # Code-related but no class name — high but not maximum confidence
            return ("complex", 0.85)

        # Medium-length queries — moderate confidence medium
        word_count = len(q.split())
        if word_count > 20:
            return ("complex", 0.7)

        if word_count > 8:
            return ("medium", 0.75)

        # Short non-greeting, non-code — uncertain medium
        return ("medium", 0.6)

    except Exception as e:
        logger.error(f"classify_with_confidence failed: {e}")
        return ("medium", 0.5)


def classify_with_confidence_llm(question: str) -> tuple:
    """
    Confidence-aware classifier with LLM fallback.

    Fast path: regex heuristics (same as classify_with_confidence).
    Slow path: when regex confidence < 0.7, delegates to Claude Haiku for a
               more accurate classification.

    Returns (label, confidence) where confidence reflects the final decision
    source (regex confidence if fast path, 0.9 if LLM resolved the ambiguity).
    """
    label, confidence = classify_with_confidence(question)
    if confidence < 0.7:
        llm_label = _llm_classify(question)
        logger.info(
            f"Classifier: regex=({label},{confidence}) → LLM fallback → {llm_label}"
        )
        return (llm_label, 0.9)
    return (label, confidence)