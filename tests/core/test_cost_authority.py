# SPDX-License-Identifier: MIT
"""D105 — one cost authority, reading the price recorded on the model row.

Before Phase 8 about twenty readers looked up MODEL_COST_PER_1M, a table keyed
by the env vars Phase 8 deletes, and disagreed about an unknown model: (0,0),
(2,8) or (3,15). Now every reader asks core.model_registry.rates_for, and
migration Part AE1 carries each model's .env-era price onto its row once.
"""

from __future__ import annotations

import pathlib
import re
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _row(model_id, family="anthropic", **caps):
    return {"model_id": model_id, "family": family, "capabilities": caps}


@pytest.fixture
def registry(monkeypatch):
    import core.llm_provider_registry as reg
    import gateway_local_llm as glm
    rows: dict = {}
    monkeypatch.setattr(reg, "get_model", lambda mid: rows.get(mid), raising=True)
    monkeypatch.setattr(reg, "get_enabled_models", lambda channel=None: list(rows.values()), raising=True)
    monkeypatch.setattr(glm, "is_local_model", lambda mid: mid == "served-locally", raising=True)
    return rows


def test_a_recorded_price_wins(registry):
    from core.model_registry import rates_for, price_of
    registry["claude-x"] = _row("claude-x", cost_per_1m_input=15.0, cost_per_1m_output=75.0)
    assert rates_for("claude-x") == (15.0, 75.0) == price_of("claude-x")


@pytest.mark.parametrize("row", [
    _row("llama-x:1b", family="ollama"),
    _row("hosted-free", family="openai_compatible", billing_tier="free"),
])
def test_local_and_free_rows_cost_nothing(registry, row):
    from core.model_registry import rates_for
    registry[row["model_id"]] = row
    assert rates_for(row["model_id"]) == (0.0, 0.0)


def test_an_unpriced_paid_model_over_bills(registry):
    from core.model_registry import rates_for, price_of, UNPRICED_RATES
    registry["gpt-x"] = _row("gpt-x", family="openai")
    assert rates_for("gpt-x") == UNPRICED_RATES
    assert price_of("gpt-x") is None


def test_a_display_label_resolves_to_the_id_inside_it(registry):
    from core.model_registry import rates_for
    registry["claude-x"] = _row("claude-x", cost_per_1m_input=3.0, cost_per_1m_output=15.0)
    registry["claude-x-mini"] = _row("claude-x-mini", cost_per_1m_input=1.0, cost_per_1m_output=2.0)
    assert rates_for("Claude X Mini (claude-x-mini)") == (1.0, 2.0)


@pytest.mark.parametrize("model", ["local:anything", "Local (In-house) (kimi)", "served-locally"])
def test_unregistered_local_models_are_free(registry, model):
    from core.model_registry import rates_for
    assert rates_for(model) == (0.0, 0.0)


def test_an_unknown_model_over_bills(registry):
    from core.model_registry import rates_for, price_of, UNPRICED_RATES
    assert rates_for("never-registered") == UNPRICED_RATES and price_of("never-registered") is None
    assert rates_for("") == UNPRICED_RATES


def test_estimate_cost_uses_the_authority(registry):
    import gateway
    registry["claude-x"] = _row("claude-x", cost_per_1m_input=3.0, cost_per_1m_output=15.0)
    assert gateway._estimate_cost("claude-x", 1_000_000, 1_000_000) == pytest.approx(18.0)


# ── Part AE1 ─────────────────────────────────────────────────────────────────


def test_env_prices_resolve_like_the_old_table():
    from db.phase8_env_prices import env_prices as _ae1_env_prices
    env = {"CLAUDE_PRIMARY_MODEL": "claude-sonnet-4-6", "CLAUDE_OPUS_MODEL": "claude-opus-4-7",
           "OPENAI_CODING_MODEL": "gpt-5.4", "CLAUDE_HAIKU": ""}
    assert _ae1_env_prices(env) == {
        "claude-sonnet-4-6": (3.00, 15.00), "claude-opus-4-7": (15.00, 75.00), "gpt-5.4": (2.50, 15.00)}


def test_a_later_entry_wins_as_in_the_old_dict_literal():
    from db.phase8_env_prices import env_prices as _ae1_env_prices
    env = {"CLAUDE_PRIMARY_MODEL": "same-id", "CLAUDE_SONNET_5_MODEL": "same-id", "CLAUDE_OPUS_MODEL": "x"}
    env["CLAUDE_OPUS_5_MODEL"] = "same-id"
    assert _ae1_env_prices(env)["same-id"] == (3.00, 15.00)   # CLAUDE_SONNET_5_MODEL is listed last


class _Session:
    def __init__(self, rows):
        self.rows, self.committed = rows, False

    def query(self, _model):
        return types.SimpleNamespace(all=lambda: self.rows)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


def _backfill(monkeypatch, rows, env):
    import db.database
    import db.migrate as mig
    session = _Session(rows)
    monkeypatch.setattr(db.database, "SessionLocal", lambda: session, raising=True)
    mig._part_ae1_price_backfill_2026_10_01(env)
    return session


def test_the_backfill_prices_only_unpriced_rows(monkeypatch):
    rows = [types.SimpleNamespace(model_id="claude-sonnet-4-6", capabilities={"modality": ["text"]}),
            types.SimpleNamespace(model_id="claude-opus-4-7",
                                  capabilities={"cost_per_1m_input": 9.0, "cost_per_1m_output": 9.0}),
            types.SimpleNamespace(model_id="unrelated", capabilities={})]
    s = _backfill(monkeypatch, rows, {"CLAUDE_PRIMARY_MODEL": "claude-sonnet-4-6",
                                      "CLAUDE_OPUS_MODEL": "claude-opus-4-7"})
    assert s.committed
    assert rows[0].capabilities == {"modality": ["text"], "cost_per_1m_input": 3.0, "cost_per_1m_output": 15.0}
    assert rows[1].capabilities == {"cost_per_1m_input": 9.0, "cost_per_1m_output": 9.0}   # admin price kept
    assert rows[2].capabilities == {}


def test_a_pinned_veo_model_gets_the_flat_rate(monkeypatch):
    rows = [types.SimpleNamespace(model_id="veo-x", capabilities={})]
    _backfill(monkeypatch, rows, {"VEO_MODEL": "veo-x", "VEO_COST_PER_SECOND": "0.5"})
    assert rows[0].capabilities == {"cost_per_second": 0.5}


def test_nothing_set_means_nothing_touched(monkeypatch):
    import db.database
    monkeypatch.setattr(db.database, "SessionLocal", lambda: pytest.fail("opened a session"), raising=True)
    import db.migrate as mig
    mig._part_ae1_price_backfill_2026_10_01({})


def test_the_backfill_runs_with_the_other_parts():
    src = (ROOT / "db" / "migrate.py").read_text(encoding="utf-8")
    body = src[src.index("def run_migrations():"):src.index("def _part_ac1_sdlc_governance_ledger_drift")]
    assert "_part_ae1_price_backfill_2026_10_01()" in body


# ── no reader bypasses the authority ─────────────────────────────────────────


def test_no_application_module_reads_the_old_tables():
    pattern = re.compile(r"\bMODEL_COST_PER_(1M|SECOND)\b")
    offenders = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "docs/", "services/llm_proxy/")) or "node_modules" in rel:
            continue
        code = "\n".join(l for l in path.read_text(encoding="utf-8", errors="ignore").splitlines()
                         if not l.lstrip().startswith(("#", "--")))
        if pattern.search(code):
            offenders.append(rel)
    assert offenders == []
