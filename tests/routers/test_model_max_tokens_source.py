# SPDX-License-Identifier: MIT
"""D99–D102 — every reader of a model's output limit reads the registry row.

Before Rev 21 there were three hand-kept sources: a 23-key JS table with a
4096 default, `MODEL_MAX_OUTPUT_TOKENS` (one entry, keyed by an env var that is
blank on the reference deployment) and a 17-family `reserved_output` table in
config/model_context_windows.json. An unknown model now gets the platform
limit, never a smaller guess.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _row(model_id, **caps):
    return {"model_id": model_id, "capabilities": caps}


@pytest.fixture
def registry(monkeypatch):
    import core.llm_provider_registry as reg
    rows: dict = {}
    monkeypatch.setattr(reg, "get_model", lambda mid: rows.get(mid), raising=True)
    return rows


# ── CLI catalogue (D101) ────────────────────────────────────────────────────


def test_the_cli_is_served_the_registry_ceiling():
    from routers.messages_compat_router import _finish_list_models_compat
    out = _finish_list_models_compat([
        {"id": "with-limit", "provider": "claude", "max_output_tokens": 64000},
        {"id": "without", "provider": "claude"},
    ])
    by_id = {m["id"]: m for m in out["data"]}
    assert by_id["with-limit"]["max_completion_tokens"] == 64000
    assert "max_completion_tokens" not in by_id["without"]


def test_the_catalogue_carries_the_limit(monkeypatch):
    import core.llm_provider_registry as reg
    monkeypatch.setattr(reg, "get_enabled_models", lambda channel=None: [{
        "model_id": "m", "family": "anthropic", "display_name": "M", "provider_name": "P",
        "capabilities": {"max_output_tokens": 32000},
    }], raising=True)
    (entry,) = reg.get_cli_style_models()
    assert entry["max_output_tokens"] == 32000


def test_no_per_model_token_table_remains_in_the_registry_module():
    tree = ast.parse((ROOT / "core" / "model_registry.py").read_text(encoding="utf-8"))
    names = {t.id for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))
             for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)}
    assert not [n for n in names if "MAX" in n and "TOKEN" in n]


# ── compaction reserve (D102) ───────────────────────────────────────────────


def test_an_admin_reserve_wins(registry):
    import gateway
    registry["m"] = _row("m", reserved_output=3000, max_output_tokens=64000)
    assert gateway._reserved_output_for("m") == 3000


def test_otherwise_the_provider_limit_is_the_reserve(registry):
    import gateway
    registry["m"] = _row("m", max_output_tokens=64000)
    registry["llama-x:1b"] = _row("llama-x:1b", max_output_tokens=131072)
    assert gateway._reserved_output_for("m") == 64000
    assert gateway._reserved_output_for("local:llama-x:1b") == 131072


def test_an_unknown_model_gets_the_platform_reserve(registry):
    import gateway
    assert gateway._reserved_output_for("sonnet") == gateway._PLATFORM_RESERVED_OUTPUT
    assert gateway._reserved_output_for("") == gateway._PLATFORM_RESERVED_OUTPUT


def test_the_context_config_holds_windows_only():
    import gateway
    data = json.loads((ROOT / "config" / "model_context_windows.json").read_text(encoding="utf-8"))
    assert "reserved_output" not in data
    assert gateway._load_context_config() == {k: int(v) for k, v in data["context_windows"].items()}
    assert not hasattr(gateway, "_DEFAULT_RESERVED_OUTPUT")


@pytest.mark.parametrize("window", [128_000, 131_072, 200_000])
@pytest.mark.parametrize("reserve", [2_000, 8_000, 64_000, 128_000])
def test_compaction_is_unchanged_up_to_200k(monkeypatch, window, reserve):
    """trigger = max(150000, window*0.75 - reserve): the floor wins at <= 200k."""
    import gateway
    monkeypatch.setattr(gateway, "_context_window_for", lambda _h: window, raising=True)
    monkeypatch.setattr(gateway, "_reserved_output_for", lambda _h: reserve, raising=True)
    assert gateway._usable_history_budget("m", 150_000) == 150_000


# ── local completions (D99/D102) ────────────────────────────────────────────


def test_an_unknown_local_model_may_use_what_fits(registry, monkeypatch):
    import gateway
    import gateway_local_llm as g
    monkeypatch.setattr(gateway, "_context_window_for", lambda _h: 131_072, raising=True)
    expected = 131_072 - 1_000 - g._MAX_TOKENS_SAFETY_MARGIN
    assert g._resolve_max_tokens("llama-x:1b", 1_000, None) == expected


def test_a_recorded_local_figure_is_honoured(registry, monkeypatch):
    import gateway
    import gateway_local_llm as g
    monkeypatch.setattr(gateway, "_context_window_for", lambda _h: 131_072, raising=True)
    registry["llama-x:1b"] = _row("llama-x:1b", reserved_output=6_000)
    assert g._resolve_max_tokens("llama-x:1b", 1_000, None) == 6_000
    assert g._resolve_max_tokens("llama-x:1b", 1_000, 500) == 500


# ── the platform limit the editors fall back to (D99) ───────────────────────


def _llm_config_limit() -> int:
    tree = ast.parse((ROOT / "AgentStudio" / "backend" / "app" / "models.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LLMConfig")
    field = next(n for n in cls.body if isinstance(n, ast.AnnAssign) and n.target.id == "max_tokens")
    return next(k.value.value for k in field.value.keywords if k.arg == "le")


def test_the_editors_pre_load_value_is_the_backend_limit():
    js = (ROOT / "AgentStudio" / "frontend" / "src" / "utils" / "modelMaxTokens.js").read_text(encoding="utf-8")
    m = re.search(r"PLATFORM_LIMIT_BEFORE_LOAD\s*=\s*(\d+)", js)
    assert m and int(m.group(1)) == _llm_config_limit()


def test_the_catalogue_serves_the_backend_limit():
    src = (ROOT / "AgentStudio" / "backend" / "app" / "api" / "generation.py").read_text(encoding="utf-8")
    assert '"max_tokens_limit": max_tokens_limit()' in src
    assert 'LLMConfig.model_fields["max_tokens"]' in src


def test_an_unreachable_registry_still_means_what_fits(monkeypatch):
    import sys
    import gateway_local_llm as g
    monkeypatch.setitem(sys.modules, "gateway", None)
    assert g._desired_output_tokens("llama-x:1b") is None
