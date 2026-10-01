# SPDX-License-Identifier: MIT
"""§N.1 step 8e — document images ask the image-output tier.

This is the one part of step 8 that fixes a live, reproducible defect rather
than moving a decision to a better place. Before this change, on this very
deployment:

    workers.doc_worker._resolve_image_provider()  ->  "disabled"

with GEMINI_API_KEY set, `gemini-3.1-flash-image` assigned to the image-output
tier, and chat image generation working. Three separate functions each guessed
the provider by sniffing environment variables, and the one that ran checked
GOOGLE_API_KEY — a name this platform does not use (gateway_gemini.py:81 reads
GEMINI_API_KEY). Document and PPTX images were therefore off, silently: a
missing image is a geometric slide, never an error.

The regression that matters most is test_the_key_name_mismatch_cannot_return:
nothing else in the suite would notice if someone reintroduced an env-key
sniff, because the symptom is an absence.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from core.tiers import Tier
import workers.doc_worker as dw

ROOT = pathlib.Path(__file__).resolve().parents[2]


class _RM:
    def __init__(self, model_id, family):
        self.model_id, self.family = model_id, family


@pytest.fixture(autouse=True)
def _auto_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin PPT_IMAGE_PROVIDER to its default so a developer's .env cannot make
    these pass by forcing a provider."""
    monkeypatch.setattr(dw, "_PPT_IMG_PROVIDER", "auto")


def _resolves_to(monkeypatch, rm):
    import models.model_router as mr
    monkeypatch.setattr(mr, "resolve_media_model", lambda tier, **kw: rm)


def _raises(monkeypatch, exc):
    import models.model_router as mr

    def _boom(tier, **kw):
        raise exc
    monkeypatch.setattr(mr, "resolve_media_model", _boom)


# ── The tier decides ──────────────────────────────────────────────────────


def test_a_gemini_assignment_enables_images_and_names_the_model(monkeypatch):
    _resolves_to(monkeypatch, _RM("gemini-3.1-flash-image", "gemini"))
    assert dw._image_target() == ("gemini", "gemini-3.1-flash-image")


def test_an_openai_assignment_maps_to_the_dalle_leg(monkeypatch):
    _resolves_to(monkeypatch, _RM("dall-e-3", "openai"))
    assert dw._image_target() == ("dalle", "dall-e-3")


def test_the_model_id_reaches_the_sandbox_vocabulary_too(monkeypatch):
    """_sandbox_image_provider used to return an unused prompt suffix as its
    second element; it now returns the SKU, so the doc sandbox can name the
    model the tier resolved instead of running whatever its own env says."""
    _resolves_to(monkeypatch, _RM("gemini-3.1-flash-image", "gemini"))
    assert dw._sandbox_image_provider() == ("gemini", "gemini-3.1-flash-image")
    _resolves_to(monkeypatch, _RM("dall-e-3", "openai"))
    assert dw._sandbox_image_provider() == ("openai", "dall-e-3")


# ── Two vocabularies that must not be confused ────────────────────────────


def test_a_registry_family_is_never_sent_as_a_provider(monkeypatch):
    """The resolver deals in llm_providers.family (anthropic|gemini|ollama);
    the proxy and doc sandbox deal in gemini|dalle|openai. They overlap on one
    word. Posting "anthropic" to /llm/imagen is a hard 400 — this is the
    mistake step 6's first draft made with the proxy's provider names, so it
    gets a test rather than care."""
    _resolves_to(monkeypatch, _RM("claude-sonnet-5-5", "anthropic"))
    assert dw._image_target() == ("disabled", "")
    _resolves_to(monkeypatch, _RM("llama3.2:1b", "ollama"))
    assert dw._image_target() == ("disabled", "")


def test_the_family_map_only_contains_families_the_image_paths_accept():
    assert set(dw._FAMILY_TO_IMAGE_PROVIDER.values()) <= {"gemini", "dalle"}


# ── Fail-soft, always ─────────────────────────────────────────────────────


def test_an_unassigned_tier_disables_images_rather_than_raising(monkeypatch):
    """A document must never fail because nobody filled in a Tiers row. The
    established fail-soft is "no image, geometric slide", and it stays."""
    from core.tier_resolver import NoEligibleModel
    _raises(monkeypatch, NoEligibleModel(Tier.IMAGE_OUTPUT, None, {}))
    assert dw._image_target() == ("disabled", "")
    assert dw._resolve_image_provider() == "disabled"


def test_a_database_outage_disables_images_rather_than_raising(monkeypatch):
    _raises(monkeypatch, RuntimeError("could not connect to server"))
    assert dw._image_target() == ("disabled", "")


# ── The operator's switches still work ────────────────────────────────────


def test_ppt_image_provider_disabled_is_still_the_explicit_off_switch(monkeypatch):
    """Turning images off must not require un-assigning the tier — chat
    image generation shares that assignment."""
    monkeypatch.setattr(dw, "_PPT_IMG_PROVIDER", "disabled")
    _resolves_to(monkeypatch, _RM("gemini-3.1-flash-image", "gemini"))
    assert dw._image_target() == ("disabled", "")


def test_ppt_image_provider_can_still_force_a_provider(monkeypatch):
    monkeypatch.setattr(dw, "_PPT_IMG_PROVIDER", "dalle")
    _resolves_to(monkeypatch, _RM("gemini-3.1-flash-image", "gemini"))
    assert dw._image_target() == ("dalle", "")


# ── The regression that has no other symptom ──────────────────────────────


def test_the_key_name_mismatch_cannot_return():
    """GOOGLE_API_KEY vs GEMINI_API_KEY. The bug produced no error anywhere —
    images just stopped appearing — so the only thing that can catch its
    return is an assertion that the decision does not read API keys at all.

    AST-scoped to the three functions that made the decision, so the module
    stays free to read keys elsewhere."""
    tree = ast.parse((ROOT / "workers" / "doc_worker.py").read_text(encoding="utf-8"))
    targets = {"_image_target", "_resolve_image_provider", "_sandbox_image_provider"}
    found = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name in targets):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "getenv"
                    and sub.args and isinstance(sub.args[0], ast.Constant)
                    and "API_KEY" in str(sub.args[0].value)):
                found.setdefault(node.name, []).append(sub.args[0].value)
    assert not found, (
        f"image provider decided from API keys again: {found}. The presence of "
        f"a key is not the question — the question is which model the "
        f"administrator assigned to the image-output tier.")


def test_the_three_guesses_are_now_one_decision():
    """_resolve_image_provider and _sandbox_image_provider must both delegate.
    Two functions answering the same question independently is how they came
    to disagree (one preferred Gemini, the other OpenAI, and
    sandbox/doc_executor.py defaulted to a third answer)."""
    src = (ROOT / "workers" / "doc_worker.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for name in ("_resolve_image_provider", "_sandbox_image_provider"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        calls = {c.func.id for c in ast.walk(fn)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert "_image_target" in calls, f"{name}() no longer delegates"


# ── The wire contract, end to end ─────────────────────────────────────────


def test_the_ppt_image_request_accepts_a_model():
    """Without it the tier governs the FAMILY and not the SKU on the
    production path — exactly the defect Phase 6.5 item 2 closed for
    /llm/imagen, which this endpoint is the sibling of."""
    src = (ROOT / "services" / "llm_proxy" / "main.py").read_text(encoding="utf-8")
    cls = next(n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.ClassDef) and n.name == "PptImageRequest")
    field = next((t for t in cls.body
                  if isinstance(t, ast.AnnAssign)
                  and getattr(t.target, "id", "") == "model"), None)
    assert field is not None, "PptImageRequest carries no `model` field"
    assert isinstance(field.value, ast.Constant) and field.value.value is None, (
        "PptImageRequest.model must stay OPTIONAL — a caller with no tier "
        "assignment sends none, and that has to keep meaning 'the "
        "deployment's default'")


def test_the_model_is_not_sent_to_the_dalle_fallback_leg():
    """Same distinction /llm/imagen makes: the DALL-E leg of `auto` runs
    `fallback_model`, never the Gemini id."""
    src = (ROOT / "services" / "llm_proxy" / "main.py").read_text(encoding="utf-8")
    assert '_want_ppt_model = (req.model or "").strip()' in src
    assert '_dalle_model = _want_ppt_model if provider == "dalle" else (req.fallback_model or "").strip()' in src
    assert "generate_imagen(req.prompt, model=_want_ppt_model)" in src
    assert "generate_image_dalle(req.prompt, model=_dalle_model)" in src


def test_the_doc_sandbox_omits_an_absent_model():
    src = (ROOT / "sandbox" / "doc_executor.py").read_text(encoding="utf-8")
    assert '**({"model": model.strip()} if (model or "").strip() else {})' in src
