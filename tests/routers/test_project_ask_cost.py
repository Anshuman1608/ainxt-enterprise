# SPDX-License-Identifier: MIT
"""D78 — a project turn is priced by the platform's cost authority.

``projects_router`` decided a model was free by finding "ollama", "local" or
"llama" in its id — the §P ``_classify_model()`` defect in another file. So an
Ollama model named ``qwen2.5:7b`` was billed at the paid default, and a cloud
model with "llama" in its id ran free. It now asks
``services.endpoint_model_catalog.estimate_cost_usd``, which reads the
registry's family and billing tier.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def cost(monkeypatch):
    """The real helper, with the registry and the local catalogue pinned."""
    import core.llm_provider_registry as reg
    import core.model_registry as mreg
    import gateway_local_llm as glm

    rows = {
        "qwen2.5:7b": {"family": "ollama", "capabilities": {}},
        "meta-llama-4-maverick": {"family": "openai_compatible", "capabilities": {}},
        "priced-model": {"family": "openai", "capabilities": {}},
    }
    monkeypatch.setattr(reg, "get_model", lambda mid: rows.get(mid), raising=True)
    monkeypatch.setattr(glm, "is_local_model", lambda mid: False, raising=True)
    monkeypatch.setattr(mreg, "MODEL_COST_PER_1M", {"priced-model": (3.0, 15.0)}, raising=True)

    from routers.projects_router import _project_ask_cost
    return _project_ask_cost


def test_a_registry_ollama_model_is_free_whatever_its_name(cost):
    assert cost("qwen2.5:7b", 1_000_000, 1_000_000) == 0.0


def test_a_cloud_model_with_llama_in_its_id_is_billed(cost):
    assert cost("meta-llama-4-maverick", 1_000_000, 1_000_000) > 0.0


def test_the_local_prefix_is_still_free(cost):
    assert cost("local:Kimi-k2.5", 1_000_000, 1_000_000) == 0.0


def test_a_priced_model_bills_its_rate(cost):
    assert cost("priced-model", 1_000_000, 1_000_000) == pytest.approx(18.0)


def test_no_name_heuristic_remains():
    tree = ast.parse((ROOT / "routers" / "projects_router.py").read_text(
        encoding="utf-8", errors="replace"))
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not {"llama", "ollama"} & literals
