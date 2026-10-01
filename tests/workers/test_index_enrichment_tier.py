# SPDX-License-Identifier: MIT
"""§N.1 step 6 — chunk enrichment asks for a tier.

`workers/index_worker.py` makes one LLM call per code chunk per indexed repo:
the highest-volume LLM consumer in the platform. Nothing governed it, and not
because anybody decided that — `ENRICH_MODEL` is empty on a default install,
and `ModelRouter.route()` gates its hint branch on `if model_hint:`, so an
empty hint skipped the hint path entirely and every chunk was
complexity-classified on its own.

The assertion that would have caught the bug in the first draft of this work
is test_the_legacy_hint_is_a_real_hint: `_coerce_tier` raises ValueError unless
`legacy_hint` is a known alias (core.tiers.LEGACY_INBOUND_ALIASES), and the falsy-key guard at
model_router.py:800 strips `""`. So `legacy_hint=""` — the obvious way to say
"it used to pass nothing" — would have raised on the first enriched chunk of
the first indexed repo.

Read as source rather than imported: index_worker pulls in the embed-service
pool and a Redis handle at module scope. Same reason
tests/agents/test_connector_first_routing.py does it this way.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKER = ROOT / "workers" / "index_worker.py"
ROUTER = ROOT / "models" / "model_router.py"


def _src(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _enrich_call() -> ast.Call:
    """The single model_router.generate(...) call on the enrichment path."""
    calls = [
        n for n in ast.walk(ast.parse(_src(WORKER)))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "generate"
    ]
    assert calls, "no model_router.generate(...) found in index_worker.py"
    assert len(calls) == 1, (
        f"index_worker.py now has {len(calls)} generate() calls; this test "
        f"assumed one and needs updating rather than silently checking the "
        f"wrong one")
    return calls[0]


# ── The tier request ──────────────────────────────────────────────────────


def test_enrichment_asks_for_a_tier_not_a_model():
    src = _src(WORKER)
    assert '"tier": Tier.SIMPLE' in src, (
        "chunk enrichment no longer asks for Tier.SIMPLE — the platform's "
        "highest-volume LLM consumer is back to being ungoverned")


def test_simple_and_not_mini():
    """MINI is defined by bounded cost, SIMPLE by needing reliable
    instruction-following. An enrichment that returns prose where a
    description was asked for is silently dropped (the caller requires >10
    chars), so the failure mode is a chunk that never gets enriched and a
    retrieval quality loss nobody can see."""
    assert "Tier.MINI" not in _src(WORKER)


def test_the_legacy_hint_is_a_real_hint():
    """_coerce_tier raises unless legacy_hint is a known alias
    (core.tiers.LEGACY_INBOUND_ALIASES). The first draft of this change passed
    legacy_hint="" to mean "it used to pass nothing" — that would have raised
    ValueError on the first enriched chunk."""
    from core.tiers import LEGACY_INBOUND_ALIASES
    m = re.search(r'"legacy_hint":\s*"([^"]*)"', _src(WORKER))
    assert m, "the enrichment call passes no legacy_hint — D15 needs one"
    hint = m.group(1)
    assert hint, "legacy_hint is empty; no alias is falsy"
    assert hint in LEGACY_INBOUND_ALIASES, (
        f"legacy_hint={hint!r} is not a known alias — "
        f"_coerce_tier raises ValueError for exactly this")


def test_the_call_dispatches_through_the_kwargs_mapping():
    """A branch that computes _route_kwargs and then does not use them is the
    way this change silently becomes a no-op."""
    call = _enrich_call()
    assert any(k.arg is None for k in call.keywords), (
        "model_router.generate(...) is not called with **_route_kwargs, so the "
        "tier branch above it is computed and discarded")


# ── The data-residency switch ─────────────────────────────────────────────


def test_no_cloud_egress_is_offered_and_forwarded():
    """The prompt contains SOURCE CODE. On a deployment whose `simple` tier
    holds a cloud model — which is the case on the deployment this shipped to
    — enrichment posts every indexed chunk to that vendor. There has to be a
    way to say no."""
    src = _src(WORKER)
    assert "ENRICH_NO_CLOUD_EGRESS" in src
    assert '"no_cloud_egress": _ENRICH_NO_CLOUD_EGRESS' in src


def test_no_cloud_egress_defaults_to_todays_behaviour():
    """Defaulting it ON would change what every existing deployment does on
    upgrade, and the change would present as indexing failing outright."""
    m = re.search(r'_ENRICH_NO_CLOUD_EGRESS = \(os\.getenv\("ENRICH_NO_CLOUD_EGRESS",\s*"([^"]*)"\)',
                  _src(WORKER))
    assert m, "ENRICH_NO_CLOUD_EGRESS is not read with an explicit default"
    assert m.group(1) == "", f"default is {m.group(1)!r}, expected unset"


def test_the_residency_switch_is_documented_and_the_override_is_not():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "ENRICH_NO_CLOUD_EGRESS" in env, (
        "a data-residency switch that is not in .env.example is one nobody "
        "will find when they need it")
    assert "ENRICH_MODEL" not in env   # removed in Phase 8; the `simple` tier decides


@pytest.mark.parametrize("path", [WORKER, ROUTER])
def test_the_files_still_parse(path):
    ast.parse(_src(path))
