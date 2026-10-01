# SPDX-License-Identifier: MIT
"""Phase 8 (D109) helpers that replaced env reads: the temperature capability,
the LLM_PROVIDER=local assertion and a gateway's per-vendor tier pick."""

from __future__ import annotations

import pathlib
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _row(model_id, family="openai", **caps):
    return {"model_id": model_id, "family": family, "capabilities": caps}


@pytest.fixture
def registry(monkeypatch):
    import core.llm_provider_registry as reg
    rows: dict = {}
    monkeypatch.setattr(reg, "get_model", lambda mid: rows.get(mid), raising=True)
    monkeypatch.setattr(reg, "get_enabled_models", lambda channel=None: list(rows.values()), raising=True)
    return rows


# ── accepts_temperature ─────────────────────────────────────────────────────


def test_the_registry_capability_decides(registry):
    from core.model_registry import accepts_temperature
    registry["new-reasoner"] = _row("new-reasoner", supports_temperature=False)
    registry["o1-but-allowed"] = _row("o1-but-allowed", supports_temperature=True)
    assert accepts_temperature("new-reasoner") is False
    assert accepts_temperature("o1-but-allowed") is True


def test_without_a_capability_the_prefixes_decide(registry):
    from core.model_registry import accepts_temperature, models_without_temperature
    prefix = models_without_temperature()[0]
    assert accepts_temperature(prefix + "-x") is False
    assert accepts_temperature("plain-model") is True


# ── posture_violations ──────────────────────────────────────────────────────


def test_local_posture_names_every_cloud_model(registry, monkeypatch):
    import core.model_registry as mr
    registry["kimi"] = _row("kimi", "ollama", privacy_class="deployment_local")
    registry["gpt-x"] = _row("gpt-x")
    monkeypatch.setattr(mr, "LLM_PROVIDER", "local", raising=True)
    assert mr.posture_violations() == ["gpt-x"]
    monkeypatch.setattr(mr, "LLM_PROVIDER", "cloud", raising=True)
    assert mr.posture_violations() == []


def test_startup_runs_the_posture_check():
    src = (ROOT / "gateway.py").read_text(encoding="utf-8")
    assert "posture_violations()" in src


# ── family_model ────────────────────────────────────────────────────────────


def test_the_tier_candidate_from_the_family_wins(registry, monkeypatch):
    import core.tier_resolver as tr
    from core.tiers import Tier
    registry["claude-a"] = _row("claude-a", "anthropic")
    cands = [types.SimpleNamespace(model_id="gpt-x", family="openai"),
             types.SimpleNamespace(model_id="claude-b", family="anthropic")]
    monkeypatch.setattr(tr, "resolve_tier_candidates", lambda tier, *a, **kw: cands, raising=True)
    assert tr.family_model(Tier.COMPLEX, "anthropic") == "claude-b"


def test_an_unassigned_tier_falls_back_to_the_family(registry, monkeypatch):
    import core.tier_resolver as tr
    from core.tiers import Tier

    def _boom(tier, *a, **kw):
        raise tr.NoEligibleModel(Tier(tier), None, {})
    monkeypatch.setattr(tr, "resolve_tier_candidates", _boom, raising=True)
    registry["claude-a"] = _row("claude-a", "anthropic")
    assert tr.family_model(Tier.COMPLEX, "anthropic") == "claude-a"
    assert tr.family_model(Tier.COMPLEX, "gemini") == ""
