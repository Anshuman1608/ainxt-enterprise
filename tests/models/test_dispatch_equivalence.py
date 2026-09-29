# SPDX-License-Identifier: MIT
# ============================================================
# GOLDEN DISPATCH TABLE  (Phase 5 — the flag-OFF safety net)
# ============================================================
#
# Phase 5 collapses the fifteen hand-written `_try_*` method pairs in
# models/model_router.py into one family dispatcher, keeping the old methods as
# thin wrappers. That is the change plan.html asks for, and it has one sharp
# edge: the legacy methods ARE the TIER_GOVERNANCE_ENABLED=false path. Once they
# delegate to shared new code, turning the flag off no longer restores literally
# the bytes that were running before the deploy — which is the mitigation the
# phase's risk row leans on.
#
# So the equivalence is made testable instead of assumed. This module pins the
# COMPLETE observable behaviour of _dispatch() and _dispatch_stream() for every
# legacy tier under every gateway failure mode:
#
#     which gateway was called, with which model id, with which kwargs,
#     in which order, and what last_model_label / _last_actual_tier /
#     was_fallback came out the other side.
#
# GOLDEN was captured from the pre-refactor code and must not be edited to make
# a refactor pass. A diff here means routing changed for deployments that have
# governance switched off, which is exactly what must not happen.
#
# ── Two things are deliberately stubbed, and why ─────────────────────────────
#
# _resolve_tier_model() is replaced with a function returning "<family>/<tag>".
# The real one reads .env constants and falls back to a DB registry lookup, so
# the recorded model ids would depend on the machine. What must not drift is
# WHICH family and WHICH role tag each hop asks for, and the stub preserves
# that exactly while making the table reproducible in CI.
#
# _tier_label() is replaced with "LABEL[<tier>]". The real one reaches into
# gateway_local_llm's catalogue for the local tier, which needs a running
# Ollama. The labels built inline from the *_DISPLAY constants are untouched
# and still protected; only the three sites that delegate to _tier_label lose
# their exact string, and the tier they name is still pinned.
# ============================================================

import json
from pathlib import Path

import pytest

import models.model_router as mr
from models.model_router import ModelRouter

# Every legacy tier _dispatch knows about, plus one that it does not — the
# unknown-tier branch falls back to local and that behaviour is load-bearing
# (see _dispatch's "Fail SAFE" comment).
TIERS = [
    mr.TIER_SIMPLE, mr.TIER_MINI, mr.TIER_LOCAL_MINI, mr.TIER_MEDIUM,
    mr.TIER_DEEP, mr.TIER_COMPLEX, mr.TIER_HAIKU, mr.TIER_VISION,
    mr.TIER_GEMINI, mr.TIER_SOLUTION, mr.TIER_OPUS_48, mr.TIER_OPUS_5,
    mr.TIER_SONNET_5, mr.TIER_TERA, mr.TIER_LUNA, "no-such-tier",
]

# One scenario = (gateway modes by family, breakers forced open, compliance
# kwargs). Modes:
#   ok    — returns tokens
#   error — returns a leading-"Error" token. A DIFFERENT path from raising, and
#           the two dispatchers disagree about it: the blocking chain treats it
#           as failure and moves on, while most streaming methods yield it and
#           stop. That asymmetry is behaviour, not a bug to tidy away here.
#   raise — raises, exercising the except branch
#
# The precleared scenarios exist because _filter_kwargs_for decides which hops
# receive the compliance kwargs, per gateway signature. Without them the table
# records an empty kwarg set everywhere and would not notice that forwarding
# changed.
_PRECLEARED = {"precleared": True, "precleared_findings": ["f"]}

SCENARIOS = {
    "all_ok":                  ({}, set(), {}),
    "all_ok_precleared":       ({}, set(), _PRECLEARED),
    "openai_error":            ({"openai": "error"}, set(), {}),
    "claude_error":            ({"claude": "error"}, set(), {}),
    "gemini_error":            ({"gemini": "error"}, set(), {}),
    "local_error":             ({"local": "error"}, set(), {}),
    "openai_raise":            ({"openai": "raise"}, set(), {}),
    "openai_raise_precleared": ({"openai": "raise"}, set(), _PRECLEARED),
    "claude_raise":            ({"claude": "raise"}, set(), {}),
    "gemini_raise":            ({"gemini": "raise"}, set(), {}),
    "local_raise":             ({"local": "raise"}, set(), {}),
    # Several chains return their LAST hop's result without the leading-"Error"
    # check the earlier hops get. These two scenarios are the only way to see
    # that difference: they make a fallback hop fail softly after its primary
    # has already failed hard.
    "openai_raise_claude_error": ({"openai": "raise", "claude": "error"}, set(), {}),
    "local_raise_openai_error":  ({"local": "raise", "openai": "error"}, set(), {}),
    "all_raise":               ({"openai": "raise", "claude": "raise",
                                 "gemini": "raise", "local": "raise"}, set(), {}),
    "openai_cb_open":          ({}, {"openai"}, {}),
    "claude_cb_open":          ({}, {"claude"}, {}),
    "all_cb_open":             ({}, {"openai", "claude", "gemini", "local"}, {}),
}


class _FakeGateway:
    """Records every call and returns tokens as a LIST.

    A list rather than a string because the same fake serves both dispatchers:
    _collect() joins an iterable of strings into the blocking result, and the
    streaming path iterates it token by token. A bare string would be iterated
    character by character on the streaming path.
    """

    def __init__(self, family, log, mode="ok"):
        self.family = family
        self.log = log
        self.mode = mode
        self.available = True                       # read by _try_local_simple
        self._last_selected_model = f"{family}-picked"
        self._last_input_tokens = 0
        self._last_output_tokens = 0
        self._last_cache_read_tokens = 0
        self._last_cache_creation_tokens = 0

    def generate(self, prompt, model=None, **kw):
        # kwarg NAMES are recorded, not values: _filter_kwargs_for's job is to
        # drop kwargs a gateway does not declare, and a regression there shows
        # up here as a changed key set rather than as a crash in production.
        self.log.append((self.family, model, tuple(sorted(kw))))
        if self.mode == "raise":
            raise RuntimeError(f"{self.family} boom")
        if self.mode == "error":
            return [f"Error: {self.family} failed"]
        return [f"<{self.family}:{model}>"]


class _FakeBreaker:
    """Stands in for the module-level _CB_* singletons.

    `is_open` is a plain attribute because the production code reads it as one
    (`not _CB_OPENAI.is_open`), and .call() reproduces the real breaker's
    contract of raising when open rather than returning a sentinel.
    """

    def __init__(self, name, is_open=False):
        self.name = name
        self.is_open = is_open

    def call(self, fn, *a, **kw):
        if self.is_open:
            raise RuntimeError(f"CircuitBreaker[{self.name}] is OPEN")
        return fn(*a, **kw)


@pytest.fixture
def harness(monkeypatch):
    """Build a router whose entire outside world is recorded and deterministic."""

    def _build(modes=None, open_breakers=(), **router_kw):
        modes = modes or {}
        log = []
        gws = {f: _FakeGateway(f, log, modes.get(f, "ok"))
               for f in ("local", "openai", "claude", "gemini")}
        brk = {f: _FakeBreaker(f, f in open_breakers)
               for f in ("local", "openai", "claude", "gemini")}

        monkeypatch.setattr(ModelRouter, "_get_local", lambda self: gws["local"])
        monkeypatch.setattr(ModelRouter, "_get_openai", lambda self: gws["openai"])
        monkeypatch.setattr(ModelRouter, "_get_claude", lambda self: gws["claude"])
        monkeypatch.setattr(ModelRouter, "_get_gemini", lambda self: gws["gemini"])
        monkeypatch.setattr(mr, "_CB_LOCAL", brk["local"])
        monkeypatch.setattr(mr, "_CB_OPENAI", brk["openai"])
        monkeypatch.setattr(mr, "_CB_CLAUDE", brk["claude"])
        monkeypatch.setattr(mr, "_CB_GEMINI", brk["gemini"])
        monkeypatch.setattr(mr, "_resolve_tier_model",
                            lambda env_value, family, tag: f"{family}/{tag}")
        monkeypatch.setattr(mr, "_tier_label", lambda tier: f"LABEL[{tier}]")
        # Pinned so the table does not depend on .env. The chain is otherwise
        # empty in the OSS default, which would leave _try_openai_mini_stream's
        # fallback walk completely uncovered.
        monkeypatch.setattr(mr, "CHAT_FALLBACK_CHAIN", ["haiku", "local:pinned"])
        # The remaining .env leaks into the recorded table. _resolve_tier_model
        # is stubbed above, but these three reach dispatch directly — as the
        # model id for the solution and local_mini hops, and as the display
        # strings every label is built from. Unpinned, the table would encode
        # whatever the capturing developer happened to have in .env and fail
        # for everyone else.
        monkeypatch.setattr(mr, "SOLUTION_MODEL", "PINNED_SOLUTION_MODEL")
        monkeypatch.setattr(mr, "OPENAI_OSS_MODEL", "PINNED_OSS_MODEL")
        for _name in [n for n in dir(mr) if n.endswith("_DISPLAY")]:
            monkeypatch.setattr(mr, _name, f"DISPLAY[{_name}]")

        r = ModelRouter()
        r.last_model_label = "auto"
        r._last_actual_tier = ""
        return r, log

    return _build


def _jsonable(x):
    """Tuples → lists, so a record compares equal to its JSON round-trip."""
    if isinstance(x, (tuple, list)):
        return [_jsonable(i) for i in x]
    return x


def sync_record(r, log, tier, **kw):
    out, fb = r._dispatch(tier, "p", **kw)
    return _jsonable((tuple(log), out, fb, r.last_model_label, r._last_actual_tier))


def stream_record(r, log, tier, **kw):
    toks = list(r._dispatch_stream(tier, "p", **kw))
    return _jsonable((tuple(log), "".join(t for t in toks if isinstance(t, str)),
                      r.last_model_label))


# ── The table ───────────────────────────────────────────────────────────────
# Held as JSON beside this file rather than as a Python literal: it is data,
# not code, and a reviewer needs to be able to read the diff when it changes.
# Regenerate with scripts/ci/capture_dispatch_golden.py — ONLY when a behaviour
# change is intended and reviewed, never to make a refactor pass.
_GOLDEN_PATH = Path(__file__).with_name("dispatch_golden.json")
GOLDEN = json.loads(_GOLDEN_PATH.read_text())


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize("tier", TIERS)
def test_blocking_dispatch_matches_golden(harness, tier, scenario):
    modes, open_brk, kw = SCENARIOS[scenario]
    r, log = harness(modes, open_brk)
    assert sync_record(r, log, tier, **kw) == GOLDEN["sync"][f"{tier}|{scenario}"]


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize("tier", TIERS)
def test_streaming_dispatch_matches_golden(harness, tier, scenario):
    modes, open_brk, kw = SCENARIOS[scenario]
    r, log = harness(modes, open_brk)
    assert stream_record(r, log, tier, **kw) == GOLDEN["stream"][f"{tier}|{scenario}"]


def test_the_golden_table_covers_every_tier_and_scenario():
    """A missing key would make the parametrised tests KeyError rather than
    silently pass, but this says the shortfall in one line instead of 32."""
    expected = {f"{t}|{s}" for t in TIERS for s in SCENARIOS}
    assert set(GOLDEN["sync"]) == expected
    assert set(GOLDEN["stream"]) == expected


def test_privacy_local_only_fails_closed_without_touching_a_cloud_gateway(harness):
    """The hard invariant, stated at the dispatch layer rather than the routing one.

    Kept out of the parametrised table because it is the one case where the
    CORRECT answer is an error string: a local outage on restricted data must
    not reach openai or claude, even though the ordinary TIER_SIMPLE chain
    would try both.
    """
    r, log = harness({"local": "raise"})
    out, fb = r._dispatch(mr.TIER_SIMPLE, "p", privacy_local_only=True)

    assert [c[0] for c in log] == ["local"], "cloud gateway reached under privacy floor"
    assert out.startswith("Error: the in-house (local) model was requested")
    assert fb is False
    assert r._last_actual_tier == mr.TIER_SIMPLE


def test_solution_tier_reads_the_module_level_solution_model(harness):
    """SOLUTION_MODEL is resolved at IMPORT time from ENABLE_OPUS.

    The ENABLE_OPUS axis is therefore absent from the table above — flipping the
    env var inside a test would not change an already-imported constant. What
    the refactor must preserve is that the solution hop passes SOLUTION_MODEL
    itself, not a re-derived id, so that the existing import-time semantics
    survive. Asserting the constant is passed through verbatim pins that.
    """
    r, log = harness()
    r._dispatch(mr.TIER_SOLUTION, "p")
    assert log[0] == ("claude", "PINNED_SOLUTION_MODEL", ())
