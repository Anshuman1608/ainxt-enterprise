# SPDX-License-Identifier: MIT
"""Phase 6.6 — the tool-call channel resolver (D52 / D53).

A tool-call turn on ``/v1/chat/completions`` picked its provider AND its model
from hint literals and ``.env`` constants:

    _use_claude = _model_hint in ("claude", "solution", "haiku")   # the family
    _claude_tools_model = … else CLAUDE_PRIMARY_MODEL              # the SKU
    _tools_model        = … else OPENAI_CODING_MODEL               # the other SKU

So an Auto agentic turn — the browser agent's normal case — always ran
``OPENAI_CODING_MODEL`` through the proxy, whatever the administrator had
assigned. ``_oai_tool_channel`` replaces the family and the Auto SKU; the
explicit-pick rungs stay.

Two properties are asserted, and the first one is the point of the file:

  **D52 — the governed/pick split is DERIVED, never restated.** A hint is a
  capability request iff ``core.tiers.LEGACY_INBOUND_ALIASES`` maps it to a
  Tier. These tests read that table rather than listing hints, so the day someone adds a tier alias the partition follows
  automatically. A second hand-written list is how ``threads_router.py`` sat
  on the migrated-module list for six steps still passing
  ``synthesis_hint="solution"``.

  **D53 — the channel must be able to address the model.**
  ``stream_cloud_tools`` takes ``openai | claude | gemini``. A tier holding
  an Ollama model has no tool-call channel, and handing it to one produces a
  400 rather than a fallback. Same bar §N.1 step 6 set for CodeWiki and step
  10 reused for the ``ainxt`` CLI.

Unlike the other two Phase 6.6 files these are real unit tests: the helper is
a module-level function in ``gateway.py``, so it can be loaded on its own
without importing the ~80 routers attached at module scope.
"""

from __future__ import annotations

import ast
import pathlib
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "gateway.py"


@pytest.fixture(scope="module")
def helper():
    """`_oai_tool_channel` and its constant, lifted out of gateway.py.

    Compiled from the source of those two definitions alone. Importing
    gateway.py would attach every router and open a database; copying the
    function into the test would let the two drift, which is the failure this
    whole migration keeps finding. Neither is acceptable, so the real
    definition is executed in a bare namespace.
    """
    tree = ast.parse(GATEWAY.read_text(encoding="utf-8", errors="replace"))
    wanted = [n for n in tree.body
              if (isinstance(n, ast.FunctionDef) and n.name == "_oai_tool_channel")
              or (isinstance(n, ast.Assign)
                  and any(getattr(t, "id", "") == "_FAMILY_TO_TOOL_PROVIDER"
                          for t in n.targets))]
    assert len(wanted) == 2, (
        "_oai_tool_channel and/or _FAMILY_TO_TOOL_PROVIDER are no longer "
        "module-level definitions in gateway.py")

    import logging
    ns: dict = {"logger": logging.getLogger("test"), "Optional": __import__("typing").Optional}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "<gateway-subset>", "exec"), ns)
    return types.SimpleNamespace(fn=ns["_oai_tool_channel"],
                                 families=ns["_FAMILY_TO_TOOL_PROVIDER"])


class _Cand:
    def __init__(self, model_id, family):
        self.model_id, self.family = model_id, family


@pytest.fixture
def governed():
    """Governance is unconditional since Phase 8; kept so the call sites read clearly."""


def _pin_candidates(monkeypatch, candidates):
    """Make resolve_tier_candidates return exactly these, for any tier."""
    import core.tier_resolver as tr
    monkeypatch.setattr(tr, "resolve_tier_candidates",
                        lambda *a, **k: list(candidates), raising=True)


# ── D52: the partition is derived, not restated ────────────────────────────


def test_every_governed_alias_is_treated_as_a_capability(helper, governed, monkeypatch):
    """Derived from core.tiers.LEGACY_INBOUND_ALIASES: every alias that names a
    Tier is a capability request, with no edit here when one is added."""
    from core.tiers import LEGACY_INBOUND_ALIASES, Tier

    _pin_candidates(monkeypatch, [_Cand("some-model", "anthropic")])
    governed_hints = [h for h, t in LEGACY_INBOUND_ALIASES.items() if isinstance(t, Tier)]
    assert governed_hints, "the router exposes no governed aliases at all"

    for hint in governed_hints:
        provider, model_id = helper.fn(hint)
        assert provider == "claude" and model_id == "some-model", (
            f"{hint!r} maps to a governed tier but was treated as a user's pick")


def test_every_non_governed_alias_is_left_alone(helper, governed, monkeypatch):
    """§G. SKU aliases are a user's pick; resolving them through a tier would
    substitute a different model for the one that was asked for."""
    from core.tiers import EXPLICIT_MODEL, LEGACY_INBOUND_ALIASES

    _pin_candidates(monkeypatch, [_Cand("some-model", "anthropic")])
    picks = [h for h, t in LEGACY_INBOUND_ALIASES.items() if t == EXPLICIT_MODEL]
    assert "gemini" in picks and "opus-5" in picks, \
        "the fixture's assumption about the alias table no longer holds"

    for hint in picks:
        assert helper.fn(hint) == (None, ""), (
            f"{hint!r} is a user's pick and must not be re-resolved through a tier")


def test_no_hint_is_a_capability_request(helper, governed, monkeypatch):
    """Auto on a tool-call turn is agentic code generation against visible
    context — §D.2's `complex`. Before this it was OPENAI_CODING_MODEL, a
    vendor SKU the admin screen could not reach."""
    _pin_candidates(monkeypatch, [_Cand("assigned-model", "anthropic")])
    for empty in (None, "", "   "):
        assert helper.fn(empty) == ("claude", "assigned-model")


def test_an_unknown_model_id_is_a_pick(helper, governed, monkeypatch):
    """A raw model id is not an alias, so it is the user naming a model
    and must survive untouched — the same rule the ratchet applies."""
    _pin_candidates(monkeypatch, [_Cand("assigned-model", "anthropic")])
    assert helper.fn("some-vendor/some-model-v3") == (None, "")


# ── D53: the channel has to be able to reach it ────────────────────────────


def test_a_family_with_no_tool_channel_is_rejected(helper, governed, monkeypatch, caplog):
    """The live case on a deployment whose tiers hold Ollama models.
    stream_cloud_tools takes openai|claude|gemini only."""
    _pin_candidates(monkeypatch, [_Cand("llama3.2:1b", "ollama")])
    with caplog.at_level("WARNING"):
        assert helper.fn(None) == (None, "")
    assert "llama3.2:1b" in caplog.text, \
        "the rejected candidate is not named, so an operator cannot act on it"
    assert "no tool-call channel" in caplog.text


def test_it_walks_past_a_rejected_candidate_to_a_usable_one(helper, governed, monkeypatch):
    """The administrator's priority order is a LADDER. Stopping at the head
    would make one unusable assignment disable the whole path."""
    _pin_candidates(monkeypatch, [_Cand("llama3.2:1b", "ollama"),
                                  _Cand("claude-x", "anthropic")])
    assert helper.fn(None) == ("claude", "claude-x")


def test_a_blocked_model_is_skipped(helper, governed, monkeypatch):
    import core.model_registry as mr
    monkeypatch.setattr(mr, "BLOCKED_MODELS", {"retired-model"}, raising=False)
    _pin_candidates(monkeypatch, [_Cand("retired-model", "anthropic"),
                                  _Cand("current-model", "anthropic")])
    assert helper.fn(None) == ("claude", "current-model")


@pytest.mark.parametrize("family,provider", [
    ("anthropic", "claude"), ("openai", "openai"),
    ("google", "gemini"), ("gemini", "gemini"),
    ("openai_compatible", "openai"), ("generic_openai", "openai"),
])
def test_each_addressable_family_maps_to_its_channel(helper, governed, monkeypatch,
                                                     family, provider):
    _pin_candidates(monkeypatch, [_Cand("m", family)])
    assert helper.fn(None) == (provider, "m")


def test_an_empty_tier_falls_back_rather_than_failing(helper, governed, monkeypatch):
    """NoEligibleModel is the resolver's business, not this function's — a
    tool turn must still run."""
    import core.tier_resolver as tr
    from core.tier_resolver import NoEligibleModel

    def _boom(*a, **k):
        raise NoEligibleModel("nothing assigned")
    monkeypatch.setattr(tr, "resolve_tier_candidates", _boom, raising=True)
    assert helper.fn(None) == (None, "")


# ── D90: an explicit pick is served as itself ──────────────────────────────


def _pin_registry(monkeypatch, rows):
    import core.llm_provider_registry as reg
    monkeypatch.setattr(reg, "get_model", lambda mid: rows.get(mid), raising=True)


@pytest.mark.parametrize("family,provider", [
    ("anthropic", "claude"), ("openai", "openai"), ("google", "gemini"),
])
def test_an_explicit_pick_is_served_as_itself(helper, monkeypatch, family, provider):
    """The router's registry branch honours the pick, so tool calls must agree."""
    _pin_registry(monkeypatch, {"vendor-m": {"family": family}})
    _pin_candidates(monkeypatch, [_Cand("assigned-model", "anthropic")])
    assert helper.fn("whatever-hint", "vendor-m") == (provider, "vendor-m")


def test_the_pick_beats_the_hint_it_prefix_matches(helper, governed, monkeypatch):
    """`claude-opus-5-5` prefix-matches the hint `opus-5`, whose ladder rung is
    CLAUDE_OPUS_5_MODEL — blank on an admin-configured install."""
    _pin_registry(monkeypatch, {"claude-opus-5-5": {"family": "anthropic"}})
    _pin_candidates(monkeypatch, [_Cand("claude-sonnet-5-5", "anthropic")])
    assert helper.fn("opus-5", "claude-opus-5-5") == ("claude", "claude-opus-5-5")


def test_a_pick_with_no_tool_channel_is_served_by_auto(helper, monkeypatch, caplog):
    """An Ollama pick cannot carry tools, so it falls through to the tier's Auto model."""
    expected = ("claude", "claude-x")
    _pin_registry(monkeypatch, {"llama3.2:1b": {"family": "ollama"}})
    _pin_candidates(monkeypatch, [_Cand("claude-x", "anthropic")])
    with caplog.at_level("WARNING"):
        assert helper.fn(None, "llama3.2:1b") == expected
    assert "no tool-call channel" in caplog.text


def test_no_pick_leaves_the_tier_path_alone(helper, governed, monkeypatch):
    _pin_registry(monkeypatch, {})
    _pin_candidates(monkeypatch, [_Cand("claude-x", "anthropic")])
    assert helper.fn(None, "") == ("claude", "claude-x")
    assert helper.fn(None) == ("claude", "claude-x")


def test_the_handler_passes_the_pick(src):
    assert "_oai_tool_channel(_model_hint, _explicit_id)" in src


def test_an_unlabelled_turn_is_not_billed_as_openai_coding(src):
    """D91: the seed and the fallback named a SKU that did not run."""
    handler = src[src.index("\ndef openai_chat_completions("):]
    handler = handler[:handler.index("def _record_usage") + 2000]
    assert '"model": _OPENAI_CODING, "cost"' not in handler
    assert 'req.model or _OPENAI_CODING\n' not in handler.split("def _record_usage", 1)[1]
    assert '_meta["model"] = req.model or "unknown"' in handler


# ── The call sites consume it ──────────────────────────────────────────────


@pytest.fixture(scope="module")
def src() -> str:
    return GATEWAY.read_text(encoding="utf-8", errors="replace")


def test_the_channel_choice_follows_the_model(src):
    """Picking the vendor from a hint literal and then asking a different
    tier for the model is how the two came to disagree."""
    assert 'if _tool_provider:\n                    _use_claude = (_tool_provider == "claude")' in src


def test_both_tool_branches_prefer_the_resolved_model(src):
    assert '_tool_model_id if _tool_provider == "claude" else' in src
    assert 'if _tool_provider in ("openai", "gemini") and _tool_model_id:' in src


def test_a_sku_pick_is_the_registry_model_it_names(src):
    """§G: opus-4-8 / opus-5 / sonnet-5 resolve through the registry, not env (Phase 8)."""
    assert '_cl_alias(_model_hint) if _model_hint in ("opus-4-8", "opus-5", "sonnet-5")' in src


def test_the_image_turn_still_forces_the_proxy(src):
    """A capability requirement, not a routing preference: the Claude stream
    drops image_url parts. It has to override the tier as well as the hint,
    and it is now applied AFTER both rather than only inside the passthrough
    branch."""
    assert ("if _passthrough and _force_proxy_for_image:\n"
            "                    _use_claude = False") in src
