# SPDX-License-Identifier: MIT
"""§N.1 step 8f — the SDLC CLI helpers are now consumed by SDLC only.

core/model_registry.py's cli_model_for_tier() and openai_model_for_tier() were
SDLC plumbing: the first hardcoded ("anthropic", …) for three tiers and
("openai", …) for two — the most provider-biased map in the repository — and
the second existed purely because the SDLC manifest cross-validator spoke the
OpenAI wire format.

§N.1 step 10 DELETED both, along with cli_model_for(). The map survives only
inside _legacy_cli_model_for_tier() as the governance-off fallback, and Phase
8 removes it with the rest of the .env model constants. This file now guards
two things: that nothing has reintroduced them, and that the three named
phase helpers that replaced them stay SDLC-only.

Three modules outside SDLC had drifted onto them, each acquiring an accidental
dependency on SDLC_TIER_*_MODEL and ENABLE_OPUS:

    models/hybrid_retriever.py    — removed in step 6
    services/feedback_processor.py \\ removed here, so that step 10 inherits an
    routers/coach_router.py        / SDLC-only surface and can delete in one go

This file is the ratchet for that. It is deliberately an IMPORT-graph test
rather than a text scan: the two migrated modules now name the old helpers in
comments explaining what was wrong with them, which is exactly the
documentation the next reader needs.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Deleted outright by §N.1 step 10 — importing one is now an ImportError, so
# this set is a readability guard rather than a ratchet. Kept because a
# revert would otherwise show up only as a crash at run time in a module with
# no test coverage.
_DELETED_HELPERS = {"cli_model_for_tier", "openai_model_for_tier", "cli_model_for"}

# Still present, still SDLC-only: the named CLI phases. They resolve through
# cli_tier_model_id() (the tier assignments) rather than through .env, so the
# reason to keep non-SDLC modules off them is narrower than it was — but it
# has not gone away. Each applies a DEPRECATED per-phase env pin, and a
# non-SDLC caller picking one up would inherit an SDLC operator's override.
_HELPERS = {"cli_classify_model", "cli_plan_model", "cli_implement_model",
            "cli_coder_model", "cli_tier_model_id"} | _DELETED_HELPERS

# Where an SDLC helper legitimately belongs until §N.1 step 10 deletes it.
# The last two are SDLC modules that do not carry the `sdlc` prefix — listed
# individually rather than by a looser pattern, so that a NEW non-SDLC
# consumer cannot slip in under a broad rule.
_ALLOWED_PREFIXES = ("agents/sdlc", "core/model_registry.py", "tests/")
_ALLOWED_FILES = {"workers/sdlc_worker.py", "agents/brd_fsd_pipeline.py"}


def _python_files():
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("venv/", ".git/", "node_modules/", "AgentStudio/frontend/")):
            continue
        yield rel, path


def _imports_a_helper(path: pathlib.Path) -> set[str]:
    """Names imported FROM core.model_registry. Parsed, not grepped."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "core.model_registry":
            found |= {a.name for a in node.names} & _HELPERS
    return found


def test_only_sdlc_imports_the_sdlc_cli_helpers():
    offenders = {}
    for rel, path in _python_files():
        if rel.startswith(_ALLOWED_PREFIXES) or rel in _ALLOWED_FILES:
            continue
        hit = _imports_a_helper(path)
        if hit:
            offenders[rel] = sorted(hit)
    assert not offenders, (
        f"non-SDLC modules import SDLC CLI helpers: {offenders}. These carry "
        f"SDLC's own deprecated per-phase env pins and resolve a CONCRETE id "
        f"for an out-of-process spawn. An in-process caller wants routing "
        f"kwargs — ask for a tier; see services/feedback_processor.py.")


def test_the_provider_biased_helpers_are_deleted():
    """The thing this file was written in step 8 to make possible. Its
    docstring then said "§N.1 step 10 deletes both"; this is that."""
    import core.model_registry as mr

    still_here = sorted(h for h in _DELETED_HELPERS if hasattr(mr, h))
    assert not still_here, (
        f"{still_here} came back. _tier_to_role hardcoded a provider family "
        f"per tier and never read the admin's assignments; it lives on only "
        f"as _legacy_cli_model_for_tier, the governance-off path.")


def test_nothing_anywhere_imports_a_deleted_helper():
    """Including the SDLC modules themselves — they are exempt from the
    sweep above, so without this the deletion would be unguarded exactly
    where it matters most."""
    offenders = {}
    for rel, path in _python_files():
        if rel.startswith("tests/"):
            continue
        hit = _imports_a_helper(path) & _DELETED_HELPERS
        if hit:
            offenders[rel] = sorted(hit)
    assert not offenders, f"deleted helpers are still imported by {offenders}"


@pytest.mark.parametrize("rel", [
    "services/feedback_processor.py",
    "routers/coach_router.py",
    "models/hybrid_retriever.py",
])
def test_the_three_reclaimed_modules_stay_clean(rel):
    """Named individually as well as covered by the sweep above, so a failure
    says which module regressed rather than only that one did."""
    assert not _imports_a_helper(ROOT / rel)


def test_coach_no_longer_constructs_a_provider_gateway_directly():
    """It built OpenAIGateway() itself, so a deployment with no OpenAI
    provider could not use the Coach rewrite at all — and it resolved to
    gpt-5-mini, which has no registry row here, so the feature was already
    falling through to the deterministic scaffold."""
    tree = ast.parse((ROOT / "routers" / "coach_router.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("gateway_"):
            raise AssertionError(
                f"routers/coach_router.py:{node.lineno} imports {node.module} "
                f"directly — the router knows every family; pinning one here "
                f"is the provider assumption §N.1 exists to remove")


def test_feedback_processor_no_longer_posts_a_provider_at_the_proxy():
    """It sent {"provider": "claude", ...} to /llm/generate — byte-for-byte
    the defect step 6 removed from hybrid_retriever."""
    tree = ast.parse((ROOT / "services" / "feedback_processor.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and k.value == "provider"
                    and isinstance(v, ast.Constant)):
                raise AssertionError(
                    f"services/feedback_processor.py:{node.lineno}: hardcoded "
                    f"provider {v.value!r}")


def test_the_coach_rewrite_sends_the_prompt_and_not_its_type():
    """`{type(prompt).__name__}` interpolates the literal "str", so Coach
    asked the model to rewrite the word "str". Present since the initial
    commit and invisible because the call around it was already failing —
    fixing the routing without fixing this would have turned a broken feature
    into a working call that produces nonsense."""
    # AST over f-string interpolations, not a text scan: the fix is explained
    # in a comment directly above itself, and that comment necessarily quotes
    # the broken expression. A grep would flag the explanation as the defect —
    # the same trap check_tier_migration parses to avoid.
    tree = ast.parse((ROOT / "routers" / "coach_router.py").read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for part in node.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            expr = ast.unparse(part.value)
            if "__name__" in expr:
                bad.append((node.lineno, expr))
    assert not bad, (
        f"routers/coach_router.py interpolates a type name into a prompt "
        f"instead of the prompt itself: {bad}")
