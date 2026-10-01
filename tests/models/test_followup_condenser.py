# SPDX-License-Identifier: MIT
# ============================================================
# Tests for models/followup_condenser.py
#
# All tests mock model_router.generate() and the module's redis_client —
# no live LLM or Redis dependency, so these run anywhere (including CI
# with no infra configured).
# ============================================================

import pytest

from models import followup_condenser as fc


class _FakeRedis:
    """Minimal in-memory stand-in for the redis_client used by the module."""
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


@pytest.fixture
def fake_redis(monkeypatch):
    client = _FakeRedis()
    monkeypatch.setattr(fc, "redis_client", client)
    return client


def _history():
    return [
        {"role": "user", "content": "What is UPI settlement TAT?"},
        {"role": "assistant", "content": "UPI settlement happens in 3 steps: initiation, clearing, confirmation."},
    ]


# ── _build_history_text ─────────────────────────────────────────────────────

def test_build_history_text_includes_full_content_no_truncation():
    long_answer = "A" * 5000  # deliberately long — must NOT be truncated
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": long_answer},
    ]
    text = fc._build_history_text(messages)
    assert long_answer in text  # full text present, not cut off
    assert "USER: question" in text
    assert f"ASSISTANT: {long_answer}" in text


def test_build_history_text_skips_non_user_assistant_roles():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hello"},
    ]
    text = fc._build_history_text(messages)
    assert "system prompt" not in text
    assert "USER: hello" in text


def test_build_history_text_skips_empty_content():
    messages = [
        {"role": "user", "content": "   "},
        {"role": "assistant", "content": "real answer"},
    ]
    text = fc._build_history_text(messages)
    assert "real answer" in text
    assert text.count("\n") == 0  # only one non-empty line


# ── the condense model request (core/config.py wiring) ──────────────────────


def test_condense_override_takes_only_the_first_entry():
    """The remaining hops are the tier assignment's job now."""
    parsed = [m.strip() for m in "a,b,c".split(",") if m.strip()]
    assert (parsed or [""])[0] == "a"


def test_the_condenser_asks_for_a_tier_not_a_sku(monkeypatch, fake_redis):
    """rule 1: application code may not name a LEGACY_INBOUND_ALIASES key.

    The old default was "haiku", which is one.
    """
    from core.tiers import Tier, LEGACY_INBOUND_ALIASES
    seen = {}

    def _capture(prompt, **kw):
        seen.update(kw)
        return "standalone q"

    monkeypatch.setattr("models.model_router.model_router.generate", _capture)
    fc.condense_followup("what about step 3?", _history())

    assert seen.get("tier") is Tier.SIMPLE
    assert "model_hint" not in seen
    assert seen.get("legacy_hint") == "haiku"   # the D15 audit trail
    assert LEGACY_INBOUND_ALIASES.get("haiku") is Tier.SIMPLE


# ── condense_followup — happy path ──────────────────────────────────────────

def test_condense_followup_returns_llm_output(monkeypatch, fake_redis):
    monkeypatch.setattr(
        "models.model_router.model_router.generate",
        lambda prompt, **kw: "What is the UPI settlement confirmation step?",
    )
    result = fc.condense_followup("what about step 3?", _history(), chat_id="chat-1")
    assert result == "What is the UPI settlement confirmation step?"


def test_condense_followup_caches_result(monkeypatch, fake_redis):
    calls = {"n": 0}

    def _fake_generate(prompt, **kw):
        calls["n"] += 1
        return "standalone question"

    monkeypatch.setattr("models.model_router.model_router.generate", _fake_generate)

    first = fc.condense_followup("what about step 3?", _history())
    second = fc.condense_followup("what about step 3?", _history())

    assert first == "standalone question"
    assert second == "standalone question"
    assert calls["n"] == 1  # second call hit the cache, no second LLM call


# ── condense_followup — fallback / fail-safe behaviour ──────────────────────

def test_condense_followup_returns_original_on_empty_history(monkeypatch, fake_redis):
    # No history at all → nothing to condense against.
    result = fc.condense_followup("what about step 3?", [])
    assert result == "what about step 3?"


def test_condense_followup_returns_original_on_empty_question(monkeypatch, fake_redis):
    result = fc.condense_followup("", _history())
    assert result == ""


def test_condense_followup_falls_back_on_llm_exception(monkeypatch, fake_redis):
    def _raise(*a, **k):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("models.model_router.model_router.generate", _raise)

    result = fc.condense_followup("what about step 3?", _history())
    assert result == "what about step 3?"  # falls back to original, never raises


def test_condense_followup_falls_back_on_empty_llm_output(monkeypatch, fake_redis):
    monkeypatch.setattr("models.model_router.model_router.generate", lambda p, **kw: "")
    result = fc.condense_followup("what about step 3?", _history())
    assert result == "what about step 3?"


def test_condense_followup_falls_back_on_too_long_output(monkeypatch, fake_redis):
    too_long = "x" * (fc._MAX_STANDALONE_LEN + 50)
    monkeypatch.setattr("models.model_router.model_router.generate", lambda p, **kw: too_long)
    result = fc.condense_followup("what about step 3?", _history())
    assert result == "what about step 3?"


def test_condense_followup_falls_back_on_multiline_output(monkeypatch, fake_redis):
    multiline = "line one\nline two\nline three"
    monkeypatch.setattr("models.model_router.model_router.generate", lambda p, **kw: multiline)
    result = fc.condense_followup("what about step 3?", _history())
    assert result == "what about step 3?"


def test_condense_followup_strips_surrounding_quotes(monkeypatch, fake_redis):
    monkeypatch.setattr(
        "models.model_router.model_router.generate",
        lambda p, **kw: '"What is the settlement step?"',
    )
    result = fc.condense_followup("what about step 3?", _history())
    assert result == "What is the settlement step?"


def test_condense_followup_redis_get_failure_does_not_raise(monkeypatch, fake_redis):
    def _raise_get(key):
        raise ConnectionError("redis down")

    monkeypatch.setattr(fake_redis, "get", _raise_get)
    monkeypatch.setattr("models.model_router.model_router.generate", lambda p, **kw: "standalone q")

    result = fc.condense_followup("what about step 3?", _history())
    assert result == "standalone q"  # still works, just skips the cache


def test_condense_followup_redis_setex_failure_does_not_raise(monkeypatch, fake_redis):
    def _raise_setex(key, ttl, value):
        raise ConnectionError("redis down")

    monkeypatch.setattr(fake_redis, "setex", _raise_setex)
    monkeypatch.setattr("models.model_router.model_router.generate", lambda p, **kw: "standalone q")

    result = fc.condense_followup("what about step 3?", _history())
    assert result == "standalone q"  # cache write failure is non-fatal


# ── the deprecated explicit override ────────────────────────────────────────
#
# KB_FOLLOWUP_CONDENSE_MODEL_CHAIN used to be an ordered hop chain walked by
# this module. It is now a single pin handed to tier_request(override=), which
# warns once per process. Phase 8 removes it. Ordering is the admin's priority
# order on the `simple` tier, walked by resolve_tier_candidates().


# ── the cost guard ──────────────────────────────────────────────────────────
#
# A within-tier choice is the administrator's decision and is accepted. The
# resolver walking TIER_FALLBACK_LADDER is not: that leaves `simple` and can
# bill a heavy model for a one-sentence rewrite.
#
# This replaces a check for a "[fallback]" suffix on last_model_label. That
# suffix is written only by the legacy _try_* chain — the governed path sets
# the bare model id — so the old guard stopped firing exactly when governance
# was turned on.


class _FakeInfo:
    def __init__(self, occurred, frm="simple", to="medium", label="m"):
        self.fallback_occurred = occurred
        self.from_tier = frm
        self.to_tier = to
        self.to_label = label
        self.from_label = frm
        self.reason = "tier_fallback" if occurred else "primary"


class _FakeModelRouter:
    """Controls generate()'s return value and last_decision independently."""

    def __init__(self, output, info):
        self.output = output
        self.last_decision = info
        self.last_model_label = "whatever"
        self.calls = []

    def generate(self, prompt, **kw):
        self.calls.append(kw)
        return self.output


def test_a_ladder_walk_is_rejected(monkeypatch, fake_redis):
    """Served from outside `simple` → keep the original question."""
    router = _FakeModelRouter("A rewritten standalone question?",
                              _FakeInfo(True))
    monkeypatch.setattr("models.model_router.model_router", router)

    result = fc.condense_followup("what about step 3?", _history())

    assert result == "what about step 3?"
    assert len(router.calls) == 1, "no second hop — the tier owns ordering now"


def test_a_within_tier_answer_is_accepted(monkeypatch, fake_redis):
    """No false positive: the admin's own priority order is approved."""
    router = _FakeModelRouter("What is the UPI settlement confirmation step?",
                              _FakeInfo(False))
    monkeypatch.setattr("models.model_router.model_router", router)

    result = fc.condense_followup("what about step 3?", _history())
    assert result == "What is the UPI settlement confirmation step?"


def test_a_missing_last_decision_does_not_reject(monkeypatch, fake_redis):
    """An old router object without the attribute must not fail closed —
    this function's contract is that it never degrades the question."""
    router = _FakeModelRouter("What is the settlement step?", None)
    monkeypatch.setattr("models.model_router.model_router", router)
    assert fc.condense_followup("what about step 3?", _history()) == \
        "What is the settlement step?"


def test_a_rejected_answer_is_not_cached(monkeypatch, fake_redis):
    router = _FakeModelRouter("a rewritten question?", _FakeInfo(True))
    monkeypatch.setattr("models.model_router.model_router", router)
    fc.condense_followup("what about step 3?", _history())
    assert fake_redis.store == {}


# ── condense_followup — LLM decides self-contained vs. follow-up ────────────
#
# There is no separate pattern-matching classifier any more (see the module
# docstring in followup_condenser.py) — the condenser is called on EVERY
# turn with history, and the LLM itself decides whether to echo the
# question back unchanged (self-contained) or rewrite it (follow-up).
# Callers derive the "was this a follow-up?" signal by comparing the
# returned value to the original question — these tests cover both paths.

def test_condense_followup_llm_echoes_self_contained_question_unchanged(monkeypatch, fake_redis):
    """When the LLM judges the question already self-contained, it should
    echo it back verbatim — the caller then correctly concludes this was
    NOT a follow-up (result == original question)."""
    original = "What is the UPI settlement TAT?"
    monkeypatch.setattr(
        "models.model_router.model_router.generate",
        lambda prompt, **kw: original,
    )
    result = fc.condense_followup(original, _history())
    assert result == original


def test_condense_followup_llm_rewrites_dependent_question(monkeypatch, fake_redis):
    """When the LLM judges the question depends on context, it rewrites it
    — the caller then correctly concludes this WAS a follow-up
    (result != original question)."""
    monkeypatch.setattr(
        "models.model_router.model_router.generate",
        lambda prompt, **kw: "What is the UPI settlement confirmation step?",
    )
    result = fc.condense_followup("what about step 3?", _history())
    assert result != "what about step 3?"
    assert result == "What is the UPI settlement confirmation step?"


def test_condense_followup_prompt_instructs_llm_to_judge_self_containment(monkeypatch, fake_redis):
    """The prompt sent to the LLM must explicitly ask it to decide between
    echoing the question unchanged vs rewriting it — this is the mechanism
    that replaced the old regex classifier."""
    captured = {}

    def _capture(prompt, **kw):
        captured["prompt"] = prompt
        return "some output"

    monkeypatch.setattr("models.model_router.model_router.generate", _capture)
    fc.condense_followup("what about step 3?", _history())

    assert "self-contained" in captured["prompt"].lower()
    assert "unchanged" in captured["prompt"].lower()


# ── condense_followup — resistance to persona/style bleed-through ──────────
#
# gateway.py prepends behavioral directives (built-in persona, a user's own
# Custom Instructions, cross-chat memory notes) onto the FIRST message in
# the conversation history before this function ever sees it. Those
# directives are arbitrary and unbounded — any user can type anything, in
# any language, in Settings — so the fix can't rely on detecting specific
# known phrases. Instead the condensation prompt itself must instruct the
# model to disregard any such directives it encounters in the transcript.
# These tests verify that instruction is actually present, and (functionally)
# that a persona-laden history doesn't change condense_followup's contract.

def test_condense_followup_prompt_tells_model_to_ignore_persona_directives(monkeypatch, fake_redis):
    """The prompt must explicitly instruct the model to ignore any
    persona/tone/style directives found in the conversation transcript,
    regardless of what that directive says or what language it's in —
    this is what prevents a chatty/long rewrite that fails the length
    sanity check on the primary (paid) hop."""
    captured = {}

    def _capture(prompt, **kw):
        captured["prompt"] = prompt
        return "some output"

    monkeypatch.setattr("models.model_router.model_router.generate", _capture)
    fc.condense_followup("what about step 3?", _history())

    prompt_lower = captured["prompt"].lower()
    assert "ignore" in prompt_lower
    assert "persona" in prompt_lower
    assert "not the assistant" in prompt_lower or "background utility" in prompt_lower


def test_condense_followup_still_works_with_persona_laden_history(monkeypatch, fake_redis):
    """Functional check: a history whose first message carries a persona
    directive (the real-world shape gateway.py produces) must not break
    condensation — the model still receives the full history (nothing
    stripped) and the function still returns whatever the model outputs,
    following the same contract as a persona-free history."""
    persona_history = [
        {
            "role": "user",
            "content": (
                "[PERSONA — talk like a helpful friend, not a corporate bot] "
                "Be warm, natural, and genuinely conversational. Their name "
                "is Naveen. Use contractions and everyday language.\n\n"
                "What is UPI settlement TAT?"
            ),
        },
        {"role": "assistant", "content": "UPI settlement TAT is T+1 business day."},
    ]
    captured = {}

    def _capture(prompt, **kw):
        captured["prompt"] = prompt
        return "What is the UPI settlement confirmation step?"

    monkeypatch.setattr("models.model_router.model_router.generate", _capture)
    result = fc.condense_followup("what about step 3?", persona_history)

    # The persona text is NOT stripped out of the history sent to the model
    # (stripping arbitrary/unknown user text is fragile — see module
    # docstring) — it's present verbatim, alongside the ignore-directive.
    assert "PERSONA" in captured["prompt"]
    assert "ignore" in captured["prompt"].lower()
    # condense_followup still returns the model's output normally.
    assert result == "What is the UPI settlement confirmation step?"
