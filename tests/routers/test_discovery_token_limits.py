# SPDX-License-Identifier: MIT
"""D97/D98 — a model's output limit is recorded when it is registered.

Only the output limit. Anthropic also reports max_input_tokens (1M for 7 of
the 8 live Claude ids), and recording it would move CLI compaction from 200k.

Anthropic's list-models response carries `max_tokens` and `max_input_tokens`;
discovery read neither, and fetched only the first page of 20. Gemini's
`outputTokenLimit` was stored as `reserved_output`. OpenAI's list carries no
limits at all, so an OpenAI-shaped model is probed: one request with an
impossible max_tokens, whose 400 states the real limit.
"""

from __future__ import annotations

import types

import pytest

from routers import llm_provider_admin_router as r


class _Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body or {}, text

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _provider(family, base_url=None):
    return types.SimpleNamespace(id="p1", family=family, base_url=base_url, slug="p", credential_id=None)


@pytest.fixture
def calls(monkeypatch):
    log = {"get": [], "post": []}
    monkeypatch.setattr(r, "_provider_api_key", lambda _p: "k", raising=True)
    return log


def _route(monkeypatch, calls, get=None, post=None):
    def _get(url, **kw):
        calls["get"].append((url, kw))
        return get(url, **kw)

    def _post(url, **kw):
        calls["post"].append((url, kw))
        return post(url, **kw)

    monkeypatch.setattr(r.httpx, "get", _get, raising=True)
    monkeypatch.setattr(r.httpx, "post", _post, raising=True)


# ── discovery, per family ───────────────────────────────────────────────────


def test_anthropic_limits_are_recorded_from_every_page(monkeypatch, calls):
    body = {"data": [{"id": "claude-x", "display_name": "X", "max_tokens": 64000, "max_input_tokens": 200000},
                     {"id": "claude-y", "max_tokens": None}]}
    _route(monkeypatch, calls, get=lambda *_a, **_k: _Resp(body=body))
    out = {d["model_id"]: d["capabilities"] for d in r._discover_models(_provider("anthropic"))}
    # max_input_tokens is deliberately not a window source: 1M would move CLI compaction.
    assert out["claude-x"] == {"max_output_tokens": 64000}
    assert out["claude-y"] == {}
    assert calls["get"][0][1]["params"] == {"limit": 1000}


def test_gemini_output_limit_is_not_stored_as_a_reserve(monkeypatch, calls):
    body = {"models": [{"name": "models/gem-x", "inputTokenLimit": 1000000, "outputTokenLimit": 65536}]}
    _route(monkeypatch, calls, get=lambda *_a, **_k: _Resp(body=body))
    (d,) = r._discover_models(_provider("gemini"))
    assert d["capabilities"] == {"context_window": 1000000, "max_output_tokens": 65536}


def test_vllm_model_length_bounds_output(monkeypatch, calls):
    body = {"data": [{"id": "served-x", "max_model_len": 32768}]}
    _route(monkeypatch, calls, get=lambda *_a, **_k: _Resp(body=body))
    (d,) = r._discover_models(_provider("openai_compatible", "http://vllm/v1"))
    assert d["capabilities"] == {"max_output_tokens": 32768}


def test_ollama_context_length_comes_from_show(monkeypatch, calls):
    tags = {"models": [{"name": "llama-x:1b"}]}
    show = {"model_info": {"llama.context_length": 131072, "llama.block_count": 16}}
    _route(monkeypatch, calls, get=lambda *_a, **_k: _Resp(body=tags), post=lambda *_a, **_k: _Resp(body=show))
    (d,) = r._discover_models(_provider("ollama", "http://ollama:11434"))
    assert d["capabilities"] == {"billing_tier": "free", "max_output_tokens": 131072}
    assert calls["post"][0][1]["json"] == {"model": "llama-x:1b"}


def test_ollama_show_failure_keeps_the_model(monkeypatch, calls):
    tags = {"models": [{"name": "llama-x:1b"}]}
    _route(monkeypatch, calls, get=lambda *_a, **_k: _Resp(body=tags), post=lambda *_a, **_k: _Resp(status=500))
    (d,) = r._discover_models(_provider("ollama", "http://ollama:11434"))
    assert d["capabilities"] == {"billing_tier": "free"}


# ── the probe ───────────────────────────────────────────────────────────────

_OPENAI_400 = ("max_completion_tokens is too large: 100000000. This model supports at most "
               "128000 completion tokens, whereas you provided 100000000.")
_VLLM_400 = "This model's maximum context length is 32768 tokens. However, you requested 100000010 tokens."


@pytest.mark.parametrize("family,text,param,expected", [
    ("openai", _OPENAI_400, "max_completion_tokens", {"max_output_tokens": 128000}),
    ("openai_compatible", _VLLM_400, "max_tokens", {"max_output_tokens": 32768}),
])
def test_the_probe_reads_the_limit_from_the_rejection(monkeypatch, calls, family, text, param, expected):
    _route(monkeypatch, calls, post=lambda *_a, **_k: _Resp(status=400, text=text))
    assert r._probe_max_output(_provider(family, "http://x/v1"), "m") == expected
    sent = calls["post"][0][1]["json"]
    assert sent[param] == r._PROBE_TOKENS and sent["model"] == "m"


@pytest.mark.parametrize("resp", [
    _Resp(status=200, text="ok"),
    _Resp(status=400, text="something else entirely"),
    _Resp(status=401, text=_OPENAI_400),
])
def test_an_unreadable_probe_records_nothing(monkeypatch, calls, resp):
    _route(monkeypatch, calls, post=lambda *_a, **_k: resp)
    assert r._probe_max_output(_provider("openai"), "m") == {}


def test_a_failed_probe_records_nothing(monkeypatch, calls):
    def boom(*_a, **_k):
        raise TimeoutError("slow")
    _route(monkeypatch, calls, post=boom)
    assert r._probe_max_output(_provider("openai"), "m") == {}


def test_only_models_without_a_limit_are_probed(monkeypatch, calls):
    _route(monkeypatch, calls, post=lambda *_a, **_k: _Resp(status=400, text=_OPENAI_400))
    discovered = [
        {"model_id": "known-here", "capabilities": {"max_output_tokens": 4096}},
        {"model_id": "known-stored", "capabilities": {}},
        {"model_id": "unknown", "capabilities": {}},
    ]
    stored = {"known-stored": types.SimpleNamespace(capabilities={"max_output_tokens": 8192})}
    r._fill_probed_limits(_provider("openai"), discovered, stored)
    assert [c[1]["json"]["model"] for c in calls["post"]] == ["unknown"]
    assert discovered[2]["capabilities"]["max_output_tokens"] == 128000
    assert discovered[0]["capabilities"]["max_output_tokens"] == 4096


@pytest.mark.parametrize("family", ["anthropic", "gemini", "ollama"])
def test_families_with_a_list_api_are_never_probed(monkeypatch, calls, family):
    _route(monkeypatch, calls, post=lambda *_a, **_k: _Resp(status=400, text=_OPENAI_400))
    r._fill_probed_limits(_provider(family), [{"model_id": "m", "capabilities": {}}], {})
    assert calls["post"] == []


# ── registering one model by hand ───────────────────────────────────────────


class _Q:
    def __init__(self, first):
        self._first = first

    def filter_by(self, **_kw):
        return self

    def first(self):
        return self._first


class _DB:
    def __init__(self, provider):
        self.provider, self.added = provider, []

    def query(self, model):
        return _Q(self.provider if model is r.LLMProvider else None)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def refresh(self, _obj):
        pass


def _create(monkeypatch, provider, caps=None):
    monkeypatch.setattr(r, "ensure_default_model", lambda _db: None, raising=True)
    monkeypatch.setattr(r, "invalidate_cache", lambda: None, raising=True)
    db = _DB(provider)
    body = r.ModelCreate(model_id="m-1", display_name="M", capabilities=caps)
    return r.create_model("p1", body, {"email": "a@b"}, db)["model"]["capabilities"]


def test_a_hand_added_anthropic_model_gets_its_limits(monkeypatch, calls):
    _route(monkeypatch, calls, get=lambda *_a, **_k: _Resp(body={"id": "m-1", "max_tokens": 32000,
                                                                "max_input_tokens": 200000}))
    caps = _create(monkeypatch, _provider("anthropic"))
    assert caps["max_output_tokens"] == 32000 and "context_window" not in caps
    assert calls["get"][0][0].endswith("/v1/models/m-1")


def test_a_hand_added_openai_model_is_probed(monkeypatch, calls):
    _route(monkeypatch, calls, post=lambda *_a, **_k: _Resp(status=400, text=_OPENAI_400))
    assert _create(monkeypatch, _provider("openai"))["max_output_tokens"] == 128000


def test_an_admin_supplied_limit_is_not_looked_up(monkeypatch, calls):
    _route(monkeypatch, calls, get=lambda *_a, **_k: pytest.fail("looked up"),
           post=lambda *_a, **_k: pytest.fail("probed"))
    assert _create(monkeypatch, _provider("openai"), {"max_output_tokens": 1000})["max_output_tokens"] == 1000
