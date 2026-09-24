# SPDX-License-Identifier: MIT
# ============================================================
# TIER_REGISTRY resilience — circuit breaker + cross-vendor fallback.
#
# TIER_REGISTRY (admin-registered models, reached when route() resolves a
# model_id from the provider registry) was the ONE tier with neither. Its own
# source comment said so: "No cross-vendor fallback (unlike every other tier
# above)". An admin-added model that failed simply returned an error string to
# the user, while every built-in tier cascaded across vendors.
#
# That made each per-feature assignment onto an admin-added model a new single
# point of failure, which is the risk that made per-feature assignment unsafe
# to roll out widely. These tests pin the fix.
#
# core.circuit_breaker needed no change — get_breaker() already accepts an
# arbitrary name with (10, 30) defaults. The gap was only that this path never
# called it.
# ============================================================

from __future__ import annotations

from collections.abc import Iterator

import pytest

from models.model_router import TIER_REGISTRY, ModelRouter


class _OkGateway:
    def __init__(self, text: str = "PRIMARY_OK"):
        self.text = text
        self.calls: list[str] = []

    def generate(self, prompt: str, *, model: str) -> Iterator[str]:
        del prompt
        self.calls.append(model)
        yield self.text


class _RaisingGateway:
    """Fails by RAISING — one of the two ways gateways signal failure."""

    def __init__(self):
        self.calls: list[str] = []

    def generate(self, prompt: str, *, model: str) -> Iterator[str]:
        del prompt
        self.calls.append(model)
        raise RuntimeError("upstream 503")
        yield ""   # pragma: no cover — makes this a generator


class _ErrorStringGateway:
    """Fails by RETURNING 'Error: ...' — the other way, which a breaker
    wrapping the call cannot see on its own (audit §3.8: 'Plain string
    returned')."""

    def __init__(self):
        self.calls: list[str] = []

    def generate(self, prompt: str, *, model: str) -> Iterator[str]:
        del prompt
        self.calls.append(model)
        yield "Error: model is not available"


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> ModelRouter:
    """A router with the breaker disabled and no real providers behind it.

    CIRCUIT_BREAKER_DISABLED makes breaker.call() a passthrough, so these cases
    exercise the FALLBACK logic and not breaker accounting.

    It also has to be set, not merely convenient: breaker state lives in REDIS,
    keyed by breaker name, so it outlives the test process. Without this, the
    failures these tests deliberately provoke tripped the shared
    `registry:<model>` breaker and later tests found it already OPEN — which is
    exactly how this suite started failing the moment it ran on a machine with
    Redis up, having passed everywhere Redis was unavailable (is_open
    fails open).
    """
    monkeypatch.setenv("CIRCUIT_BREAKER_DISABLED", "1")
    r = ModelRouter()
    # No built-in providers, so the last-resort cascade has nothing to offer
    # unless a case wires one up.
    monkeypatch.setattr(r, "_get_openai", lambda: None)
    monkeypatch.setattr(r, "_get_claude", lambda: None)
    monkeypatch.setattr(r, "_get_local", lambda: None, raising=False)
    return r


def _no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.llm_provider_registry as reg
    monkeypatch.setattr(reg, "get_default_model_id", lambda *a, **k: None)


# ── the happy path is unchanged ──────────────────────────────────────────────

def test_a_working_registry_model_is_not_marked_as_a_fallback(
    router: ModelRouter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gw = _OkGateway()
    monkeypatch.setattr(router, "_resolve_registry_gateway",
                        lambda m: (gw, "openai_compatible"))

    result, was_fallback = router._try_registry("hi", provider_model="my-model")

    assert result == "PRIMARY_OK"
    assert was_fallback is False
    assert gw.calls == ["my-model"]
    assert router._last_actual_tier == TIER_REGISTRY


# ── both failure shapes fall back ────────────────────────────────────────────

@pytest.mark.parametrize("gateway_cls", [_RaisingGateway, _ErrorStringGateway])
def test_a_failing_registry_model_falls_back_to_the_configured_default(
    gateway_cls, router: ModelRouter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The admin's own global default from the LLM Providers screen is tried
    # before any built-in assumption — it is a stated operator preference.
    import core.llm_provider_registry as reg
    monkeypatch.setattr(reg, "get_default_model_id", lambda *a, **k: "fallback-model")

    broken, healthy = gateway_cls(), _OkGateway("FALLBACK_OK")
    monkeypatch.setattr(
        router, "_resolve_registry_gateway",
        lambda m: (healthy, "openai_compatible") if m == "fallback-model"
        else (broken, "openai_compatible"),
    )

    result, was_fallback = router._try_registry("hi", provider_model="broken-model")

    assert result == "FALLBACK_OK"
    # Marked as a fallback so the UI shows "[fallback]" and the turn is
    # attributed to the model that actually answered.
    assert was_fallback is True
    assert "[fallback]" in router.last_model_label
    assert healthy.calls == ["fallback-model"]


def test_an_error_string_is_treated_as_a_failure_not_an_answer(
    router: ModelRouter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Before the fix this string was returned to the user verbatim as if it
    # were the model's answer.
    _no_default(monkeypatch)
    monkeypatch.setattr(router, "_resolve_registry_gateway",
                        lambda m: (_ErrorStringGateway(), "openai_compatible"))
    monkeypatch.setattr(router, "_try_openai_coding",
                        lambda p, **kw: ("CASCADE_OK", False))

    result, was_fallback = router._try_registry("hi", provider_model="broken")

    assert result == "CASCADE_OK"
    assert was_fallback is True


def test_falls_through_to_the_builtin_cascade_when_the_default_also_fails(
    router: ModelRouter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.llm_provider_registry as reg
    monkeypatch.setattr(reg, "get_default_model_id", lambda *a, **k: "also-broken")
    monkeypatch.setattr(router, "_resolve_registry_gateway",
                        lambda m: (_RaisingGateway(), "openai_compatible"))
    # TIER_MEDIUM's own chain is openai -> claude -> local, the most resilient
    # built-in cascade, so it is the right last resort.
    monkeypatch.setattr(router, "_try_openai_coding",
                        lambda p, **kw: ("CASCADE_OK", False))

    result, was_fallback = router._try_registry("hi", provider_model="broken")

    assert result == "CASCADE_OK"
    assert was_fallback is True


def test_no_resolvable_gateway_falls_back_instead_of_erroring(
    router: ModelRouter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gemini/ollama families fall off the end of _resolve_registry_gateway, and
    # a provider can be disabled between resolution and dispatch.
    _no_default(monkeypatch)
    monkeypatch.setattr(router, "_resolve_registry_gateway", lambda m: (None, "gemini"))
    monkeypatch.setattr(router, "_try_openai_coding",
                        lambda p, **kw: ("CASCADE_OK", False))

    result, was_fallback = router._try_registry("hi", provider_model="unreachable")

    assert result == "CASCADE_OK"
    assert was_fallback is True


def test_the_default_model_is_not_retried_when_it_is_the_one_that_failed(
    router: ModelRouter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.llm_provider_registry as reg
    monkeypatch.setattr(reg, "get_default_model_id", lambda *a, **k: "the-model")

    broken = _RaisingGateway()
    monkeypatch.setattr(router, "_resolve_registry_gateway",
                        lambda m: (broken, "openai_compatible"))
    monkeypatch.setattr(router, "_try_openai_coding",
                        lambda p, **kw: ("CASCADE_OK", False))

    result, _ = router._try_registry("hi", provider_model="the-model")

    assert result == "CASCADE_OK"
    # Called once for the primary attempt only — not a second, pointless time
    # as its own fallback.
    assert broken.calls == ["the-model"]


# ── breaker wiring ───────────────────────────────────────────────────────────

def test_breakers_are_keyed_per_model_not_per_family() -> None:
    a = ModelRouter._registry_breaker("vendor-a/model-1")
    b = ModelRouter._registry_breaker("vendor-b/model-2")

    # One dead OpenRouter deployment must not open the breaker for every other
    # openai_compatible model.
    assert a is not b
    assert a.name != b.name
    assert a.name.startswith("registry:")
    # And must not collide with the built-in provider breakers.
    assert a.name not in ("openai", "claude", "gemini", "local")


def test_the_breaker_for_a_model_is_a_singleton() -> None:
    assert ModelRouter._registry_breaker("m") is ModelRouter._registry_breaker("m")


def test_an_open_breaker_short_circuits_to_the_fallback(
    router: ModelRouter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_default(monkeypatch)
    gw = _OkGateway()
    monkeypatch.setattr(router, "_resolve_registry_gateway",
                        lambda m: (gw, "openai_compatible"))

    # A stub rather than a real OPEN breaker, so the case does not depend on
    # Redis state shared with every other test in this file. call() is what
    # fast-fails in production (the router no longer pre-checks is_open, so
    # that CIRCUIT_BREAKER_DISABLED is honoured), so that is what is stubbed.
    class _OpenBreaker:
        name = "registry:x"

        def call(self, fn, *a, **kw):
            raise RuntimeError("CircuitBreaker[registry:x] is OPEN — fast-failing")

        def record_failure(self, exc=None):
            pass

    monkeypatch.setattr(router, "_registry_breaker", staticmethod(lambda m: _OpenBreaker()))
    monkeypatch.setattr(router, "_try_openai_coding",
                        lambda p, **kw: ("CASCADE_OK", False))

    result, was_fallback = router._try_registry("hi", provider_model="x")

    assert result == "CASCADE_OK"
    assert was_fallback is True
    # The broken model is not called at all while its breaker is open.
    assert gw.calls == []


# ── streaming ────────────────────────────────────────────────────────────────

def test_stream_falls_back_when_the_model_fails_before_the_first_token(
    router: ModelRouter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_default(monkeypatch)
    monkeypatch.setattr(router, "_resolve_registry_gateway",
                        lambda m: (_RaisingGateway(), "openai_compatible"))
    monkeypatch.setattr(router, "_try_openai_coding",
                        lambda p, **kw: ("CASCADE_OK", False))

    out = list(router._try_registry_stream("hi", provider_model="broken"))

    assert "".join(out) == "CASCADE_OK"


def test_stream_passes_tokens_through_unchanged_on_the_happy_path(
    router: ModelRouter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MultiToken:
        def generate(self, prompt, *, model):
            del prompt, model
            yield "one "
            yield "two "
            yield "three"

    monkeypatch.setattr(router, "_resolve_registry_gateway",
                        lambda m: (_MultiToken(), "openai_compatible"))

    # The first token is pulled eagerly to decide whether to fall back; it must
    # still be emitted, in order, exactly once.
    assert list(router._try_registry_stream("hi", provider_model="m")) == \
        ["one ", "two ", "three"]


def test_a_mid_stream_failure_is_reported_not_restarted(
    router: ModelRouter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BreaksAfterFirst:
        def generate(self, prompt, *, model):
            del prompt, model
            yield "partial answer"
            raise RuntimeError("connection reset")

    monkeypatch.setattr(router, "_resolve_registry_gateway",
                        lambda m: (_BreaksAfterFirst(), "openai_compatible"))
    called: list[str] = []
    monkeypatch.setattr(router, "_try_openai_coding",
                        lambda p, **kw: called.append("x") or ("CASCADE", False))

    out = list(router._try_registry_stream("hi", provider_model="m"))

    # Once tokens are flowing the turn is committed: restarting on another
    # model would replay output the user has already seen.
    assert out[0] == "partial answer"
    assert "Error" in out[-1]
    assert called == []
