# SPDX-License-Identifier: MIT
"""D103 — Phase 8 prep: one list of the env vars Phase 8 removes.

Nothing is removed here. The list drives the startup warning, doctor.sh and a
release-check ratchet, so the deprecation an operator is told about and the
one CI enforces are the same set.
"""

from __future__ import annotations

import logging
import pathlib
import sys

import pytest

import core.legacy_env as le

ROOT = pathlib.Path(__file__).resolve().parents[2]

# §I.5 Keep rows plus the explicit exceptions; none may ever be listed.
KEEP = {
    "LLM_PROXY_URL", "LLM_TIMEOUT_SEC", "LOCAL_LLM_BASE_URL", "LOCAL_LLM_API_KEY", "OPENAI_BASE_URL",
    "OLLAMA_URL", "LOCAL_LLM_TEMPERATURE", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
    "GOOGLE_API_KEY", "LLM_PROVIDER", "PRIVACY_FLOOR_ENFORCE", "CONTEXT_SIZE_ROUTING",
    "CONTEXT_FIT_FRACTION", "PIPELINE_V2", "PIPELINE_V2_ROUTING", "CIL_MODEL_ROUTING",
    "ENABLE_RAW_OPENAI_API", "LOCAL_HIDDEN_MODELS", "LOCAL_MODEL_REFRESH_SECS",
    "LOCAL_MODEL_IDS", "LOCAL_MODEL_IDS_DEFAULT", "LOCAL_MODEL_IDS_REPLACE",
    "MODEL_CONTEXT_CONFIG", "OPENAI_EMBED_MODEL", "TIER_GOVERNANCE_ENABLED",
}


def test_no_kept_variable_is_listed():
    assert not KEEP & set(le.PHASE8_REMOVED_VARS)


def test_the_list_has_no_duplicates():
    assert len(le.PHASE8_REMOVED_VARS) == len(set(le.PHASE8_REMOVED_VARS))


def test_only_set_variables_are_reported():
    env = {"OPENAI_CODING_MODEL": "gpt-x", "VEO_ENABLED": "true", "CLAUDE_HAIKU": "  ",
           "LLM_PROXY_URL": "http://proxy"}
    assert le.legacy_vars_set(env) == ["OPENAI_CODING_MODEL", "VEO_ENABLED"]


def test_the_warning_fires_once(monkeypatch, caplog):
    monkeypatch.setattr(le, "_warned", False, raising=True)
    env = {"OPENAI_CODING_MODEL": "gpt-x"}
    with caplog.at_level(logging.WARNING):
        le.warn_legacy_env_once(env)
        le.warn_legacy_env_once(env)
    hits = [r for r in caplog.records if "Phase 8" in r.getMessage()]
    assert len(hits) == 1 and "OPENAI_CODING_MODEL" in hits[0].getMessage()


def test_nothing_is_logged_when_none_are_set(monkeypatch, caplog):
    monkeypatch.setattr(le, "_warned", False, raising=True)
    with caplog.at_level(logging.WARNING):
        assert le.warn_legacy_env_once({}) == []
    assert not [r for r in caplog.records if "Phase 8" in r.getMessage()]


def test_the_gateway_warns_at_startup():
    src = (ROOT / "gateway.py").read_text(encoding="utf-8")
    start = src.index("async def startup():")
    assert "warn_legacy_env_once()" in src[start:start + 1500]


# ── the release-check ratchet ───────────────────────────────────────────────


@pytest.fixture
def rc(monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts" / "ci"))
    import release_checks
    yield release_checks
    sys.path.remove(str(ROOT / "scripts" / "ci"))


def test_the_ratchet_reads_the_same_list(rc):
    assert rc._phase8_vars() == list(le.PHASE8_REMOVED_VARS)


def test_the_ratchet_catches_a_new_reference(rc, monkeypatch):
    monkeypatch.setattr(rc, "legacy_env_refs", lambda: {"x.py": rc._LEGACY_ENV_REF_BASELINE + 1}, raising=True)
    assert rc.check_legacy_env_refs({})
    monkeypatch.setattr(rc, "legacy_env_refs", lambda: {"x.py": rc._LEGACY_ENV_REF_BASELINE}, raising=True)
    assert rc.check_legacy_env_refs({}) == []


def test_the_baseline_is_the_measured_count(rc):
    """Raising the baseline would hide a new reference; lower it when one goes."""
    assert sum(rc.legacy_env_refs().values()) == rc._LEGACY_ENV_REF_BASELINE
