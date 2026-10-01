# SPDX-License-Identifier: MIT
"""Phase 8 (D111): the LLM proxy picks no model and prices nothing.

Every request names its model; the backend resolves it from the tier. The
proxy keeps only its egress deny-list (U2).
"""

from __future__ import annotations

import importlib.util
import pathlib
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROXY = ROOT / "services" / "llm_proxy"


def _proxy_policy():
    spec = importlib.util.spec_from_file_location("_proxy_policy", PROXY / "core" / "model_registry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _src(rel: str) -> str:
    return (PROXY / rel).read_text(encoding="utf-8")


# ── the proxy refuses to guess ──────────────────────────────────────────────


def test_the_proxy_policy_holds_only_the_deny_list():
    mod = _proxy_policy()
    public = {n for n in vars(mod) if n.isupper()}
    assert public == {"BLOCKED_MODELS"}
    assert "claude-opus-4-6" in mod.BLOCKED_MODELS


@pytest.mark.parametrize("model", ["", None, "   "])
def test_no_model_is_refused(model):
    with pytest.raises(ValueError, match="no model named"):
        _proxy_policy().require_model(model, "x")


def test_a_blocked_model_is_refused_and_a_named_one_passes():
    mod = _proxy_policy()
    with pytest.raises(ValueError, match="blocked"):
        mod.require_model("gpt-5.2", "x")
    assert mod.require_model(" claude-x ", "x") == "claude-x"


def test_the_text_endpoints_return_400_not_a_default():
    src = _src("main.py")
    for ep in ("/llm/generate {req.provider}", "/llm/claude-tools-stream",
               "/llm/openai-tools-stream", "/llm/gemini-tools-stream", "/llm/chat {req.provider}"):
        assert f'_model_or_400(req.model, "{ep}")' in src or f'_model_or_400(req.model, f"{ep}")' in src, ep
    assert "raise HTTPException(400, str(exc))" in src


@pytest.mark.parametrize("rel", ["gateway_claude.py", "gateway_openai.py", "gateway_gemini.py"])
def test_the_proxy_gateways_require_a_model(rel):
    assert "require_model(" in _src(rel)
    assert "MODEL_COST_PER_1M" not in _src(rel)


def test_veo_runs_the_model_the_backend_sent():
    """It used to ignore req.model and run the proxy host's VEO_MODEL."""
    src = _src("main.py")
    body = src[src.index("def _call_veo():"):src.index("video_bytes, err = await _run_in_pool(loop, _call_veo)")]
    assert "model=req.model," in body


def test_ppt_gemini_leg_passes_its_model():
    """The proxy's generate_imagen took no model=, so this call always raised."""
    assert "def generate_imagen(self, prompt: str, *, model: str = \"\")" in _src("gateway_gemini.py")


def test_vision_names_both_legs():
    src = _src("main.py")
    assert 'require_model(req.model, "gemini vision")' in src
    assert 'model=req.fallback_model or ""' in src


# ── the backend always names the model ─────────────────────────────────────


def test_proxy_default_model_follows_the_direct_gateways(monkeypatch):
    import core.tier_resolver as tr
    from core.tiers import Tier
    seen = []
    monkeypatch.setattr(tr, "family_model", lambda tier, fam: seen.append((tier, fam)) or f"{fam}-m")
    assert tr.proxy_default_model("claude") == "anthropic-m"
    assert tr.proxy_default_model("openai") == "openai-m"
    assert tr.proxy_default_model("gemini") == "gemini-m"
    assert tr.proxy_default_model("local_llm") == ""
    assert seen == [(Tier.COMPLEX, "anthropic"), (Tier.MEDIUM, "openai"), (Tier.MEDIUM, "gemini")]


def test_the_router_proxy_fills_a_missing_model(monkeypatch):
    import models.model_router as mr
    sent = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def iter_lines(self):
            return iter([])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Client:
        def stream(self, method, url, json=None, **kw):
            sent.update(json)
            return _Resp()

        def post(self, url, json=None, **kw):
            sent.update(json)
            return types.SimpleNamespace(raise_for_status=lambda: None,
                                         json=lambda: {"text": "", "in_tok": 0, "out_tok": 0})

    monkeypatch.setattr(mr, "_llm_proxy_url", lambda: "http://proxy", raising=True)
    monkeypatch.setattr(mr, "_get_proxy_client", lambda: _Client(), raising=True)
    monkeypatch.setattr(mr, "proxy_default_model", lambda p: f"default-for-{p}", raising=True)
    try:
        list(mr._ProxyGateway("claude").generate("hi"))
    except Exception:  # noqa: BLE001 — only the payload matters here
        pass
    assert sent.get("model") == "default-for-claude"


def test_backend_image_senders_name_both_legs():
    gw = (ROOT / "gateway_gemini.py").read_text(encoding="utf-8")
    assert '"fallback_model": _tier_openai_image_input(),' in gw
    router = (ROOT / "models" / "model_router.py").read_text(encoding="utf-8")
    assert '"fallback_model": _family_model(Tier.IMAGE_INPUT, "openai"),' in router
    doc = (ROOT / "workers" / "doc_worker.py").read_text(encoding="utf-8")
    assert 'model=family_model(Tier.IMAGE_OUTPUT, "openai"))' in doc
