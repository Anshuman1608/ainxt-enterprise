# SPDX-License-Identifier: MIT
# ============================================================
# core/feature_model_resolver.py — the precedence chain.
#
# Every migrated call site routes through resolve_feature_model(), so the two
# properties that matter most are:
#
#   1. It NEVER raises and NEVER returns something worse than the caller's own
#      literal. A DB outage, a deleted model, a disabled provider, an unknown
#      feature — all must degrade to `default`, because this sits on the path
#      of every LLM call in the platform.
#   2. The precedence order is exactly as documented, in particular that the
#      privacy floor cannot be overridden by feature config.
#
# The config store is stubbed throughout: these are unit tests of the
# resolution logic, not of Postgres or Redis.
# ============================================================

from __future__ import annotations

import pytest

import core.feature_model_resolver as fmr

# Captured at import time, before the autouse fixture below stubs the module
# attribute, so one test can exercise the genuine cache -> DB -> {} path.
_real_get_all_config = fmr.get_all_config


@pytest.fixture(autouse=True)
def no_store(monkeypatch: pytest.MonkeyPatch):
    """Empty config store by default, and no leaked break-glass env vars."""
    monkeypatch.setattr(fmr, "get_all_config", lambda: {})
    for key in list(fmr.os.environ):
        if key.startswith("AINXT_FEATURE_MODEL_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PRIVACY_FLOOR_ENFORCE", "true")


def _store(monkeypatch: pytest.MonkeyPatch, rows: dict) -> None:
    monkeypatch.setattr(fmr, "get_all_config", lambda: rows)


def _row(**kw) -> dict:
    base = {"model_id": None, "fallback_model_ids": [], "capability_override": None,
            "enabled": True}
    base.update(kw)
    return base


def _registry(monkeypatch: pytest.MonkeyPatch, by_uuid: dict) -> None:
    import core.llm_provider_registry as reg
    monkeypatch.setattr(reg, "get_model_by_uuid", lambda pk: by_uuid.get(pk))


# ── env var naming ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("feature_key, expected", [
    ("skills.generate",  "AINXT_FEATURE_MODEL_SKILLS_GENERATE"),
    ("sdlc.code_review", "AINXT_FEATURE_MODEL_SDLC_CODE_REVIEW"),
    ("mcp.document_revise", "AINXT_FEATURE_MODEL_MCP_DOCUMENT_REVISE"),
    ("a.b.c",            "AINXT_FEATURE_MODEL_A_B_C"),
])
def test_env_var_naming(feature_key: str, expected: str) -> None:
    assert fmr.env_var_for(feature_key) == expected


# ── precedence, lowest rung upward ───────────────────────────────────────────

def test_unknown_feature_returns_the_call_sites_literal() -> None:
    # Rung 6. This is what makes the call-site migration safe to land
    # file-by-file: an unmigrated feature behaves exactly as before.
    assert fmr.resolve_feature_model("no.such.feature", default="complex") == "complex"
    assert fmr.explain("no.such.feature", default="complex")["rule"] == "call_site"


def test_feature_with_no_declared_default_returns_the_literal() -> None:
    # ide.complete is deliberately seeded with default_capability=None because
    # its call sites use "medium" (OpenAI), which has no capability equivalent.
    assert fmr.resolve_feature_model("ide.complete", default="medium") == "medium"
    assert fmr.explain("ide.complete", default="medium")["rule"] == "call_site"


def test_declared_default_capability_beats_the_literal() -> None:
    # Rung 5. "balanced" resolves to the same tier as "complex", so this is a
    # zero-behaviour-change substitution — see test_feature_registry.py.
    assert fmr.resolve_feature_model("skills.generate", default="complex") == "balanced"
    assert fmr.explain("skills.generate", default="complex")["rule"] == "feature_default"


def test_platform_wide_assignment_beats_the_declared_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Rung 4.
    _registry(monkeypatch, {"uuid-1": {"id": "uuid-1", "model_id": "my-openrouter-model"}})
    _store(monkeypatch, {"skills.generate\x00default": _row(model_id="uuid-1")})

    assert fmr.resolve_feature_model("skills.generate", default="complex") == "my-openrouter-model"
    assert fmr.explain("skills.generate", default="complex")["rule"] == "default_assignment"


def test_org_assignment_beats_the_platform_wide_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Rung 3.
    _registry(monkeypatch, {
        "uuid-shared": {"id": "uuid-shared", "model_id": "shared-model"},
        "uuid-acme":   {"id": "uuid-acme",   "model_id": "acme-model"},
    })
    _store(monkeypatch, {
        "skills.generate\x00default": _row(model_id="uuid-shared"),
        "skills.generate\x00acme":    _row(model_id="uuid-acme"),
    })

    assert fmr.resolve_feature_model("skills.generate", org_id="acme") == "acme-model"
    assert fmr.resolve_feature_model("skills.generate", org_id="other") == "shared-model"
    assert fmr.explain("skills.generate", org_id="acme")["rule"] == "org_assignment"


def test_env_break_glass_beats_every_assignment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Rung 2 — an operator must be able to reroute mid-incident with no DB access.
    _registry(monkeypatch, {"uuid-1": {"id": "uuid-1", "model_id": "assigned-model"}})
    _store(monkeypatch, {"skills.generate\x00default": _row(model_id="uuid-1")})
    monkeypatch.setenv("AINXT_FEATURE_MODEL_SKILLS_GENERATE", "emergency-model")

    assert fmr.resolve_feature_model("skills.generate", default="complex") == "emergency-model"
    assert fmr.explain("skills.generate")["rule"] == "env"


@pytest.mark.parametrize("classification", ["CONFIDENTIAL", "RESTRICTED", "PCI_SENSITIVE"])
def test_privacy_floor_beats_everything_including_break_glass(
    classification: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Rung 1, the hard invariant. Feature config must not be able to send
    # CONFIDENTIAL+ data to a cloud provider, and neither must the env override.
    _registry(monkeypatch, {"uuid-1": {"id": "uuid-1", "model_id": "some-cloud-model"}})
    _store(monkeypatch, {"chat.respond\x00default": _row(model_id="uuid-1")})
    monkeypatch.setenv("AINXT_FEATURE_MODEL_CHAT_RESPOND", "another-cloud-model")

    hint = fmr.resolve_feature_model(
        "chat.respond", default="complex", data_classification=classification,
    )
    assert hint == "local-only"
    assert fmr.explain("chat.respond", data_classification=classification)["rule"] == "privacy_floor"


@pytest.mark.parametrize("classification", ["PUBLIC", "INTERNAL", None, ""])
def test_non_sensitive_classifications_do_not_trigger_the_floor(
    classification, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry(monkeypatch, {"uuid-1": {"id": "uuid-1", "model_id": "cloud-model"}})
    _store(monkeypatch, {"chat.respond\x00default": _row(model_id="uuid-1")})

    assert fmr.resolve_feature_model(
        "chat.respond", default="complex", data_classification=classification,
    ) == "cloud-model"


# ── capability override ──────────────────────────────────────────────────────

def test_capability_override_is_returned_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    _store(monkeypatch, {"skills.generate\x00default": _row(capability_override="expert")})
    assert fmr.resolve_feature_model("skills.generate", default="complex") == "expert"


# ── degradation: the property that keeps this off the critical path ──────────

def test_a_disabled_assignment_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _registry(monkeypatch, {"uuid-1": {"id": "uuid-1", "model_id": "assigned-model"}})
    _store(monkeypatch, {"skills.generate\x00default": _row(model_id="uuid-1", enabled=False)})

    # Falls through to the declared default, not to the disabled assignment.
    assert fmr.resolve_feature_model("skills.generate", default="complex") == "balanced"


def test_a_deleted_model_falls_through_instead_of_stranding_the_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The FK is ON DELETE SET NULL and a provider can be disabled at any time,
    # so the assignment can outlive the model it names.
    _registry(monkeypatch, {})   # nothing resolves
    _store(monkeypatch, {"skills.generate\x00default": _row(model_id="uuid-gone")})

    assert fmr.resolve_feature_model("skills.generate", default="complex") == "balanced"


def test_fallback_chain_is_walked_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    # Admin-added models get no cross-vendor fallback from the router's built-in
    # tiers, so this cascade is the only fallback they have.
    _registry(monkeypatch, {"uuid-3": {"id": "uuid-3", "model_id": "third-choice"}})
    _store(monkeypatch, {"skills.generate\x00default": _row(
        model_id="uuid-1", fallback_model_ids=["uuid-2", "uuid-3"],
    )})

    assert fmr.resolve_feature_model("skills.generate", default="complex") == "third-choice"


def test_resolution_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom():
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(fmr, "get_all_config", _boom)
    assert fmr.resolve_feature_model("skills.generate", default="complex") == "complex"


def test_config_load_failure_degrades_to_no_assignments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment running this code before migrate.py must still serve turns.

    Exercises the real get_all_config (the autouse fixture stubs it for every
    other case) with both the cache and the DB unavailable — the shape of "code
    deployed, table not created yet".
    """
    def _boom():
        raise RuntimeError('relation "feature_model_config" does not exist')

    monkeypatch.setattr(fmr, "get_all_config", _real_get_all_config)
    monkeypatch.setattr(fmr, "_read_cache", lambda: None)
    monkeypatch.setattr(fmr, "_load_from_db", _boom)

    assert fmr.get_all_config() == {}
    # And a resolution on top of that still hands back the caller's literal.
    assert fmr.resolve_feature_model("ide.complete", default="medium") == "medium"


def test_get_config_prefers_the_org_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _store(monkeypatch, {
        "f\x00default": _row(capability_override="fast"),
        "f\x00acme":    _row(capability_override="expert"),
    })
    assert fmr.get_config("f", "acme")["capability_override"] == "expert"
    assert fmr.get_config("f", "nobody")["capability_override"] == "fast"
    assert fmr.get_config("missing", "acme") is None


def test_every_precedence_rule_has_a_human_label() -> None:
    # The admin UI renders these directly; a missing one would show a raw
    # identifier where an explanation belongs.
    for rule in ("privacy_floor", "env", "org_assignment", "default_assignment",
                 "feature_default", "call_site"):
        assert fmr.PRECEDENCE_LABELS.get(rule), rule
