# SPDX-License-Identifier: MIT
"""§N.1 step 6 — the retrieval path stops naming a vendor.

`models/hybrid_retriever.py` had two LLM helpers that POSTed straight to the
proxy with `{"provider": "claude", "model": cli_model_for_tier("haiku")}`.
Two defects in one line:

  * the provider is pinned to a vendor, in the retrieval path of every query;
  * the model comes from `cli_model_for_tier()`, which is an **SDLC CLI**
    helper — so retrieval was quietly coupled to SDLC_TIER_SIMPLE_MODEL and
    ENABLE_OPUS, and changing an SDLC setting moved a retrieval model.

The obvious fix — forward `ResolvedModel.family` as `provider` — is wrong, and
test_the_proxy_vocabulary_is_not_the_registry_vocabulary is here so the next
person does not try it: the proxy accepts only claude|openai|gemini
(services/llm_proxy/main.py:2049) while registry families are
anthropic|gemini|ollama. It would 400. Routing through ModelRouter instead
removes the need to know that at all.

Asserted over the AST rather than the text, because the docstrings now QUOTE
the old pinned payload on purpose — that is the explanation a reader needs,
and a grep would flag the documentation of the fix as the defect. Same reason
scripts/ci/release_checks.py::check_tier_migration parses instead of grepping.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
RETRIEVER = ROOT / "models" / "hybrid_retriever.py"
PROXY = ROOT / "services" / "llm_proxy" / "main.py"


def _tree() -> ast.Module:
    return ast.parse(RETRIEVER.read_text(encoding="utf-8", errors="ignore"))


def _src() -> str:
    return RETRIEVER.read_text(encoding="utf-8", errors="ignore")


def _string_constants() -> list[str]:
    """Every string literal that is NOT a docstring."""
    tree = _tree()
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value not in docstrings]


# ── The vendor pin is gone ────────────────────────────────────────────────


def test_no_executable_string_pins_a_provider():
    for value in _string_constants():
        assert '"provider": "claude"' not in value, (
            "the retrieval path still pins provider=claude outside a docstring")


def test_no_dict_literal_sets_provider_to_a_vendor():
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and k.value == "provider"
                    and isinstance(v, ast.Constant)):
                raise AssertionError(
                    f"hybrid_retriever.py:{node.lineno}: provider is hardcoded "
                    f"to {v.value!r}; it must come from the resolver")


def test_the_sdlc_cli_helper_is_no_longer_imported():
    """The subtler half. cli_model_for_tier is SDLC's CLI resolver; importing
    it here made an SDLC env var move a retrieval model."""
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom):
            names = {a.name for a in node.names}
            assert "cli_model_for_tier" not in names, (
                f"hybrid_retriever.py:{node.lineno}: still imports "
                f"cli_model_for_tier from {node.module}")


# ── It asks for a tier instead ────────────────────────────────────────────


def test_both_helpers_route_through_the_model_router():
    src = _src()
    assert src.count("tier=Tier.SIMPLE") == 2, (
        "expected both _expand_query and _decompose_query to ask for a tier")
    assert src.count('legacy_hint="haiku"') == 2, (
        "D15 needs the pre-migration hint alongside the tier — the old code "
        "resolved cli_model_for_tier('haiku')")


def test_the_proxy_vocabulary_is_not_the_registry_vocabulary():
    """Why forwarding ResolvedModel.family as `provider` would not work. If
    this ever stops being true the comment in _expand_query should be revised
    rather than left to mislead."""
    proxy = PROXY.read_text(encoding="utf-8", errors="ignore")
    assert 'req.provider not in ("claude", "openai", "gemini")' in proxy, (
        "the proxy's accepted provider set changed — recheck whether the "
        "registry's families can now be forwarded directly")


# ── The switch ────────────────────────────────────────────────────────────


def test_expansion_is_off_unless_explicitly_enabled():
    """These add an LLM call to the retrieval hot path. They used to be gated
    on LLM_PROXY_URL being set, which was never an intentional switch — it
    meant a deployment that configured a proxy for unrelated reasons turned
    query expansion on without knowing. Swapping the transport without a real
    switch would have turned it on here, silently."""
    src = _src()
    assert 'os.getenv("QUERY_EXPANSION_ENABLED", "")' in src, (
        "no explicit switch — the transport swap would change behaviour")
    assert src.count("_query_expansion_enabled()") >= 3, (
        "both helpers must consult the switch (plus its definition)")


def test_the_switch_is_not_llm_proxy_url_any_more():
    """If LLM_PROXY_URL still gates these, the accidental switch is back."""
    for fn_name in ("_expand_query", "_decompose_query"):
        fn = next(n for n in ast.walk(_tree())
                  if isinstance(n, ast.FunctionDef) and n.name == fn_name)
        body = ast.unparse(fn)
        assert "LLM_PROXY_URL" not in body, (
            f"{fn_name} still reads LLM_PROXY_URL as its enable switch")


def test_the_file_still_parses():
    _tree()
