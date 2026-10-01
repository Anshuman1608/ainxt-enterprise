# SPDX-License-Identifier: MIT
"""Phase 6.5 item 2 — the image-output tier names the model, not just the family.

The defect this closes was the only one in the Phase 6.5 block that produced a
WRONG answer rather than a missing one, and it was silent:

    An administrator assigns `gemini-3-pro-image` to the `image-output` tier.
    chat_router resolves the tier, reads `ResolvedModel.family` → "gemini",
    and posts `{"provider": "gemini", ...}` to the proxy's /llm/imagen.
    The proxy has no model field to read, so it runs GEMINI_IMAGE_MODEL.
    The request succeeds. An image comes back. The Tiers screen shows the
    assignment. Nothing anywhere says a different model ran.

So this is a contract test across three files: chat_router must send the
resolved SKU, gateway_gemini must forward it, and the proxy must honour it —
and a break in ANY of the three reproduces the original bug in full, with the
same absence of symptoms. Asserted against the real sources rather than by
standing up the proxy, because two of the three are not importable under
pytest (chat_router pulls in the gateway's module-level filesystem setup).

The subtle one is test_a_model_id_is_never_sent_to_the_fallback_provider: the
proxy falls back gemini→openai, and "which family" and "which SKU within that
family" are separate facts. Handing a Gemini id to the OpenAI Images API would
turn a working fallback into a hard 400 — a fix that broke the thing it was
meant to leave alone.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

PROXY = ROOT / "services" / "llm_proxy" / "main.py"
GATEWAY = ROOT / "gateway_gemini.py"
CHAT = ROOT / "routers" / "chat_router.py"


def _src(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _func(path: pathlib.Path, name: str):
    for node in ast.walk(ast.parse(_src(path))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in {path.name} — did it move?")


# ── The wire contract ─────────────────────────────────────────────────────


def test_imagen_request_accepts_a_model():
    fields = {
        t.target.id
        for node in ast.walk(ast.parse(_src(PROXY)))
        if isinstance(node, ast.ClassDef) and node.name == "ImagenRequest"
        for t in node.body
        if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)
    }
    assert "model" in fields, (
        f"ImagenRequest carries no `model` field — got {sorted(fields)}. "
        f"Without it the image-output tier can only govern the provider family.")


def test_the_model_field_is_optional():
    """It must STAY optional. sandbox/doc_executor.py posts to /llm/imagen for
    document illustrations and is not tier-migrated (that is §N.1 step 8), so
    an absent model has to keep meaning "the deployment's configured default".
    Making it required would break the document pipeline outright."""
    cls = next(
        node for node in ast.walk(ast.parse(_src(PROXY)))
        if isinstance(node, ast.ClassDef) and node.name == "ImagenRequest"
    )
    field = next(
        t for t in cls.body
        if isinstance(t, ast.AnnAssign) and getattr(t.target, "id", "") == "model"
    )
    assert field.value is not None, "ImagenRequest.model has no default"
    assert isinstance(field.value, ast.Constant) and field.value.value is None


def test_the_document_pipeline_still_sends_no_model():
    """Stated as a test because it is the reason the field is optional: if this
    starts failing, someone migrated doc_executor and the optionality above can
    be revisited deliberately rather than by accident."""
    doc = _src(ROOT / "sandbox" / "doc_executor.py")
    assert "/llm/imagen" in doc
    assert '"model"' not in doc.split("/llm/imagen")[1][:600]


# ── Each of the three hops forwards it ────────────────────────────────────


def test_the_proxy_honours_the_requested_gemini_model():
    src = _src(PROXY)
    assert 'want_model = (req.model or "").strip()' in src
    assert 'want_model if (want_model and req.provider == "gemini") else _GEMINI_DEFAULT' in src


def test_the_proxy_honours_the_requested_openai_model():
    src = _src(PROXY)
    assert 'if want_model and req.provider == "openai":' in src


def test_the_gateway_forwards_the_model_to_the_proxy():
    gen = _func(GATEWAY, "generate_imagen")
    params = {a.arg for a in gen.args.args} | {a.arg for a in gen.args.kwonlyargs}
    assert "model" in params, (
        "gateway_gemini.generate_imagen() takes no model= — chat_router has "
        "nowhere to put the SKU the tier resolved")
    assert '**({"model": model.strip()} if (model or "").strip() else {})' in _src(GATEWAY)


def test_the_direct_dev_path_honours_it_too():
    """The no-proxy branch is what local development runs. If only the proxy
    branch honoured the model, dev and production would pick different models
    for the same call — and the bug would be un-reproducible locally."""
    assert '_GEMINI_MULTIMODAL = (model or "").strip() or _tier_gemini_model("image-output")' in _src(GATEWAY)


def test_chat_router_sends_the_tier_resolved_model():
    """The first hop, and the one that makes the tier authoritative."""
    src = _src(CHAT)
    assert "model=_img_tier_model," in src, (
        "routers/chat_router.py resolves image-output into _img_tier_model but "
        "no longer passes it to generate_imagen — the tier is back to governing "
        "only the provider family")
    # It must still be the RESOLVED model, not the env constant.
    assert "_img_tier_model = _img_rm.model_id" in src


# ── The distinction that is easy to get wrong ─────────────────────────────


def test_a_model_id_is_never_sent_to_the_fallback_provider():
    """Both provider branches gate on `req.provider` matching their own family,
    so on the fallback leg the requested SKU is ignored and the family default
    runs. Without the gate, a gemini-primary request that fell back to OpenAI
    would call the OpenAI Images API with a Gemini model id and hard-fail —
    breaking a fallback that works today."""
    src = _src(PROXY)
    assert src.count('req.provider == "gemini"') >= 1
    assert src.count('req.provider == "openai"') >= 1
    # Neither branch may use want_model unconditionally.
    assert "model=want_model,\n" in src        # the guarded openai call
    assert 'model=want_model or' not in src    # an unguarded shortcut


def test_the_proxy_logs_which_model_it_will_run():
    """The diagnosis path. The original bug was invisible precisely because no
    log line named the model before the call — the only model in the logs was
    whatever came back, which was always the env default."""
    assert "model={want_model or '(provider default)'}" in _src(PROXY)


def test_the_stale_honest_limit_note_is_gone():
    """chat_router carried a paragraph explaining that the tier could not
    control the SKU. Leaving it would tell the next reader the opposite of
    what the code now does."""
    src = _src(CHAT)
    assert "HONEST LIMIT" not in src
    assert "the concrete SKU within the family is still chosen" not in src


# ── The pricing consequence ───────────────────────────────────────────────


def test_an_unpriced_model_is_reported_rather_than_billed_silently():
    """_image_cost falls back to the gemini image rate for a model it has no
    entry for. That was harmless while only one model could ever run; now that
    the tier can name any SKU in the family, a real model can bill at another
    model's price — so it has to say so."""
    src = _src(PROXY)
    assert "_IMAGE_RATE_WARNED" in src
    assert "has no entry in MODEL_COST_PER_1M" in src


def test_the_openai_fallback_leg_records_its_model():
    """It used to `return _to_bytes(r)` without touching _meta, so an
    OPENAI_IMAGE_MODEL image was reported as "" in X-Imagen-Model and priced by
    the carry-over rate. Found while adding the model field."""
    assert '_meta["model"] = _OPENAI_IMG_MODEL' in _src(PROXY)


@pytest.mark.parametrize("path", [PROXY, GATEWAY, CHAT])
def test_the_files_still_parse(path):
    ast.parse(_src(path))
