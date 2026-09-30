# SPDX-License-Identifier: MIT
"""§N.1 step 11 — AINXT_TIER_MAP was a second tier system; now it resolves.

core/config.py built a dict from six AINXT_MODEL_* variables, keyed by its own
vocabulary: the four governed text tiers, PLUS `local` and `local_mini` (two
deployment topologies the governed eight deliberately do not have), PLUS four
vendor aliases. None of it was visible to Model Governance > Tiers. Its own
documentation disagreed with itself — the header called the values "in-house
vLLM model ID"s while the alias comments named claude-sonnet-4-6 — which is
what an unowned second source of truth looks like.

Two constraints shape the replacement and both are tested here:

  * it has to be a FUNCTION, because resolving a tier is a live database read
    and core/config.py is imported before the database exists; and
  * it must never raise, because it sits on a chat turn.

No field evidence is available for this step: AINXT_API_URL is unset on this
deployment, so gateway.py's _AINXT_API_ENABLED is False and the block that
calls this never executes. These tests and the AST assertions at the bottom
are the whole of the evidence, which is stated rather than implied.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

import core.config as cfg
from core.tiers import Tier

ROOT = pathlib.Path(__file__).resolve().parents[2]


class _RM:
    def __init__(self, model_id):
        self.model_id = model_id


@pytest.fixture(autouse=True)
def _assigned(monkeypatch: pytest.MonkeyPatch):
    """A resolver that answers from the tier alone, so these tests describe
    the MAPPING and not this deployment's assignments."""
    import core.tier_resolver as tr
    monkeypatch.setattr(tr, "resolve_tier",
                        lambda tier, *a, **kw: _RM(f"model-for-{Tier(tier).value}"))
    for name, _ in cfg._AINXT_TIER_OVERRIDES.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cfg, "_AINXT_TIER_OVERRIDES",
                        {k: (n, "") for k, (n, _v) in cfg._AINXT_TIER_OVERRIDES.items()})
    cfg._AINXT_OVERRIDE_WARNED.clear()


# ── The old map's keys all still resolve ──────────────────────────────────


@pytest.mark.parametrize("key,tier", [
    ("simple",  Tier.SIMPLE),
    ("medium",  Tier.MEDIUM),
    ("complex", Tier.COMPLEX),
    ("mini",    Tier.MINI),
    # Vendor aliases, translated at the boundary rather than by a private copy.
    ("claude",  Tier.COMPLEX),
    ("sonnet",  Tier.COMPLEX),
    ("gpt",     Tier.MEDIUM),
    ("haiku",   Tier.SIMPLE),
    # The CIL used to be able to emit these; both meant "harder than medium".
    ("deep",     Tier.COMPLEX),
    ("solution", Tier.COMPLEX),
])
def test_every_alias_lands_on_the_tier_it_means(key, tier):
    assert cfg.ainxt_model_for(key) == f"model-for-{tier.value}"


@pytest.mark.parametrize("key", ["", "auto", "default", "  ", "AUTO"])
def test_the_no_tier_values_keep_their_flat_default(key):
    """AINXT_MODEL_DEFAULT meant "the CIL offered nothing". gateway.py's own
    flat default for that case is "medium" (_fp_hint), so the two now agree
    instead of pointing at different models."""
    assert cfg.ainxt_model_for(key) == "model-for-medium"


@pytest.mark.parametrize("key", ["local", "local_mini"])
def test_the_two_topology_tiers_become_mini_not_intent_classification(key):
    """D32, asserted BECAUSE it deviates from §E.

    §E retires TIER_LOCAL_MINI onto intent-classification, noting it has "one
    consumer". This map was a second consumer §E did not count, and it is not
    a classifier — it picks the model for a whole interactive ainxt-api
    session. Routing a chat session to the intent-classification tier would
    be a faithful reading of the table and the wrong answer.
    """
    assert cfg.ainxt_model_for(key) == "model-for-mini"


# ── A human's explicit choice is never substituted ────────────────────────


@pytest.mark.parametrize("pick", ["qwen-3.6-35B-A3B", "my-vllm-deployment", "claude-opus-5"])
def test_a_concrete_model_id_passes_through_untouched(pick):
    """Including its casing — model ids are case-sensitive."""
    assert cfg.ainxt_model_for(pick) == pick


def test_a_local_model_reference_passes_through():
    assert cfg.ainxt_model_for("local:llama3.1:8b") == "local:llama3.1:8b"


def test_a_bare_vendor_name_is_not_forwarded_as_a_model_id(monkeypatch):
    """"gemini" resolves to EXPLICIT_MODEL, but it is a VENDOR, not a model.
    Passing it through would hand ainxt-api the string "gemini" as a model id.
    It keeps resolving to AINXT_MODEL_DEFAULT exactly as before — picking a
    vendor instead of a model is a gap in the selector, not something this
    resolver can invent an answer for."""
    monkeypatch.setattr(cfg, "AINXT_MODEL_DEFAULT", "the-default")
    assert cfg.ainxt_model_for("gemini") == "the-default"


def test_gemini_stays_in_the_alias_set_so_the_gateway_keeps_routing_it_here():
    """The pairing that makes the test above true. Drop "gemini" from the set
    and the gateway stops treating it as an alias, takes its concrete-model-id
    branch, and forwards the bare word — the exact failure the resolver
    avoids."""
    assert "gemini" in cfg.AINXT_TIER_ALIASES


def test_an_explicit_sku_alias_is_a_model_not_a_tier():
    """EXPLICIT_MODEL covers bare vendors AND concrete SKUs — "claude-opus-5",
    "sonnet-5", "opus-4-8". Only the bare vendor is unusable as a model id, so
    only that one is special-cased; treating the whole sentinel as "unusable"
    would substitute a tier's model for a SKU the user asked for by name,
    which is the defect this migration removes."""
    assert cfg.ainxt_model_for("claude-opus-5") == "claude-opus-5"
    assert cfg.ainxt_model_for("sonnet-5") == "sonnet-5"


# ── Operator overrides (D28) ──────────────────────────────────────────────


def test_an_env_override_still_wins_over_the_assignment(monkeypatch):
    monkeypatch.setattr(cfg, "_AINXT_TIER_OVERRIDES",
                        {**cfg._AINXT_TIER_OVERRIDES,
                         "complex": ("AINXT_MODEL_COMPLEX", "pinned-by-operator")})
    assert cfg.ainxt_model_for("complex") == "pinned-by-operator"


def test_the_override_warns_once_per_variable(monkeypatch, caplog):
    """Warned so the bypass is visible and countable — that reading zero is
    what lets Phase 10 remove the variable."""
    monkeypatch.setattr(cfg, "_AINXT_TIER_OVERRIDES",
                        {**cfg._AINXT_TIER_OVERRIDES,
                         "medium": ("AINXT_MODEL_MEDIUM", "pinned")})
    import logging
    with caplog.at_level(logging.WARNING):
        cfg.ainxt_model_for("medium")
        cfg.ainxt_model_for("gpt")
    assert sum("AINXT_MODEL_MEDIUM" in r.message for r in caplog.records) == 1


# ── Never raise into a chat turn ──────────────────────────────────────────


def test_an_unassigned_tier_degrades_to_the_default(monkeypatch):
    from core.tier_resolver import NoEligibleModel
    import core.tier_resolver as tr

    def _boom(tier, *a, **kw):
        raise NoEligibleModel(Tier(tier), None, {})
    monkeypatch.setattr(tr, "resolve_tier", _boom)
    monkeypatch.setattr(cfg, "AINXT_MODEL_DEFAULT", "fallback-model")
    assert cfg.ainxt_model_for("complex") == "fallback-model"


def test_a_database_outage_degrades_to_the_default(monkeypatch):
    import core.tier_resolver as tr

    def _boom(tier, *a, **kw):
        raise RuntimeError("could not connect to server")
    monkeypatch.setattr(tr, "resolve_tier", _boom)
    monkeypatch.setattr(cfg, "AINXT_MODEL_DEFAULT", "fallback-model")
    assert cfg.ainxt_model_for("medium") == "fallback-model"


# ── The constraint that forced the design ─────────────────────────────────


def test_core_config_imports_with_no_database():
    """The reason AINXT_TIER_MAP could not simply become a resolved dict.
    core/config.py is imported by essentially everything, long before the DB
    is reachable; a module-scope resolve_tier() would make the gateway
    unbootable on a cold start. Run in a subprocess with the DB pointed
    nowhere, because an in-process assertion would be satisfied by an
    already-warm connection."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import core.config as c; assert callable(c.ainxt_model_for); print('ok')"],
        cwd=ROOT, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "DATABASE_URL":
             "postgresql://nobody:nobody@127.0.0.1:1/nope"},
    )
    assert proc.returncode == 0 and "ok" in proc.stdout, (
        f"core.config no longer imports without a database.\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr[-2000:]}")


def test_the_resolver_defers_its_heavy_imports():
    """Stated as a test because it is the whole design: core.tier_resolver
    pulls in the DB layer, so importing it at module scope in core/config.py
    reintroduces exactly the cycle above."""
    tree = ast.parse((ROOT / "core" / "config.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.col_offset == 0:
            assert not (node.module or "").startswith(("core.tier_resolver", "db.")), (
                f"core/config.py:{node.lineno} imports {node.module} at module "
                f"scope — it must stay inside ainxt_model_for()")


# ── The gateway consumer ──────────────────────────────────────────────────


def test_the_gateway_no_longer_imports_the_dict():
    src = (ROOT / "gateway.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = {
        a.name for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module == "core.config"
        for a in n.names
    }
    assert "AINXT_TIER_MAP" not in imported
    assert {"ainxt_model_for", "AINXT_TIER_ALIASES"} <= imported


def test_the_alias_set_covers_every_key_the_gateway_branches_on():
    """gateway.py asks "is this value a tier alias or a model the user named?"
    before calling. Keeping that list next to the resolver is the point —
    a second copy in gateway.py is how the two would answer differently."""
    for key in ("simple", "medium", "complex", "mini", "local", "local_mini",
                "auto", "default", "claude", "sonnet", "gpt"):
        assert key in cfg.AINXT_TIER_ALIASES
    # A concrete id must NOT be in it, or the gateway would send it for
    # tier resolution instead of using it verbatim.
    assert "claude-sonnet-4-6" not in cfg.AINXT_TIER_ALIASES
