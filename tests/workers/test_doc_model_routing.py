# SPDX-License-Identifier: MIT
"""§N.1 step 8 — the document pipeline asks for a tier, and the user still wins.

Real unit tests rather than the AST assertions steps 5-7 had to settle for.
That matters here because the load-bearing claim of this step is about VALUES
flowing through ~15 forwarding sites, and a source scan cannot see a value.

Making them possible needed one unplanned change: workers/doc_worker.py and
three sibling modules did a module-scope os.makedirs on the persistent doc
volume with no try/except, so importing any of them outside a container raised
PermissionError. core/config.py:403 already guards the same directory for the
same reason; these four now do too.

The claim, in one sentence: a user who picked a model in the chat dropdown gets
that model, and a user who did not gets whichever model an administrator
assigned to the `complex` tier. §G lists the first half as a hard requirement —
`user_model_hint` rides a Kafka payload from routers/doc_download_router.py all
the way here and must survive byte-for-byte.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from core.tiers import Tier
from core.tiers import LEGACY_INBOUND_ALIASES
import workers.doc_worker as dw

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test states its own env. A developer whose .env pins
    DOC_MODEL_PROVIDER would otherwise see these pass for the wrong reason."""
    monkeypatch.delenv("DOC_MODEL_PROVIDER", raising=False)


# ── 1. The user's pick is untouchable (§G) ────────────────────────────────


@pytest.mark.parametrize("pick", [
    "openai-deep", "claude-opus48", "claude-opus46", "complex", "haiku", "gemini",
])
def test_a_user_pick_is_passed_through_verbatim(pick):
    assert dw._resolve_doc_route(pick) == {"model_hint": pick}


def test_a_local_pick_normalises_to_the_local_hint():
    """"local:<id>" is the hint "local"; the specific model travels separately.
    Unchanged behaviour, asserted because the normalisation is easy to drop
    while rewriting the function around it."""
    assert dw._resolve_doc_route("local:Kimi-k2.5") == {"model_hint": "local"}


def test_a_user_pick_beats_the_env_pin(monkeypatch):
    monkeypatch.setenv("DOC_MODEL_PROVIDER", "medium")
    assert dw._resolve_doc_route("openai-deep") == {"model_hint": "openai-deep"}


@pytest.mark.parametrize("nothing", [None, "", "  ", "auto", "AUTO"])
def test_no_pick_reaches_the_tier(nothing):
    route = dw._resolve_doc_route(nothing)
    assert route == {"tier": Tier.COMPLEX, "legacy_hint": "complex"}


# ── 2. D28 — the env var splits on the KIND of value ──────────────────────


@pytest.mark.parametrize("name", ["mini", "simple", "medium", "complex"])
def test_a_tier_name_in_the_env_is_governed(monkeypatch, name):
    """An operator writing `medium` is naming a capability, which is what the
    Tiers screen is for — so it resolves through the assignments rather than
    bypassing them."""
    monkeypatch.setenv("DOC_MODEL_PROVIDER", name)
    assert dw._resolve_doc_route(None) == {"tier": Tier(name), "legacy_hint": name}


def test_a_governed_env_name_keeps_its_own_legacy_hint(monkeypatch):
    """Not the module default. With governance OFF, DOC_MODEL_PROVIDER=medium
    has always meant model_hint="medium"; carrying "complex" as the legacy hint
    would silently upgrade every flag-off deployment that set it."""
    monkeypatch.setenv("DOC_MODEL_PROVIDER", "medium")
    assert dw._resolve_doc_route(None)["legacy_hint"] == "medium"


@pytest.mark.parametrize("pin", ["my-inhouse-model-v2", "openai-deep", "qwen-3.6-35B"])
def test_a_concrete_id_in_the_env_still_bypasses_governance(monkeypatch, pin):
    """D22's precedence, kept. An operator who names a model has made a
    decision and governance must not second-guess it — this is the one
    documented way a harness with no Anthropic provider pins the pipeline."""
    monkeypatch.setenv("DOC_MODEL_PROVIDER", pin)
    assert dw._resolve_doc_route(None) == {"model_hint": pin}


def test_a_modality_tier_name_is_not_treated_as_governed(monkeypatch):
    """`image-output` is not a text generator. Naming it here is operator
    error, and it must NOT resolve an image model for a prose call — it takes
    the honoured-and-warned path where the router reports an unknown model."""
    monkeypatch.setenv("DOC_MODEL_PROVIDER", "image-output")
    assert dw._resolve_doc_route(None) == {"model_hint": "image-output"}


# ── 3. D15 — every legacy hint this module emits is real ──────────────────


def test_every_legacy_hint_the_pipeline_emits_is_a_real_hint_map_key():
    """The assertion that would have caught step 6's first wrong assumption:
    ModelRouter._coerce_tier RAISES on a legacy_hint outside core.tiers.LEGACY_INBOUND_ALIASES, so a
    plausible-looking hint is a crash with governance off, not a fallback."""
    emitted = {"complex", "haiku", "mini", "simple", "medium"}
    missing = sorted(h for h in emitted if h not in LEGACY_INBOUND_ALIASES)
    assert not missing, f"not known aliases: {missing}"


# ── 4. The threading (8b) — what a source scan cannot check ───────────────


class _SpyRouter:
    """Captures the kwargs the doc pipeline actually hands the router."""

    def __init__(self):
        self.calls: list[dict] = []

    def generate(self, prompt, **kwargs):
        kwargs.pop("return_meta", None)
        self.calls.append(kwargs)
        return {"text": '{"title":"T","sections":[]}', "meta": {"model": "spy"}}


def _spy(monkeypatch) -> _SpyRouter:
    import models.model_router as mr
    spy = _SpyRouter()
    monkeypatch.setattr(mr, "model_router", spy)
    return spy


def test_llm_call_forwards_the_route_it_was_given(monkeypatch):
    spy = _spy(monkeypatch)
    dw._llm_call("p", job_id="j", route={"model_hint": "openai-deep"})
    assert spy.calls == [{"model_hint": "openai-deep"}]


def test_llm_call_with_no_route_resolves_the_default_tier(monkeypatch):
    """`None` must mean "nobody pinned anything", never "no routing". A
    forwarding site that drops the route degrades to the documented default
    rather than to whatever the router guesses from the prompt."""
    spy = _spy(monkeypatch)
    dw._llm_call("p", job_id="j")
    assert spy.calls == [{"tier": Tier.COMPLEX, "legacy_hint": "complex"}]


def test_no_model_hint_key_survives_on_the_unpinned_path(monkeypatch):
    """Belt and braces for the router's mutual-exclusion check: passing both
    tier= and model_hint= raises, so the two must never be merged."""
    spy = _spy(monkeypatch)
    dw._llm_call("p", route=dw._resolve_doc_route(None))
    assert "model_hint" not in spy.calls[0]


# ── 5. The deprecated string form still works ─────────────────────────────


def test_the_hint_helper_reports_the_tier_when_nothing_is_pinned():
    """_resolve_doc_model_hint feeds log lines and doc_worker_agent. It must
    print something a reader can act on, not an empty string."""
    assert dw._resolve_doc_model_hint(None) == "complex"
    assert dw._resolve_doc_model_hint("openai-deep") == "openai-deep"


# ── 6. Titling (8d) ───────────────────────────────────────────────────────


def test_titling_asks_for_simple_not_a_vendor_sku():
    assert dw._title_route() == {"tier": Tier.SIMPLE, "legacy_hint": "haiku"}


def test_doc_intent_model_no_longer_silently_steers_titling(monkeypatch):
    """One env var used to drive two unrelated consumers: titling here and
    document-intent classification in models/doc_intent.py, with different
    defaults. DOC_TITLE_MODEL is titling's own knob, and it wins."""
    monkeypatch.setenv("DOC_INTENT_MODEL", "intent-model")
    monkeypatch.setenv("DOC_TITLE_MODEL", "title-model")
    import importlib
    importlib.reload(dw)
    try:
        assert dw._title_route() == {"model_hint": "title-model"}
    finally:
        monkeypatch.delenv("DOC_TITLE_MODEL", raising=False)
        monkeypatch.delenv("DOC_INTENT_MODEL", raising=False)
        importlib.reload(dw)


def test_doc_intent_model_is_still_honoured_as_a_deprecated_fallback(monkeypatch):
    """No deployment changes behaviour on upgrade just because the variable
    was renamed."""
    monkeypatch.setenv("DOC_INTENT_MODEL", "legacy-title-model")
    import importlib
    importlib.reload(dw)
    try:
        assert dw._title_route() == {"model_hint": "legacy-title-model"}
    finally:
        monkeypatch.delenv("DOC_INTENT_MODEL", raising=False)
        importlib.reload(dw)


# ── 7. No vendor pin survives anywhere in the pipeline ────────────────────


@pytest.mark.parametrize("rel", [
    "workers/doc_worker.py",
    "agents/doc_generator_agent.py",
    "services/doc_reviser.py",
    "workers/doc_worker_agent.py",
])
def test_no_module_pins_a_provider_in_a_routing_payload(rel):
    """AST, not grep: these modules now QUOTE the old literals in comments on
    purpose — '"provider": "claude" used to be here' is exactly what the next
    reader needs. A text scan would flag the explanation as the defect."""
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8", errors="replace"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and k.value == "provider"
                    and isinstance(v, ast.Constant)
                    and v.value in ("claude", "anthropic", "openai", "gemini")):
                raise AssertionError(
                    f"{rel}:{node.lineno}: hardcoded provider {v.value!r} in a "
                    f"routing payload — the tier decides the family")
