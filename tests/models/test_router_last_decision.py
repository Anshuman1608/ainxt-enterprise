# SPDX-License-Identifier: MIT
"""Two router contracts that existed on paper and not in the process.

1. ``get_router()`` — imported by agents/review_engine.py (x2),
   agents/advanced_reasoning.py and memory/postgres_memory.py, and defined
   nowhere. Each import sat inside a bare ``except Exception``, so those paths
   returned their failure value on every call instead of calling an LLM.

2. ``last_decision`` (FallbackInfo) — property and setter present since the L5
   contract was written, never assigned. The only FallbackInfo ever built was
   the ``not_set`` sentinel, so ``fallback_occurred`` read False forever and
   ``sdlc_pipeline._core._emit_fallback_event_if_any()`` never emitted.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import models.model_router as mr

ROOT = pathlib.Path(__file__).resolve().parents[2]


# ── get_router ──────────────────────────────────────────────────────────────

def test_get_router_exists_and_returns_the_singleton():
    assert mr.get_router() is mr.model_router


def test_every_get_router_importer_can_actually_import_it():
    """The four sites that were failing. Kept as a list so a fifth is caught."""
    importers = []
    for rel in ("agents/review_engine.py", "agents/advanced_reasoning.py",
                "memory/postgres_memory.py"):
        src = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        for n in ast.walk(ast.parse(src)):
            if (isinstance(n, ast.ImportFrom)
                    and (n.module or "").endswith("model_router")
                    and any(a.name == "get_router" for a in n.names)):
                importers.append(f"{rel}:{n.lineno}")
    assert importers, "nobody imports get_router any more — drop this test"
    for name in ("get_router",):
        assert hasattr(mr, name), (
            f"{importers} import {name!r} from models.model_router and it is "
            f"not defined — every one of those call sites raises ImportError "
            f"into a bare except and silently returns its failure value"
        )


# ── last_decision ───────────────────────────────────────────────────────────

class _Resolved:
    def __init__(self, req, got):
        self.requested_tier = req
        self.tier = got
        self.selection_mode = "fallback" if req is not got else "tier"


class _Decision:
    def __init__(self, fallback, resolved, model="m-1", tier="governed"):
        self.fallback = fallback
        self.resolved = resolved
        self.model = model
        self.tier = tier


def _tier(name):
    from core.tiers import Tier
    return Tier(name)


def test_record_selection_publishes_a_real_fallback_info():
    r = mr.ModelRouter()
    r._record_selection(_Decision(False, [_Resolved(_tier("simple"),
                                                    _tier("simple"))]))
    fi = r.last_decision
    assert fi.reason == "primary"
    assert fi.fallback_occurred is False
    assert fi.from_tier == "simple" and fi.to_tier == "simple"


def test_a_ladder_walk_is_reported():
    r = mr.ModelRouter()
    r._record_selection(_Decision(True, [_Resolved(_tier("simple"),
                                                   _tier("medium"))]))
    fi = r.last_decision
    assert fi.fallback_occurred is True
    assert fi.reason == "tier_fallback"
    assert fi.from_tier == "simple" and fi.to_tier == "medium"


def test_the_not_set_sentinel_is_no_longer_what_readers_get():
    """The regression this file exists for: a routed call must move it off
    the sentinel, otherwise every consumer silently reads 'no fallback'.

    Run on a fresh thread because the backing store is threading.local() and
    an earlier test in this process has already written to this one.
    """
    import threading

    seen = {}

    def _run():
        r = mr.ModelRouter()
        seen["before"] = r.last_decision.reason
        r._record_selection(_Decision(True, [_Resolved(_tier("simple"),
                                                       _tier("medium"))]))
        seen["after"] = r.last_decision.reason

    t = threading.Thread(target=_run)
    t.start()
    t.join()
    assert seen["before"] == "not_set"
    assert seen["after"] == "tier_fallback"


def test_an_unresolved_decision_does_not_crash():
    """The legacy path has decision.resolved == []."""
    r = mr.ModelRouter()
    r._record_selection(_Decision(False, [], tier="simple"))
    assert r.last_decision.fallback_occurred is False


def test_last_decision_is_thread_local():
    import threading

    r = mr.ModelRouter()
    r._record_selection(_Decision(True, [_Resolved(_tier("simple"),
                                                   _tier("medium"))]))
    seen = {}

    def _other():
        seen["reason"] = r.last_decision.reason

    t = threading.Thread(target=_other)
    t.start()
    t.join()
    assert seen["reason"] == "not_set", (
        "last_decision leaked across threads — the cost guard in "
        "followup_condenser would reject on another request's fallback"
    )
    assert r.last_decision.reason == "tier_fallback"


@pytest.mark.parametrize("entry", [
    "generate", "stream", "async_generate", "async_stream",
])
def test_every_routing_entry_point_records_its_selection(entry):
    """_record_selection is what publishes last_decision, so an entry point
    that stops calling it stops reporting fallbacks — silently."""
    src = (ROOT / "models" / "model_router.py").read_text(
        encoding="utf-8", errors="replace")
    # Scope to ModelRouter — several gateway classes in this module also
    # define generate()/stream(), and the first match is not ours.
    cls = next(
        (n for n in ast.walk(ast.parse(src))
         if isinstance(n, ast.ClassDef) and n.name == "ModelRouter"),
        None,
    )
    assert cls is not None, "ModelRouter has been renamed"
    fn = next(
        (n for n in cls.body
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name == entry),
        None,
    )
    assert fn is not None, f"ModelRouter.{entry}() has been renamed"
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_record_selection"
    ]
    assert calls, f"{entry}() no longer calls _record_selection()"
