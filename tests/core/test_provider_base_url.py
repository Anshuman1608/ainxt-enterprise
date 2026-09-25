# SPDX-License-Identifier: MIT
"""F3 — the admin-configured provider endpoint must win over the env var.

`llm_providers.base_url` used to be read on exactly one path: `get_client_for()`,
the registry dispatch used by openai_compatible models. The anthropic, openai
and gemini gateways each built their client from `os.getenv(...)` and consulted
the registry only for a CREDENTIAL. So an administrator who pointed one of
those providers at a regional gateway, an outbound proxy, or a compliance
egress had that setting silently discarded — the same class of defect as the
model-id env constants outranking the registry.

Precedence is deliberately admin-over-env: the provider row is a specific
choice made in the UI for that provider, while the env var is a
deployment-wide default.
"""

from __future__ import annotations

import pytest

import core.llm_provider_registry as reg


@pytest.fixture(autouse=True)
def _no_registry_cache(monkeypatch: pytest.MonkeyPatch):
    """Serve the registry from an in-memory list — no DB, no Redis."""
    models: list = []
    monkeypatch.setattr(reg, "get_enabled_models", lambda channel=None: models)
    return models


def _add(models: list, family: str, base_url: str | None) -> None:
    models.append({
        "id": f"m-{family}", "model_id": f"{family}-model",
        "display_name": family, "capabilities": {}, "is_default": False,
        "sort_order": 0, "provider_id": f"p-{family}", "provider_slug": family,
        "provider_name": family, "family": family, "base_url": base_url,
    })


# ── resolve_base_url_for_family ──────────────────────────────────────────────


def test_returns_configured_base_url(_no_registry_cache) -> None:
    _add(_no_registry_cache, "anthropic", "https://anthropic.eu.example/v1")
    assert reg.resolve_base_url_for_family("anthropic") == "https://anthropic.eu.example/v1"


def test_returns_none_when_provider_sets_no_base_url(_no_registry_cache) -> None:
    _add(_no_registry_cache, "anthropic", None)
    assert reg.resolve_base_url_for_family("anthropic") is None


def test_blank_base_url_is_treated_as_unset(_no_registry_cache) -> None:
    _add(_no_registry_cache, "openai", "   ")
    assert reg.resolve_base_url_for_family("openai") is None


def test_returns_none_for_unconfigured_family(_no_registry_cache) -> None:
    _add(_no_registry_cache, "openai", "https://x.example")
    assert reg.resolve_base_url_for_family("gemini") is None


def test_first_enabled_provider_wins(_no_registry_cache) -> None:
    """Same convention as resolve_credential_for_family."""
    _add(_no_registry_cache, "openai", "https://first.example")
    _no_registry_cache.append({**_no_registry_cache[0], "base_url": "https://second.example"})
    assert reg.resolve_base_url_for_family("openai") == "https://first.example"


# ── provider_base_url_or_env: the precedence rule ────────────────────────────


@pytest.mark.parametrize("family", ["anthropic", "openai", "gemini"])
def test_admin_config_beats_env(
    family: str, _no_registry_cache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE F3 FIX, for each of the three gateways that ignored it."""
    monkeypatch.setenv("SOME_BASE_URL", "https://from-env.example")
    _add(_no_registry_cache, family, "https://from-admin.example")
    assert reg.provider_base_url_or_env(family, "SOME_BASE_URL") == "https://from-admin.example"


def test_env_used_when_no_admin_config(_no_registry_cache, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_BASE_URL", "https://from-env.example")
    _add(_no_registry_cache, "openai", None)
    assert reg.provider_base_url_or_env("openai", "SOME_BASE_URL") == "https://from-env.example"


def test_none_when_neither_set(_no_registry_cache, monkeypatch: pytest.MonkeyPatch) -> None:
    """None means "let the SDK use its own default"."""
    monkeypatch.delenv("SOME_BASE_URL", raising=False)
    _add(_no_registry_cache, "openai", None)
    assert reg.provider_base_url_or_env("openai", "SOME_BASE_URL") is None


def test_registry_failure_degrades_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable DB must not stop a gateway constructing."""
    def _boom(channel=None):
        raise RuntimeError("database is down")

    monkeypatch.setattr(reg, "get_enabled_models", _boom)
    monkeypatch.setenv("SOME_BASE_URL", "https://from-env.example")
    assert reg.provider_base_url_or_env("openai", "SOME_BASE_URL") == "https://from-env.example"


def test_disagreement_is_logged(
    _no_registry_cache, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Silently swapping an endpoint is what caused this bug — say it out loud."""
    monkeypatch.setenv("SOME_BASE_URL", "https://from-env.example")
    _add(_no_registry_cache, "openai", "https://from-admin.example")
    with caplog.at_level("INFO"):
        reg.provider_base_url_or_env("openai", "SOME_BASE_URL")
    assert any("from-admin.example" in r.getMessage() for r in caplog.records)


# ── The gateway wrappers stay safe when the registry is unavailable ──────────


@pytest.mark.parametrize(
    "module_name",
    ["gateway_claude", "gateway_openai", "gateway_gemini"],
)
def test_gateway_wrapper_falls_back_to_env(
    module_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    # These modules construct a gateway singleton at import time, which needs
    # a provider key present. The keys are never used — the wrapper under test
    # is a pure function — but the import would otherwise raise.
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(var, "test-key-not-used")

    mod = importlib.import_module(module_name)
    monkeypatch.setenv("SOME_BASE_URL", "https://from-env.example")
    monkeypatch.setattr(
        reg, "get_enabled_models",
        lambda channel=None: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    assert mod._resolve_provider_base_url("openai", "SOME_BASE_URL") == "https://from-env.example"
