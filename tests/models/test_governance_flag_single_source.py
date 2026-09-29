# SPDX-License-Identifier: MIT
"""§N.1 step 6, D26 — one reader of TIER_GOVERNANCE_ENABLED.

The flag was parsed independently in models/model_router.py and
routers/tier_governance_router.py, each carrying a comment saying the readers
"have to agree" — the kind of note that is true right up until it is not. If
they disagree the admin screen reports a state the router is not in, and an
administrator who reassigns a tier believing it took effect is worse off than
one with no screen at all.

Step 6 added three consumers that never touch ModelRouter (chunk enrichment,
CodeWiki's subprocess config, hybrid retrieval), so the choice was a fourth
and fifth copy or one shared function. core/tiers.py owns it now; everyone
else delegates.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Every module that is allowed to name the env var. core/tiers.py owns the
# parse; core/config.py documents the platform's env surface.
_ALLOWED = {"core/tiers.py", "core/config.py"}

_SKIP_DIRS = {"venv", "node_modules", ".git", "tests", "dist", "build",
              ".scratch", "AgentStudio"}


def test_core_tiers_owns_the_parse():
    from core.tiers import governance_enabled
    assert callable(governance_enabled)


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("", False), ("  ", False), ("maybe", False),
])
def test_the_vocabulary_matches_core_config(monkeypatch, raw, expected):
    """core/config.py::_env_bool, the router and the admin screen all have to
    read the same word list, or "on" means governed in one place and not in
    another."""
    from core.tiers import governance_enabled
    monkeypatch.setenv("TIER_GOVERNANCE_ENABLED", raw)
    assert governance_enabled() is expected


def test_it_is_read_per_call_not_at_import(monkeypatch):
    """An operator must be able to flip the flag — and roll it back — without
    restarting the gateway."""
    from core.tiers import governance_enabled
    monkeypatch.setenv("TIER_GOVERNANCE_ENABLED", "true")
    assert governance_enabled() is True
    monkeypatch.setenv("TIER_GOVERNANCE_ENABLED", "false")
    assert governance_enabled() is False


def test_core_tiers_stays_a_stdlib_only_leaf():
    """The module header promises this, and model_router imports it at module
    scope. A non-stdlib import here is an import cycle waiting to happen."""
    tree = ast.parse((ROOT / "core" / "tiers.py").read_text(encoding="utf-8"))
    for node in tree.body:          # module scope only; lazy imports are fine
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in {"os", "enum", "typing"}, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert (node.module or "").split(".")[0] in {"__future__", "enum", "typing"}, node.module


def test_nobody_else_hand_rolls_the_parse():
    offenders = []
    for path in sorted(ROOT.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in _ALLOWED:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "TIER_GOVERNANCE_ENABLED" not in text:
            continue
        # Naming it in a comment or a message is fine; parsing it is not.
        for node in ast.walk(ast.parse(text)):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "getenv"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "TIER_GOVERNANCE_ENABLED"):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "these modules parse TIER_GOVERNANCE_ENABLED themselves instead of "
        f"calling core.tiers.governance_enabled(): {offenders}")
