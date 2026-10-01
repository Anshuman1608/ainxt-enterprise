# SPDX-License-Identifier: MIT
"""D81 — the OpenAI-compatible endpoint honours the id the caller named.

`_oai_model_hint()` prefix-matches a model name onto a routing hint, and the
hint is what reached the router. That destroys the id: `claude-opus-5-5` became
`opus-5`, which resolves through `CLAUDE_OPUS_5_MODEL` — empty on any
deployment configured through the admin screen rather than .env. Measured live
before this change, 9 of the 11 advertised cloud ids dispatched a *different*
model than the one requested:

    claude-opus-5-5             -> claude-sonnet-5-5
    claude-opus-5               -> claude-sonnet-5-5
    claude-sonnet-5             -> claude-sonnet-5-5
    claude-opus-4-8             -> claude-sonnet-5-5
    claude-opus-4-7             -> claude-opus-5
    claude-opus-4-6             -> claude-opus-5
    claude-opus-4-5-20251101    -> claude-opus-5
    claude-haiku-4-5-20251001   -> claude-sonnet-4-6
    claude-sonnet-4-5-20250929  -> claude-sonnet-5-5

model_router already resolves this correctly for every other caller — it
matches an exact registry model_id BEFORE the alias table, because several
registry ids are also alias keys. The fix is to stop pre-translating, so the
router gets an id it can match.

gateway.py does not import under pytest, so `_oai_explicit_model_id` is
compiled out of the source by AST and exercised directly. That is stronger
than matching source text: these assert what the function returns.
"""

from __future__ import annotations

import ast
import logging
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "gateway.py"

#: The ids GET /v1/models served on a Claude-only deployment, measured
#: 2026-10-01 from get_cli_style_models(). Every one must come back verbatim.
#:
#: D83 has since withdrawn three of them (the last three below) as retired, so
#: the live catalogue is 9. The list is kept at 11 on purpose: these cases
#: assert what _oai_explicit_model_id() does with an id the registry reports
#: as enabled, and the fake registry below is what decides that. The deny-list
#: is a separate layer, covered by test_a_deny_listed_id_is_refused_outright.
SERVED_CLOUD_IDS = [
    "claude-sonnet-5-5",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-opus-4-5-20251101",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5-20250929",
]


@pytest.fixture(scope="module")
def src() -> str:
    return GATEWAY.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def tree(src: str) -> ast.AST:
    return ast.parse(src)


def _fn(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in gateway.py")


@pytest.fixture(scope="module")
def explicit_model_id(tree: ast.AST):
    """gateway.py::_oai_explicit_model_id, compiled without importing gateway."""
    fn = _fn(tree, "_oai_explicit_model_id")
    fn.decorator_list = []
    mod = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    ns: dict = {"logger": logging.getLogger("test")}
    exec(compile(mod, str(GATEWAY), "exec"), ns)   # noqa: S102 — our own source
    return ns["_oai_explicit_model_id"]


@pytest.fixture
def registry(monkeypatch):
    """Install a fake enabled-model registry; returns the backing set."""
    enabled: set = set(SERVED_CLOUD_IDS)

    def _get_model(model_id):
        return {"model_id": model_id, "family": "anthropic"} if model_id in enabled else None

    import core.llm_provider_registry as reg
    monkeypatch.setattr(reg, "get_model", _get_model)
    return enabled


# ── every advertised id survives the round trip ────────────────────────────


@pytest.mark.parametrize("model_id", SERVED_CLOUD_IDS)
def test_an_advertised_id_comes_back_verbatim(explicit_model_id, registry, model_id):
    assert explicit_model_id(model_id) == model_id


def test_all_eleven_at_once(explicit_model_id, registry):
    """The count is the point: 2 of 11 were honoured before."""
    got = [explicit_model_id(m) for m in SERVED_CLOUD_IDS]
    assert got == SERVED_CLOUD_IDS


# ── and nothing else does ──────────────────────────────────────────────────


def test_a_prefix_match_is_not_enough(explicit_model_id, monkeypatch):
    """Revert guard: a prefix match returns a plausible id for all eleven.

    `claude-opus-5` is a prefix of `claude-opus-5-5`, and that collision is the
    whole defect. Only an exact registry row may match.
    """
    import core.llm_provider_registry as reg
    monkeypatch.setattr(
        reg, "get_model",
        lambda mid: {"model_id": mid} if mid == "claude-opus-5-5" else None)
    assert explicit_model_id("claude-opus-5-5") == "claude-opus-5-5"
    assert explicit_model_id("claude-opus-5") == ""
    assert explicit_model_id("claude-opus") == ""


def test_a_model_absent_from_the_registry_falls_through(explicit_model_id, registry):
    """An IDE sending gpt-4o must still reach the hint table."""
    for name in ("gpt-4o", "gpt-3.5-turbo", "gemini-pro", "something-invented"):
        assert explicit_model_id(name) == ""


def test_a_disabled_model_does_not_match(explicit_model_id, monkeypatch):
    """get_model only returns enabled rows; assert we rely on that, not on id shape."""
    import core.llm_provider_registry as reg
    monkeypatch.setattr(reg, "get_model", lambda _mid: None)
    for model_id in SERVED_CLOUD_IDS:
        assert explicit_model_id(model_id) == ""


def test_local_ids_are_excluded(explicit_model_id, monkeypatch):
    """They already dispatch exactly, and _use_local reads the hint."""
    import core.llm_provider_registry as reg
    monkeypatch.setattr(reg, "get_model", lambda mid: {"model_id": mid})
    assert explicit_model_id("local:llama3.2:1b") == ""
    assert explicit_model_id("LOCAL:Kimi-k2.5") == ""


@pytest.mark.parametrize("name", ["", "   ", "auto", "default", None])
def test_auto_and_empty_resolve_to_no_pick(explicit_model_id, monkeypatch, name):
    """Auto must stay Auto — it is the branch that applies the department ACL."""
    import core.llm_provider_registry as reg
    monkeypatch.setattr(reg, "get_model", lambda mid: None if mid in ("auto", "default") else {"model_id": mid})
    assert explicit_model_id(name) == ""


def test_a_registry_failure_degrades_to_the_hint_table(explicit_model_id, monkeypatch):
    """Never raise into the request path — the hint table is the fallback."""
    import core.llm_provider_registry as reg

    def _boom(_mid):
        raise RuntimeError("database is down")

    monkeypatch.setattr(reg, "get_model", _boom)
    assert explicit_model_id("claude-opus-5-5") == ""


def test_the_id_is_not_case_folded(explicit_model_id, monkeypatch):
    """Model ids are case-sensitive; a near-miss must not be silently accepted."""
    import core.llm_provider_registry as reg
    monkeypatch.setattr(
        reg, "get_model",
        lambda mid: {"model_id": mid} if mid == "claude-opus-5-5" else None)
    assert explicit_model_id("Claude-Opus-5-5") == ""


# ── the dispatcher reads it ────────────────────────────────────────────────


def test_the_route_object_prefers_the_explicit_id(tree):
    """`_oai_route` is the single dispatch point Phase 6.6 consolidated."""
    fn = _fn(tree, "openai_chat_completions")
    found = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and node.target.id == "_oai_route" and node.value is not None):
            found.append(ast.unparse(node.value))
    assert found, "_oai_route is no longer annotated-assigned in the handler"
    assert any("_explicit_id or _model_hint" in f for f in found), found


def test_the_explicit_id_is_resolved_in_the_handler(tree):
    """Computed beside _model_hint, from the raw request value."""
    fn = _fn(tree, "openai_chat_completions")
    calls = [ast.unparse(n) for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "_oai_explicit_model_id"]
    assert calls == ["_oai_explicit_model_id(req.model)"], calls


def test_the_hint_is_still_computed(tree):
    """Everything else reads it: gemini/local/deep/mini, the ACL skip,
    X-Model-Hint, the context-window sizing."""
    fn = _fn(tree, "openai_chat_completions")
    assert any(getattr(n.func, "id", "") == "_oai_model_hint"
               for n in ast.walk(fn) if isinstance(n, ast.Call))


# ── the startup audit follows the catalogue that serves the route ──────────


def test_the_audit_no_longer_calls_the_deleted_builder(tree, src: str):
    fn = _fn(tree, "_audit_model_hint_coverage")
    called = {getattr(n.func, "id", "") for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert "list_oai_models" not in called
    assert "_oai_advertised_model_ids" in called


def test_the_audit_reads_the_registry_catalogue(tree):
    """D80: the ids it audits must come from what list_models_compat serves."""
    fn = _fn(tree, "_oai_advertised_model_ids")
    body = ast.unparse(fn)
    assert "get_cli_style_models" in body
    assert "gateway_local_llm" in body


def test_the_audit_accepts_a_registry_id_without_a_hint(tree):
    """Under D81 a registry id is dispatched as itself, so requiring a hint
    for it would report correct behaviour as a gap."""
    fn = _fn(tree, "_audit_model_hint_coverage")
    body = ast.unparse(fn)
    assert "get_model" in body, "the audit still demands a hint for every id"
    assert "_oai_model_hint" in body


# ── the deny-list layer (D83/D88) ──────────────────────────────────────────


def test_a_blocked_id_is_not_resolved_as_an_explicit_pick(explicit_model_id,
                                                          monkeypatch):
    """get_enabled_models() applies the deny-list, and get_model() reads
    through it, so a retired id stops being an explicit pick for free."""
    import core.llm_provider_registry as reg
    monkeypatch.setattr(reg, "get_model", lambda mid: None)
    assert explicit_model_id("claude-opus-4-6") == ""


def test_a_deny_listed_id_is_refused_outright(src):
    """Without this gate the endpoint would answer a retired id by falling
    through to _oai_model_hint(), which prefix-matches it onto SOME other
    model and serves that — measured: all three now auto-route to
    llama3.2:1b. That is exactly the silent substitution D81 removed."""
    assert "_is_blocked_oai(req.model)" in src
    assert '"code": "model_not_available"' in src


def test_the_gate_runs_before_the_turn_is_traced(tree):
    """The CLI lane rejects before the routing log for a reason — a refused
    request should not first appear as a routed one. This asserts the same
    ordering here: the gate precedes the compliance pass that builds the
    prompt, so a 400 costs nothing."""
    fn = _fn(tree, "openai_chat_completions")
    lines = [n.lineno for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "_is_blocked_oai"]
    assert len(lines) == 1, f"expected one deny-list gate, found {len(lines)}"
    redaction = [n.lineno for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "validate_input"]
    assert redaction and lines[0] < min(redaction), (
        "the deny-list gate runs after the compliance pass; a refused request "
        "pays for the whole prompt build first")


def test_an_empty_model_does_not_trip_the_gate(src):
    """"" IS in BLOCKED_MODELS on an admin-only install (the blank SKU
    constants), and an OpenAI client that omits `model` must still auto-route
    rather than get a 400."""
    assert '(req.model or "").strip() and _is_blocked_oai(req.model)' in src
