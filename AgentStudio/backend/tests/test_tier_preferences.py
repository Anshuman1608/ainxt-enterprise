# SPDX-License-Identifier: MIT
"""§N.1 step 7 — Agent Studio's model choices come from Model Governance.

Two things were hardcoded here, and plan.html calls the first "the clearest
instance of the problem this migration exists to fix":

  * workflow_factory/pipeline.py::_TIER_PREFERENCES — twenty vendor model ids
    in three priority-ordered lists, eleven lines below a comment promising
    "No model ids are hardcoded here … so that no vendor's models are
    assumed". Structurally it IS this migration's design (priority-ordered
    candidates, first available wins) expressed in the wrong place.
  * app/core/llm_handler.py::_classify_model — a prefix heuristic deciding
    whether a call goes through the proxy or straight to LiteLLM. An
    administrator could register an in-house model against an
    OpenAI-compatible provider and Agent Studio would still call it "local"
    and bypass the proxy, losing the audit row, the budget gate and the
    privacy scrub.

Both keep their old behaviour as a fallback, deliberately: this service can
run with a database it cannot reach, and an Agent Studio that picks no model
is worse than one picking from a stale list.

Source-based for the pipeline (importing it pulls in the whole workflow
factory); _classify_model is a pure function and is exercised directly.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
PIPELINE = ROOT / "AgentStudio" / "backend" / "workflow_factory" / "pipeline.py"
HANDLER = ROOT / "AgentStudio" / "backend" / "app" / "core" / "llm_handler.py"
TOOLS = ROOT / "AgentStudio" / "backend" / "app" / "tools" / "platform_tools.py"

# The AgentStudio backend is its own package root ("app.core…", not
# "AgentStudio.backend.app.core…"), which is why `cd AgentStudio/backend &&
# pytest` is the documented way to run these.
_AS_BACKEND = ROOT / "AgentStudio" / "backend"
for _p in (str(ROOT), str(_AS_BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _src(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore")


# ── 7a: the candidate lists ───────────────────────────────────────────────


def test_the_buckets_map_onto_governed_tiers():
    src = _src(PIPELINE)
    assert "_TIER_TO_GOVERNED" in src
    tree = ast.parse(src)
    mapping = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.AnnAssign)
                and getattr(node.target, "id", "") == "_TIER_TO_GOVERNED"):
            mapping = ast.literal_eval(node.value)
    assert mapping == {"fast": "mini", "balanced": "medium", "deep": "complex"}


def test_the_wire_names_are_not_renamed():
    """D24. "fast"/"balanced"/"deep" appear in saved workflow JSON on live
    deployments; renaming them is a data migration, not a refactor."""
    src = _src(PIPELINE)
    assert '_MODEL_TIERS = ("fast", "balanced", "deep")' in src


def test_resolution_goes_through_the_platform_resolver():
    src = _src(PIPELINE)
    assert "resolve_tier_candidates" in src, (
        "Agent Studio no longer asks the resolver — the tier assignments on "
        "the admin screen decide nothing here again")
    assert "def _tier_candidates(" in src


def test_the_hardcoded_lists_survive_only_as_a_fallback():
    src = _src(PIPELINE)
    assert "_TIER_PREFERENCES_FALLBACK" in src
    # and nothing reads the old name any more
    tree = ast.parse(src)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "_TIER_PREFERENCES" not in names, (
        "something still reads _TIER_PREFERENCES directly, bypassing the "
        "resolver")


def test_the_fallback_is_reached_only_on_failure():
    """If the fallback were consulted first, or merged in, the registry would
    be decorative."""
    tree = ast.parse(_src(PIPELINE))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_tier_candidates")
    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "_tier_candidates cannot fall back at all"
    for h in handlers:
        assert "return fallback" in ast.unparse(h), (
            "the except branch does not return the built-in preference list")


def test_an_empty_resolver_answer_also_falls_back():
    """resolve_tier_candidates raises rather than returning [], but a future
    change to that contract must not leave Agent Studio with no models."""
    assert "ids or fallback" in _src(PIPELINE)


def test_the_runtime_catalogue_filter_is_kept():
    """A model can be assigned to a tier and still be absent from what the
    CLI will serve. Dropping this filter trades a governance bug for an
    'unknown model id' failure at run time."""
    src = _src(PIPELINE)
    assert "if candidate in available_models:" in src


# ── 7b: provider classification ───────────────────────────────────────────


def test_the_registry_is_consulted_before_the_prefixes():
    src = _src(HANDLER)
    i_reg = src.index("from core.llm_provider_registry import get_model")
    i_prefix = src.index('if name.startswith("claude"):')
    assert i_reg < i_prefix, (
        "the prefix heuristic runs first, so an admin-registered model's real "
        "provider is ignored")


def test_the_prefix_heuristic_is_still_there_for_unknown_ids():
    """In-house GPU model ids follow no cloud naming convention and are not
    always registry rows. Routing them to the proxy would 404."""
    tree = ast.parse(_src(HANDLER))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_classify_model")
    body = ast.unparse(fn)
    assert 'name.startswith(\'gemini\')' in body or 'name.startswith("gemini")' in body
    last = fn.body[-1]
    assert (isinstance(last, ast.Return) and isinstance(last.value, ast.Constant)
            and last.value.value == "local"), (
        "_classify_model no longer ends in the `local` catch-all — an "
        "in-house id that matches no prefix would now fall off the end")


def test_an_unregistered_id_still_classifies_by_prefix():
    from app.core.llm_handler import _classify_model
    assert _classify_model("claude-something-nobody-registered") == "anthropic"
    assert _classify_model("gemini-9-ultra") == "gemini"
    assert _classify_model("gpt-9") == "openai"
    assert _classify_model("qwen-3.6-35B-A3B") == "local"
    assert _classify_model("") == "local"


def test_a_registry_family_the_proxy_cannot_serve_is_local():
    """The registry's families are the PROVIDER vocabulary
    (anthropic/openai/gemini/ollama/…); this function returns the
    RUNTIME-CLIENT vocabulary. Anything the proxy does not front is a direct
    LiteLLM call, which is what "local" means here."""
    src = _src(HANDLER)
    assert 'if _family in ("anthropic", "openai", "gemini"):' in src
    assert "return _family" in src


def test_registry_failure_never_breaks_classification():
    tree = ast.parse(_src(HANDLER))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_classify_model")
    assert [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]


# ── 7c: the vocabulary that named nothing ─────────────────────────────────


def test_the_llm_generate_tool_no_longer_advertises_smart():
    """"smart" was in this tool's documented schema and existed in no
    vocabulary anywhere — not the workflow factory's buckets, not
    core.tiers.LEGACY_INBOUND_ALIASES, not the router's _HINT_MAP."""
    src = _src(TOOLS)
    assert "fast | smart | balanced" not in src
    assert '"enum": ["fast", "balanced", "deep"]' in src


def test_the_tool_records_that_its_hint_is_inert():
    """It is `draft: True` so nothing seeds it, and it posts model_hint to
    services/llm_proxy, whose GenerateRequest has no such field and requires
    `provider`. Saying so beats implying the hint is honoured."""
    src = _src(TOOLS)
    assert "not honoured yet" in src
    assert "GenerateRequest" in src


@pytest.mark.parametrize("path", [PIPELINE, HANDLER, TOOLS])
def test_the_files_still_parse(path):
    ast.parse(_src(path))
