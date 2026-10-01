# SPDX-License-Identifier: MIT
"""D106/D107 — tier governance is unconditional since Phase 8.

TIER_GOVERNANCE_ENABLED is gone. Nothing may read it, the admin screen always
reports governance active, and a capability request that resolves to nothing
raises instead of falling back to .env model constants.

Replaces test_governance_flag_single_source.py (the flag's single parser) and
test_phase6_parity.py (flag-off parity of every migrated call site); their
subject no longer exists.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
_SKIP_DIRS = {"tests", "node_modules", ".git", "venv", "docs"}


def test_core_tiers_no_longer_exposes_the_switch():
    import core.tiers
    assert not hasattr(core.tiers, "governance_enabled")


def test_core_tiers_stays_a_stdlib_only_leaf():
    """model_router imports it at module scope; a non-stdlib import is a cycle waiting to happen."""
    tree = ast.parse((ROOT / "core" / "tiers.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in {"os", "enum", "typing"}, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert (node.module or "").split(".")[0] in {"__future__", "enum", "typing"}, node.module


def test_no_module_reads_the_removed_switch():
    offenders = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if _SKIP_DIRS & set(rel.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "TIER_GOVERNANCE_ENABLED" not in text and "governance_enabled" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Constant) and node.value == "TIER_GOVERNANCE_ENABLED":
                offenders.append(f"{rel.as_posix()}:{node.lineno}")
            if isinstance(node, (ast.Name, ast.Attribute)) and \
                    getattr(node, "id", getattr(node, "attr", "")) in ("governance_enabled", "_governance_enabled"):
                offenders.append(f"{rel.as_posix()}:{node.lineno}")
            if isinstance(node, ast.alias) and node.name in ("governance_enabled",):
                offenders.append(f"{rel.as_posix()}:alias")
    assert offenders == []


def test_the_admin_screen_reports_governance_active():
    from routers.tier_governance_router import _governance_active
    assert _governance_active() is True


def test_the_router_has_no_env_fallback_left():
    import models.model_router as mr
    for name in ("_governance_enabled", "_warn_env_fallback", "_ENV_FALLBACK_WARNED",
                 "_NO_ENV_FALLBACK", "_promote_for_context", "_TIER_CONTEXT_WINDOW",
                 "_CONTEXT_PROMOTION_LADDER"):
        assert not hasattr(mr, name), name


@pytest.mark.parametrize("no_cloud,needle", [(True, "cloud provider"), (False, "Model Governance")])
def test_the_user_message_names_the_right_fix(no_cloud, needle):
    from core.tier_resolver import Constraints, NoEligibleModel
    from core.tiers import Tier
    from models.model_router import no_eligible_message
    msg = no_eligible_message(NoEligibleModel(Tier.MEDIUM, Constraints(no_cloud_egress=no_cloud), {}))
    assert msg.startswith("Error:") and needle in msg
