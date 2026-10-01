# SPDX-License-Identifier: MIT
"""No application module may reach a model by importing its .env constant.

plan.html §P: application tasks use only the tiers. A constant is the same
coupling as a literal with a nicer name. Phase 8 (D109) deleted every SKU
constant and display label, so this is now a zero-rule: none is declared in
core/model_registry.py and none is imported from any model_registry.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import warnings

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "core" / "model_registry.py"


# ── the classification ──────────────────────────────────────────────────────
#
# Every public constant in core/model_registry.py is in exactly one bucket,
# and test_every_constant_is_classified proves it. That completeness check is
# the load-bearing part: without it a constant added tomorrow is silently
# outside the rule, which is precisely how review_engine.py stayed invisible.

#: Names a concrete vendor model. Deleted by Phase 8; must not return.
SKU_NAME = frozenset({
    "CLAUDE_HAIKU", "CLAUDE_OPUS_46_MODEL", "CLAUDE_OPUS_48_MODEL",
    "CLAUDE_OPUS_5_MODEL", "CLAUDE_OPUS_MODEL", "CLAUDE_PRIMARY_MODEL",
    "CLAUDE_SONNET_5_MODEL",
    "GEMINI_CODING_LITE_MODEL", "GEMINI_IMAGE_MODEL", "GEMINI_TEXT_MODEL",
    "GEMINI_VISION_MODEL",
    "OPENAI_CODING_MODEL", "OPENAI_DEEP_RESEARCH", "OPENAI_DEEP_RESEARCH_MINI",
    "OPENAI_IMAGE_MODEL", "OPENAI_LATEST_MODEL", "OPENAI_LUNA_MODEL",
    "OPENAI_OSS_MODEL", "OPENAI_PRIMARY_MODEL", "OPENAI_SIMPLE_MODEL",
    "OPENAI_TERA_MODEL",
    "LOCAL_LLM_MODEL_NAME", "SOLUTION_MODEL", "VEO_MODEL",
})

#: A display label for one SKU: a module that shows "Claude Opus 4.7" has
#: decided what answered. Deleted by Phase 8.
SKU_DISPLAY = frozenset({
    "CLAUDE_HAIKU_DISPLAY", "CLAUDE_OPUS_48_DISPLAY", "CLAUDE_OPUS_5_DISPLAY",
    "CLAUDE_OPUS_DISPLAY", "CLAUDE_PRIMARY_DISPLAY", "CLAUDE_SONNET_5_DISPLAY",
    "GEMINI_CODING_LITE_DISPLAY", "GEMINI_DISPLAY", "GEMINI_IMAGE_DISPLAY",
    "GEMINI_TEXT_DISPLAY",
    "OPENAI_CODING_DISPLAY", "OPENAI_LATEST_DISPLAY", "OPENAI_LUNA_DISPLAY",
    "OPENAI_OSS_DISPLAY", "OPENAI_SIMPLE_DISPLAY", "OPENAI_TERA_DISPLAY",
    "LOCAL_LLM_DISPLAY", "VEO_DISPLAY",
})

#: Explicitly permitted by §P — a cost/normalisation table, a deny-list at the
#: egress point, capability metadata, or a posture flag. None of these names a
#: model the caller then routes to.
PERMITTED = frozenset({
    "VEO_COST_PER_SECOND", "UNPRICED_RATES",
    "CLI_ADDRESSABLE_MODEL_PREFIXES",
    "BLOCKED_MODELS",
    "ENABLE_RAW_OPENAI_API", "LLM_PROVIDER",
    "SDLC_GROUNDING_CHARTER", "SDLC_STAGE_TIERS",
})

GUARDED = SKU_NAME | SKU_DISPLAY


# ── the sweep ───────────────────────────────────────────────────────────────


def _tracked_py() -> list[str]:
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [
        f for f in out.split()
        # tests legitimately name models; llm_proxy is vendored (§Q.5, U2)
        if not f.startswith(("tests/", "services/llm_proxy/"))
        and f != "core/model_registry.py"
        # still in the index but deleted on disk: an uncommitted removal
        and (ROOT / f).exists()
    ]


def _importers() -> dict[str, set[str]]:
    """{module: {constant, ...}} for every SKU-name/display import.

    AST rather than grep, for the reason check_tier_migration gives: the
    migrated modules now NAME the old constants in comments explaining what
    was wrong with them, and a text scan would report the explanation as the
    defect.
    """
    hits: dict[str, set[str]] = {}
    for rel in _tracked_py():
        try:
            # Parsing every tracked file surfaces warnings that belong to
            # those files rather than to this test — sandbox/
            # sandbox_image_builder.py:485,521,556 carry invalid '\s' escapes
            # in non-raw strings, which Python is progressively hardening from
            # DeprecationWarning toward an error. Real, pre-existing, and not
            # this phase's to fix; silenced here so the suite output stays
            # about the ratchet.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse((ROOT / rel).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if not (node.module or "").endswith("model_registry"):
                continue
            named = {a.name for a in node.names} & GUARDED
            if named:
                hits.setdefault(rel, set()).update(named)
    return hits


@pytest.fixture(scope="module")
def importers():
    return _importers()


# ── tests ───────────────────────────────────────────────────────────────────


def _declared() -> set[str]:
    tree = ast.parse(REGISTRY.read_text(encoding="utf-8", errors="replace"))
    return {
        t.id
        for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
        for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
        if isinstance(t, ast.Name) and not t.id.startswith("_")
    }


def test_no_sku_constant_is_declared():
    assert sorted(_declared() & GUARDED) == []


def test_every_constant_is_classified():
    """A new public constant must be argued into PERMITTED, not slipped in."""
    declared = _declared()
    assert sorted(declared - PERMITTED) == [], "classify it in PERMITTED or delete it"
    assert sorted(PERMITTED - declared) == [], "PERMITTED names a constant that is gone"


def test_no_module_imports_a_sku_constant(importers):
    assert importers == {}
