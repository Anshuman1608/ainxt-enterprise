# SPDX-License-Identifier: MIT
"""Phase 2 — capability metadata the tier resolver will filter on.

Two properties are under test:

1. ``privacy_class`` derivation FAILS SAFE. It gates the no-cloud-egress
   routing constraint (plan.html §M.1), and the asymmetry matters: classifying
   an external model as local leaks data silently, while the reverse merely
   fails a request visibly. Anything unclassifiable must therefore come back
   ``external``.

2. Capability validation rejects malformed governed keys rather than coercing
   them. A typo in ``privacy_class`` does not fail loudly on its own — it
   quietly makes a model ineligible for every confidential request — so the
   admin API has to catch it at write time.
"""

from __future__ import annotations

import pytest

from core.llm_provider_registry import (
    PRIVACY_DEPLOYMENT_LOCAL,
    PRIVACY_EXTERNAL,
    derive_privacy_class,
)


# ── privacy_class derivation ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "family, base_url",
    [
        ("ollama", None),
        ("ollama", "http://ollama:11434"),
        ("ollama", "https://example.com"),          # family alone is decisive
        ("openai_compatible", "http://localhost:4000"),
        ("openai_compatible", "http://127.0.0.1:8000"),
        ("openai_compatible", "http://192.168.1.50:8000"),
        ("openai_compatible", "http://10.0.0.7:4000"),
        ("openai_compatible", "http://litellm:4000"),       # bare service name
        ("openai_compatible", "http://vllm.internal:8000"),
        ("openai_compatible", "http://gpu01.cluster.local:4000"),
    ],
)
def test_deployment_local_is_recognised(family: str, base_url: str | None) -> None:
    assert derive_privacy_class(family, base_url) == PRIVACY_DEPLOYMENT_LOCAL


@pytest.mark.parametrize(
    "family, base_url",
    [
        ("anthropic", None),
        ("openai", None),
        ("gemini", None),
        ("anthropic", "https://api.anthropic.com"),
        ("openai_compatible", "https://openrouter.ai/api/v1"),
        ("openai_compatible", "https://api.together.xyz/v1"),
    ],
)
def test_external_is_recognised(family: str, base_url: str | None) -> None:
    assert derive_privacy_class(family, base_url) == PRIVACY_EXTERNAL


@pytest.mark.parametrize(
    "family, base_url",
    [
        ("openai_compatible", None),     # ambiguous family, no URL to judge by
        ("openai_compatible", ""),
        ("", None),                      # unknown family
        ("something-new", "https://x.example"),
        ("openai_compatible", "not-a-url"),
        (None, None),                    # type: ignore[arg-type]
    ],
)
def test_unclassifiable_fails_safe_to_external(family, base_url) -> None:
    """Never ``deployment_local`` on a guess.

    Under-permitting fails a request visibly; over-permitting leaks data
    silently. An unclassified model must not satisfy no-cloud-egress.
    """
    assert derive_privacy_class(family, base_url) == PRIVACY_EXTERNAL


def test_derivation_never_raises() -> None:
    """Runs during migration and admin sync — must not break either."""
    for bad in [object(), 123, [], {}]:
        assert derive_privacy_class("openai_compatible", bad) == PRIVACY_EXTERNAL  # type: ignore[arg-type]


# ── Capability validation ────────────────────────────────────────────────────


def _validate(caps: dict):
    from routers.llm_provider_admin_router import _validate_capabilities

    return _validate_capabilities(caps)


def test_valid_capabilities_pass_through_unchanged() -> None:
    caps = {
        "context_window": 200_000,
        "reserved_output": 8_000,
        "max_output_tokens": 64_000,
        "cost_per_1m_input": 3.0,
        "cost_per_1m_output": 15.0,
        "cost_per_second": 0.4,
        "supports_temperature": False,
        "supports_tools": True,
        "requires_tools": False,
        "modality": ["text", "image-in"],
        "privacy_class": "deployment_local",
        "billing_tier": "free",
        "channels": ["cli", "api"],
    }
    assert _validate(dict(caps)) == caps


def test_unknown_keys_are_preserved() -> None:
    """A provider may report anything; we validate, we do not filter."""
    caps = {"vendor_specific_thing": {"nested": True}, "context_window": 128_000}
    assert _validate(dict(caps)) == caps


def test_none_is_allowed() -> None:
    assert _validate(None) is None  # type: ignore[arg-type]


def test_bare_string_modality_accepted_for_backwards_compat() -> None:
    """Rows seeded before Phase 2 stored e.g. "video"; the UI still reads it.

    ai-ui Chat.jsx and KbChat.jsx branch on `modality === "video"`, so
    rejecting the scalar form would break existing rows.
    """
    assert _validate({"modality": "video"}) == {"modality": "video"}


@pytest.mark.parametrize(
    "caps, reason",
    [
        ({"privacy_class": "deployment-local"}, "hyphen instead of underscore"),
        ({"privacy_class": "local"}, "not one of the two values"),
        ({"modality": ["text", "holograph"]}, "unknown modality"),
        ({"context_window": -1}, "negative window"),
        ({"context_window": 0}, "zero window"),
        ({"context_window": "200k"}, "string window"),
        ({"context_window": True}, "bool is not an int here"),
        ({"cost_per_1m_input": -0.5}, "negative cost"),
        ({"supports_tools": "yes"}, "string instead of bool"),
        ({"billing_tier": "cheap"}, "not paid/free"),
        ({"channels": "cli"}, "string instead of list"),
        ({"channels": [1, 2]}, "non-string channel"),
    ],
)
def test_malformed_governed_keys_are_rejected(caps: dict, reason: str) -> None:
    with pytest.raises(ValueError):
        _validate(caps)


# ── Merge semantics: never clobber an admin edit ─────────────────────────────


def test_sync_backfill_does_not_overwrite_admin_privacy_class() -> None:
    """Re-syncing a provider must not undo a manual privacy_class correction.

    ``openai_compatible`` is derived from the base_url, which is a guess — the
    operator is the only one who actually knows whether their endpoint is
    on-prem. This models the setdefault() the sync path relies on.
    """
    admin_edited = {"privacy_class": "deployment_local", "context_window": 32_000}
    discovered = {"context_window": 32_000}

    merged = {**admin_edited, **{k: v for k, v in discovered.items() if v is not None}}
    merged.setdefault("privacy_class", PRIVACY_EXTERNAL)

    assert merged["privacy_class"] == "deployment_local"


def test_sync_backfill_fills_missing_privacy_class() -> None:
    existing = {"context_window": 32_000}
    merged = {**existing}
    merged.setdefault("privacy_class", PRIVACY_EXTERNAL)
    assert merged["privacy_class"] == PRIVACY_EXTERNAL
