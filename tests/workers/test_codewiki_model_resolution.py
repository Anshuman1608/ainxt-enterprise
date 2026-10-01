# SPDX-License-Identifier: MIT
"""§N.1 step 6 — CodeWiki's LLM comes from the `medium` tier.

CodeWiki is configured entirely outside the platform: CODEWIKI_BASE_URL and
CODEWIKI_API_KEY are hard-required, plus three CODEWIKI_*_MODEL vars, written
into the third-party CLI's own persistent config via `codewiki config set` on
every job. An operator configures their LLM twice and the second copy is
invisible to the Tiers screen.

The first version of this work filtered candidates down to OpenAI-compatible
families, on the belief that CodeWiki spoke no other API. That was WRONG, and
test_anthropic_is_a_supported_provider is here so it stays fixed: codewiki
2.0.0's Configuration.provider accepts "openai-compatible", "atlas-cloud",
"anthropic", "bedrock" and "azure-openai" (cli/models/config.py:125). Excluding
Anthropic would have left a Claude-only deployment — which is what this shipped
to — permanently on the env fallback for no reason.

The real constraint is narrower and is what the tests below pin: every
non-subscription provider must have a base_url, because
Configuration.validate() calls validate_url() before anything else, Anthropic
included. A provider row with no base_url is unusable however capable its
model is — and the fix for that is the administrator filling the field in, not
this file carrying a vendor default.

Source-based: codewiki_worker imports GitPython and opens a DB connection at
module scope.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKER = ROOT / "workers" / "codewiki_worker.py"


def _src() -> str:
    return WORKER.read_text(encoding="utf-8", errors="ignore")


def _func(name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(_src())):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in codewiki_worker.py — did it move?")


# ── The OpenAI-compatibility filter ───────────────────────────────────────


def _provider_map() -> dict:
    for node in ast.walk(ast.parse(_src())):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "_FAMILY_TO_CODEWIKI_PROVIDER"
                        for t in node.targets)):
            return ast.literal_eval(node.value)
    raise AssertionError("_FAMILY_TO_CODEWIKI_PROVIDER is not a literal dict")


def test_anthropic_is_a_supported_provider():
    """The correction. codewiki 2.0.0 accepts provider="anthropic"; filtering
    Claude out left a Claude-only deployment on the env fallback for nothing."""
    assert _provider_map().get("anthropic") == "anthropic"


def test_openai_compatible_families_map_to_the_clis_own_name():
    """The CLI's vocabulary is "openai-compatible", not "openai" — sending the
    family name verbatim would be rejected."""
    m = _provider_map()
    for fam in ("openai", "generic_openai", "ollama", "local"):
        assert m.get(fam) == "openai-compatible", fam


def test_gemini_is_not_mapped():
    """codewiki has no Gemini provider. Google does publish an
    OpenAI-compatible endpoint, but the base_url on a gemini provider row is
    the NATIVE one — the platform's own Gemini gateway uses it — so calling it
    "openai-compatible" would point CodeWiki at a different protocol."""
    assert "gemini" not in _provider_map()


def test_the_resolved_provider_is_passed_to_the_cli():
    """Resolving a provider and then not sending --provider would leave the
    CLI on its "openai-compatible" default and 400 every Anthropic call."""
    src = _src()
    assert 'os.getenv("CODEWIKI_PROVIDER") or (_tier_cfg or {}).get("provider")' in src
    assert '"--provider", provider' in src


def test_a_candidate_without_a_base_url_is_rejected():
    """Configuration.validate() calls validate_url() for every
    non-subscription provider, Anthropic included, so a provider row with no
    base_url cannot be used at all."""
    src = _src()
    assert 'if not (c.base_url or "").strip():' in src
    assert "provider has no base_url set" in src


def test_no_vendor_endpoint_is_hardcoded_as_a_default():
    """Substituting a vendor's public endpoint when the row has no base_url
    would re-introduce the hardcoded endpoint this migration removes, and
    would silently override an administrator who needs a regional or proxied
    one.

    Over the AST, not the text: the module docstring NAMES the endpoint it
    must not use, which is the explanation a reader needs. A grep would flag
    the documentation of the fix as the defect — the same reason
    scripts/ci/release_checks.py::check_tier_migration parses.
    """
    tree = ast.parse(_src())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if node.value in docstrings:
            continue
        for literal in ("api.anthropic.com", "api.openai.com",
                        "generativelanguage.googleapis.com"):
            assert literal not in node.value, (
                f"codewiki_worker.py:{node.lineno}: {literal} is hardcoded as "
                f"a fallback endpoint")


def test_the_rejection_reason_names_each_candidate():
    """"no usable model" without saying which one failed and why is a support
    ticket. The two reasons are different fixes: an unsupported family means
    reassign the tier, a missing base_url means fill in one field."""
    src = _src()
    assert "rejected.append" in src
    assert "CodeWiki has no such provider" in src


def test_it_falls_back_rather_than_raising_when_nothing_qualifies():
    """A tier with no OpenAI-compatible model must leave the existing env path
    exactly as it was, not break a deployment that was working."""
    fn = _func("_codewiki_llm_from_tier")
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert any(isinstance(r.value, ast.Constant) and r.value.value is None
               for r in returns), (
        "_codewiki_llm_from_tier never returns None, so the caller has no "
        "fallback signal")


def test_the_fallback_names_the_screen_that_fixes_it():
    """A warning that does not say what to do produces a support ticket."""
    src = _src()
    assert "Model Governance" in src
    assert "LLM Providers" in src


# ── Precedence and the ladder ─────────────────────────────────────────────


def test_main_and_fallback_come_from_the_candidate_ladder():
    """CodeWiki's --main-model/--fallback-model pair IS a priority ladder, so
    the first two eligible candidates map onto it directly."""
    src = _src()
    assert "resolve_tier_candidates" in src
    assert "usable[0]" in src
    assert "usable[1][0] if len(usable) > 1 else main" in src, (
        "a single-candidate tier must give main == fallback — that is what "
        "CODEWIKI_FALLBACK_MODEL's default already does and is not a bug")


def test_the_fallback_model_flag_uses_the_resolved_value():
    """It used to inline os.getenv(...) in the argv list; if the flag stops
    reading the computed variable the tier's second candidate is silently
    dropped."""
    assert '"--fallback-model", fallback_model,' in _src()


# ── Credentials ───────────────────────────────────────────────────────────


def test_a_keyless_provider_gets_a_placeholder_not_an_empty_string():
    """Ollama and most self-hosted OpenAI-compatible servers need no key.
    `codewiki config set` persists an empty --api-key and later reports
    itself unconfigured, which is a confusing way to fail."""
    src = _src()
    assert "_CODEWIKI_NO_AUTH_PLACEHOLDER" in src
    assert 'api_key or _CODEWIKI_NO_AUTH_PLACEHOLDER' in src


def test_the_credential_comes_from_the_vault_not_an_env_var():
    assert "resolve_credential" in _src()


def test_resolution_failure_never_breaks_a_job():
    """This runs on every CodeWiki job. A resolver exception must degrade to
    the env path, not take the job with it."""
    fn = _func("_codewiki_llm_from_tier")
    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "_codewiki_llm_from_tier has no exception handling"


def test_the_file_still_parses():
    ast.parse(_src())
