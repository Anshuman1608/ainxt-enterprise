# SPDX-License-Identifier: MIT
"""§N.1 step 10 — the tier → concrete model id resolver for the SDLC CLI.

The SDLC CLI phases (classify / plan / implement / coder) spawn the `ainxt`
binary with ``--model <id>``, so the model name leaves the process and the
answer has to be a string, not routing kwargs. What that string came from
before this step was ``cli_model_for_tier``'s ``_tier_to_role`` — a map that
hardcoded ``("anthropic", …)`` for three tiers and ``("openai", …)`` for two
and never looked at the tier assignments at all. Changing the `complex`
assignment on Admin > Model Governance had **zero** effect on the phases that
write the code.

The sharpest thing this file pins is the ADDRESSABILITY bar, and it is the
part this file got WRONG on the first attempt.

The original bar was a model-id prefix, reasoning that the CLI calls back
into this platform and the compat router rewrites unknown prefixes. The real
constraint is narrower and earlier: the `ainxt` binary accepts only the model
aliases in its OWN config (bin/config.toml → ~/.ainxt/config.toml) and each
alias's underlying `model` value. Anything else is refused before a request
is made. On a default install that config also points the CLI straight at the
provider, so the compat router is not in the path at all.

A prefix is therefore necessary and nowhere near sufficient — every id the
CLI refused in the incident that exposed this began with "claude", so the
prefix test stayed green while a live PLAN phase suspended with
"CLI exited with code 1". The bar now reads the CLI's own config, and these
tests supply that config rather than inheriting the developer's.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import core.model_registry as mr

ROOT = pathlib.Path(__file__).resolve().parents[2]


class _Cand:
    """The shape resolve_tier_candidates returns, reduced to what is read."""

    def __init__(self, model_id, family="anthropic"):
        self.model_id = model_id
        self.family = family


@pytest.fixture(autouse=True)
def _quiet_warn_cache():
    """The fallback warning is deduplicated per (tier, reasons) for the life of
    the process — a pipeline resolving one phase forty times should log once,
    not forty times. Cleared between tests so each one can observe it."""
    mr._CLI_TIER_FALLBACK_WARNED.clear()
    mr._CLI_OVERRIDE_WARNED.clear()
    yield
    mr._CLI_TIER_FALLBACK_WARNED.clear()
    mr._CLI_OVERRIDE_WARNED.clear()


@pytest.fixture
def governed(monkeypatch):
    """A settable candidate list (governance is always on since Phase 8)."""
    box = {"candidates": [], "raises": None}

    def _resolve(tier, constraints=None, **kw):
        if box["raises"]:
            raise box["raises"]
        box["asked"] = (tier, constraints)
        return list(box["candidates"])

    monkeypatch.setattr("core.tier_resolver.resolve_tier_candidates", _resolve)
    monkeypatch.setattr(mr, "LLM_PROVIDER", "cloud")
    return box


# ── The addressability bar ─────────────────────────────────────────────────
#
# This section was rewritten after the first version of it passed while the
# thing it was guarding failed in production. It asserted a model-id PREFIX,
# on the theory that the CLI calls back into this platform and the compat
# router rewrites unknown prefixes. In fact the `ainxt` binary refuses any id
# that is not in its OWN config, before making a request — and every id it
# refused in that incident began with "claude", so the prefix test was green
# throughout. The bar is now the CLI's own vocabulary, and these tests supply
# that config rather than reading whatever the developer's machine has.


@pytest.fixture
def cli_config(tmp_path, monkeypatch):
    """Point the resolver at a CLI config we control."""
    def _write(body: str):
        cfg = tmp_path / "config.toml"
        cfg.write_text(body, encoding="utf-8")
        monkeypatch.setattr(mr, "_cli_config_path", lambda: cfg)
        mr._CLI_MODELS_CACHE.clear()
        return cfg
    return _write


@pytest.fixture
def no_cli_config(tmp_path, monkeypatch):
    """A process that cannot see the CLI config — e.g. the gateway, which
    does not mount it and never spawns the binary."""
    monkeypatch.setattr(mr, "_cli_config_path", lambda: tmp_path / "absent.toml")
    mr._CLI_MODELS_CACHE.clear()


_ONE_ALIAS = '''
[models]
default = "sdlc-model"

[model.sdlc-model]
model = "claude-sonnet-4-6"
base_url = "https://api.anthropic.com/v1"
'''


def test_the_alias_and_its_underlying_model_are_both_accepted(cli_config):
    """Measured against the real binary: `--model sdlc-model` and
    `--model claude-sonnet-4-6` both work; anything else is refused."""
    cli_config(_ONE_ALIAS)
    assert mr.cli_acceptable_model_ids() == {"sdlc-model", "claude-sonnet-4-6"}
    assert mr.cli_model_is_addressable("sdlc-model")
    assert mr.cli_model_is_addressable("claude-sonnet-4-6")


@pytest.mark.parametrize("model_id", [
    "claude-sonnet-4-5-20250929",   # the exact id that suspended a live PLAN phase
    "claude-opus-5", "gpt-5.4", "gemini-3.5-flash", "llama3.2:1b", "",
])
def test_an_id_the_cli_does_not_know_is_refused(cli_config, model_id):
    """The regression this file exists for. Note the first two: a plausible
    Anthropic id that the CLI has never heard of is exactly the case the
    prefix check waved through."""
    cli_config(_ONE_ALIAS)
    assert not mr.cli_model_is_addressable(model_id)


def test_a_config_with_several_aliases_widens_what_is_assignable(cli_config):
    """The operator's lever. Governing the CLI phases from the Tiers screen
    requires a CLI config that knows the models they intend to assign — the
    tier assignment cannot widen it on its own."""
    cli_config(_ONE_ALIAS + '''
[model.fast]
model = "claude-haiku-4-5-20251001"
''')
    assert mr.cli_model_is_addressable("claude-haiku-4-5-20251001")
    assert mr.cli_model_is_addressable("fast")


def test_the_accepted_set_is_recomputed_when_the_config_changes(cli_config):
    """sdlc-setup.sh regenerates the file; a cache keyed on nothing would
    pin the old answer until a restart."""
    cli_config(_ONE_ALIAS)
    assert not mr.cli_model_is_addressable("claude-opus-4-7")
    cli_config(_ONE_ALIAS + '\n[model.deep]\nmodel = "claude-opus-4-7"\n')
    assert mr.cli_model_is_addressable("claude-opus-4-7")


def test_an_unreadable_config_falls_back_to_the_prefix_heuristic(no_cli_config):
    """Returning "nothing is acceptable" here would make the gateway reject
    every candidate for a spawn it never performs. The weaker answer is
    correct precisely where it cannot matter: only the SDLC worker mounts the
    config, and only the SDLC worker spawns the binary."""
    assert mr.cli_acceptable_model_ids() == frozenset()
    assert mr.cli_model_is_addressable("claude-sonnet-4-6")
    assert mr.cli_model_is_addressable("gemini-3.5-flash")
    assert not mr.cli_model_is_addressable("mistral-large-2")


def test_a_malformed_config_falls_back_rather_than_raising(cli_config):
    cli_config("this is not valid toml [[[")
    assert mr.cli_acceptable_model_ids() == frozenset()
    assert mr.cli_model_is_addressable("claude-sonnet-4-6")


def test_the_prefix_list_still_matches_the_compat_router():
    """The fallback set is still a mirror of
    routers/messages_compat_router.py::_normalise_model, so the degraded
    answer stays honest about what that endpoint would keep. Asserted over
    the AST: the router is the inbound CLI/IDE boundary and §E holds it until
    Phase 9/10, so it is read, never edited."""
    src = (ROOT / "routers" / "messages_compat_router.py").read_text(encoding="utf-8")
    found = []
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "startswith"
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Tuple)):
            values = [e.value for e in node.args[0].elts
                      if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if "claude" in values and "gemini" in values:
                found.append(tuple(values))
    assert found, ("could not find _normalise_model's prefix tuple — if it was "
                   "refactored, re-point this test rather than deleting it")
    assert set(mr.CLI_ADDRESSABLE_MODEL_PREFIXES) == set(found[0])


# ── Resolution order ────────────────────────────────────────────────────────


def test_the_tier_head_wins(governed, cli_config):
    from core.tiers import Tier

    cli_config(_ONE_ALIAS + '\n[model.a]\nmodel = "model-a"\n'
                            '\n[model.b]\nmodel = "model-b"\n')
    governed["candidates"] = [_Cand("model-a"), _Cand("model-b")]
    assert mr.cli_tier_model_id(Tier.COMPLEX, "complex") == "model-a"


def test_an_unaddressable_head_is_skipped_for_the_next_candidate(governed, cli_config):
    """The administrator's first choice is a model the CLI cannot reach. It is
    skipped IN FAVOUR OF the next assigned candidate — not fallen back past
    the whole tier — so the admin's priority order still decides among the
    models that work."""
    from core.tiers import Tier

    cli_config(_ONE_ALIAS + '\n[model.known]\nmodel = "known-model"\n')
    governed["candidates"] = [_Cand("claude-sonnet-4-5-20250929"),
                              _Cand("known-model")]
    assert mr.cli_tier_model_id(Tier.COMPLEX, "complex") == "known-model"


def test_a_blocked_candidate_is_skipped(governed, cli_config):
    from core.tiers import Tier

    cli_config(_ONE_ALIAS + '\n[model.g]\nmodel = "gpt-5.2"\n'
                            '\n[model.k]\nmodel = "known-model"\n')
    governed["candidates"] = [_Cand("gpt-5.2"), _Cand("known-model")]
    assert "gpt-5.2" in mr.BLOCKED_MODELS, "fixture assumes gpt-5.2 is retired"
    assert mr.cli_tier_model_id(Tier.COMPLEX, "complex") == "known-model"


def test_nothing_usable_falls_back_and_NAMES_each_rejection(governed, caplog, cli_config):
    """A fallback that says only "no model available" leaves an operator
    guessing which of their assignments is the problem. Step 6 learned this on
    CodeWiki; the message shape is deliberately the same."""
    from core.tiers import Tier

    cli_config(_ONE_ALIAS)
    governed["candidates"] = [_Cand("mistral-large-2", "openai_compatible"),
                              _Cand("deepseek-v3", "openai_compatible")]
    with caplog.at_level("WARNING"):
        out = mr.cli_tier_model_id(Tier.COMPLEX, "complex")

    assert out == mr._legacy_cli_model_for_tier("complex")
    text = caplog.text
    assert "mistral-large-2" in text and "deepseek-v3" in text
    assert "Model Governance" in text


def test_the_fallback_warning_is_logged_once_per_reason(governed, caplog, cli_config):
    from core.tiers import Tier

    cli_config(_ONE_ALIAS)
    governed["candidates"] = [_Cand("deepseek-v3", "openai_compatible")]
    with caplog.at_level("WARNING"):
        for _ in range(5):
            mr.cli_tier_model_id(Tier.COMPLEX, "complex")
    assert caplog.text.count("deepseek-v3") == 1


def test_a_resolver_failure_falls_back_rather_than_raising(governed):
    """An SDLC run must not die because Postgres blinked while resolving a
    tier. It degrades to the .env chain, which is D15's whole point."""
    from core.tiers import Tier

    governed["raises"] = RuntimeError("database is down")
    assert mr.cli_tier_model_id(Tier.COMPLEX, "complex") == \
        mr._legacy_cli_model_for_tier("complex")


# ── The two things that must be checked BEFORE the tier ─────────────────────


def test_local_only_short_circuits_before_the_tier_is_consulted(governed, monkeypatch):
    """LLM_PROVIDER=local means no cloud model id may reach a provider. A
    governed answer is still a cloud id, so the posture is checked first —
    the candidate list is never even asked for."""
    from core.tiers import Tier

    monkeypatch.setattr(mr, "LLM_PROVIDER", "local")
    governed["candidates"] = [_Cand("claude-sonnet-5")]
    assert mr.cli_tier_model_id(Tier.COMPLEX, "complex") == mr.LOCAL_LLM_MODEL_NAME
    assert "asked" not in governed


def test_an_operator_pin_beats_the_assignment(governed, caplog):
    from core.tiers import Tier

    governed["candidates"] = [_Cand("claude-sonnet-5")]
    with caplog.at_level("WARNING"):
        out = mr.cli_tier_model_id(Tier.COMPLEX, "complex", "my-inhouse-qwen",
                                   override_name="SDLC_CLI_PLAN_MODEL")
    assert out == "my-inhouse-qwen"
    assert "DEPRECATED" in caplog.text


def test_a_pin_naming_a_TIER_is_still_read_as_a_tier(governed):
    """SDLC_CLI_PLAN_MODEL=solution has always meant "the solution tier".
    Reading it as a model id would hand the CLI the literal string
    "solution"."""
    from core.tiers import Tier

    governed["candidates"] = [_Cand("claude-sonnet-5")]
    assert mr.cli_tier_model_id(Tier.COMPLEX, "complex", "solution") == \
        mr._legacy_cli_model_for_tier("solution")


def test_the_historical_local_pin_still_means_what_it_meant(governed):
    """"local" mapped to CLAUDE_HAIKU, not to the in-house model: the ainxt
    CLI has no Ollama bridge. Surprising, and preserved because changing it
    would move an operator's pin without telling them."""
    from core.tiers import Tier

    governed["candidates"] = [_Cand("claude-sonnet-5")]
    assert mr.cli_tier_model_id(Tier.COMPLEX, "complex", "local") == \
        mr._role_model(mr.CLAUDE_HAIKU, "anthropic", "haiku")


def test_the_role_preference_reaches_the_resolver(governed):
    """§M.3a. The governance SCAN reviewer asks for role='review'."""
    from core.tier_resolver import ROLE_REVIEW
    from core.tiers import Tier

    governed["candidates"] = [_Cand("claude-opus-5")]
    mr.cli_tier_model_id(Tier.COMPLEX, "complex", require_role=ROLE_REVIEW)
    _tier, constraints = governed["asked"]
    assert constraints.require_role == ROLE_REVIEW


# ── The named phases ────────────────────────────────────────────────────────


@pytest.mark.parametrize("fn_name,tier_value,env_var", [
    ("cli_classify_model",  "simple",  "SDLC_CLI_CLASSIFY_MODEL"),
    ("cli_plan_model",      "complex", "SDLC_CLI_PLAN_MODEL"),
    ("cli_implement_model", "complex", "SDLC_CLI_IMPLEMENT_MODEL"),
    ("cli_coder_model",     "complex", "SDLC_MODEL_CODER"),
])
def test_each_named_phase_asks_for_its_tier(governed, monkeypatch,
                                            fn_name, tier_value, env_var):
    monkeypatch.delenv(env_var, raising=False)
    governed["candidates"] = [_Cand("claude-sonnet-5")]
    getattr(mr, fn_name)()
    tier, _c = governed["asked"]
    assert tier.value == tier_value


@pytest.mark.parametrize("fn_name,env_var", [
    ("cli_classify_model",  "SDLC_CLI_CLASSIFY_MODEL"),
    ("cli_plan_model",      "SDLC_CLI_PLAN_MODEL"),
    ("cli_implement_model", "SDLC_CLI_IMPLEMENT_MODEL"),
    ("cli_coder_model",     "SDLC_MODEL_CODER"),
])
def test_each_named_phase_honours_its_pin(governed, monkeypatch, fn_name, env_var):
    monkeypatch.setenv(env_var, "claude-opus-4-8")
    governed["candidates"] = [_Cand("claude-sonnet-5")]
    assert getattr(mr, fn_name)() == "claude-opus-4-8"


def test_the_provider_biased_helpers_are_gone():
    """plan.html §F: "_tier_to_role hardcodes ("anthropic", …) for 3 of 5 tiers
    and ("openai", …) for 2 — the single most provider-biased map in the
    codebase. Replaced wholesale." The map survives only inside
    _legacy_cli_model_for_tier as the flag-off path, which Phase 8 removes
    with the rest of the .env constants."""
    assert not hasattr(mr, "cli_model_for_tier")
    assert not hasattr(mr, "cli_model_for")
    assert not hasattr(mr, "openai_model_for_tier")
