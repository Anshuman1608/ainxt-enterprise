# SPDX-License-Identifier: MIT
"""No user-facing model catalogue may name a tier.

plan.html §P: *"No tier name appears in any user-facing catalogue response or
picker."* Phase 7 closed the picker half and asserted it in
``ai-ui/src/utils/modelPicker.test.js``. This is the response half, which
until now had been read by hand and by nothing else.

WHY THIS IS NOT A HYPOTHETICAL
------------------------------
Tiers are the *administrator's* vocabulary. A user picks a model or picks
Auto; exposing "complex" in a catalogue invites clients to route on it, which
re-creates the coupling this whole migration removes — and it leaks the
operator's tier layout to anyone with a token.

The leak vector is one line away in four places. ``capabilities`` is seeded
with tier names by ``db/migrate.py``'s ``_AC1_MODEL_ROLE_TAGS``::

    "CLAUDE_PRIMARY_MODEL": ("anthropic", ["complex", "claude", "sonnet"], ...)
    "OPENAI_SIMPLE_MODEL":  ("openai",    ["simple", "mini"],              ...)
    "OPENAI_CODING_MODEL":  ("openai",    ["medium", "coding"],            ...)

and all four builders below already hold ``caps`` in hand to read
``billing_tier``, ``modality`` and ``context_window`` off it. A single
``entry["capabilities"] = caps`` publishes the tier layout of every model.
(Measured on the reference deployment: ``tier_tags`` is ``None`` on all 12
enabled rows, because they predate that seed — the same reason Phase 7 found
``context_window`` unset on 0 of 12. So the drift would not be caught by
eyeballing a live response either.)

WHAT IS ASSERTED, AND WHAT IS NOT
---------------------------------
Exact value equality against the **eight capability tier names only**, over
every string in the payload, recursively. Deliberately not a substring scan
and deliberately not the legacy aliases:

  * ``local`` is a legacy alias, but §P separately requires the ``local:``
    id prefix be *preserved unchanged*, and ``/v1/models`` emits a literal
    ``{"id": "local"}`` entry. Scanning for it would fail on the thing the
    plan protects.
  * ``gemini``, ``claude``, ``opus`` are substrings of real model ids.
  * substring matching on the eight would flag ``gpt-5-mini`` for ``mini``.

Exact-match is safe for all eight: none of them is ever a whole field value
in a correct payload. A tier name buried inside an operator-authored display
label is out of scope — that is text someone typed, not a code defect.

HOW THE FOUR BUILDERS ARE REACHED
---------------------------------
``routers.ide_router`` and ``routers.messages_compat_router`` import on a bare
CI runner, so they are called directly. ``gateway.py`` is loaded from source
by AST, the technique ``tests/test_browser_agent_prompt.py:22`` uses and for
the same reason: ``gateway.py:31`` runs ``core.ckms.load_at_boot()`` at import
time, and importing it also mounts every router and opens its connections.
"""

from __future__ import annotations

import ast
import json
import pathlib
from typing import Any

import anyio
import pytest

from core.tiers import ALL_TIERS

ROOT = pathlib.Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "gateway.py"

#: The eight. Sourced from core.tiers so a ninth tier cannot be added without
#: this test covering it.
TIER_NAMES = frozenset(t.value for t in ALL_TIERS)


# ── the synthetic registry ──────────────────────────────────────────────────
#
# Shaped exactly like a core.llm_provider_registry.get_enabled_models() row.
# `tier_tags` carries three tier-shaped values on purpose: two live tiers and
# one retired alias. If a builder ever passes `capabilities` through, this row
# makes it fail here rather than on a customer's screen.

_CAPS = {
    "tier_tags": ["complex", "solution", "deep"],
    "billing_tier": "paid",
    "modality": "text",
    "context_window": 200_000,
    "privacy_class": "cloud",
}

_CLOUD = {
    "model_id": "vendor-x-1",
    "display_name": "Vendor X 1",
    "family": "openai",
    "provider_name": "VendorCo",
    "capabilities": dict(_CAPS),
}

#: A model with NO tier assignment and no capabilities at all — §H says it
#: must still be selectable, so it must still appear in every catalogue.
_UNASSIGNED = {
    "model_id": "vendor-y-2",
    "display_name": "Vendor Y 2",
    "family": "anthropic",
    "provider_name": "OtherCo",
    "capabilities": None,
}

_LOCAL = {
    "model_id": "llama-test:1b",
    "display_name": "Llama Test",
    "family": "ollama",
    "provider_name": "In-house",
    "capabilities": dict(_CAPS),
}

_ROWS = [_CLOUD, _UNASSIGNED, _LOCAL]


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    """Every builder reads the same three rows, so a difference between two
    catalogues is a difference in the builder and not in the data."""
    monkeypatch.setattr("core.llm_provider_registry.get_enabled_models",
                        lambda **kw: [dict(r) for r in _ROWS], raising=False)
    return _ROWS


# ── the assertion ───────────────────────────────────────────────────────────


def _strings(node: Any, path: str = "$"):
    """Every string in the payload, with the path that reaches it."""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield f"{path}.{k}(key)", str(k)
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")


def assert_no_tier_name(payload: Any, who: str) -> None:
    offenders = [(p, s) for p, s in _strings(payload) if s in TIER_NAMES]
    assert not offenders, (
        f"{who} publishes the administrator's tier vocabulary: "
        + "; ".join(f"{p} == {s!r}" for p, s in offenders)
        + ". Tiers are not a client-facing concept (plan.html §P) — emit the "
          "model id, the billing tier or the modality instead."
    )
    # Named separately because it is the mechanism, and because the message
    # should say so rather than just naming three strings.
    keys = [p for p, _ in _strings(payload) if p.endswith("(key)")
            and p.rsplit(".", 1)[-1] == "tier_tags(key)"]
    assert not keys, (
        f"{who} emits capabilities.tier_tags ({keys}). That field is the "
        f"administrator's tier layout for the model; it is internal."
    )


# ── gateway.py, loaded from source ──────────────────────────────────────────


def _gateway_builder(name: str, injections: dict):
    """Compile one route handler out of gateway.py without importing it."""
    tree = ast.parse(GATEWAY.read_text(encoding="utf-8", errors="replace"))
    found = [n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == 1, (
        f"expected exactly one module-level def {name}() in gateway.py, "
        f"found {len(found)} — the catalogue endpoint has moved"
    )
    fn = found[0]
    fn.decorator_list = []          # the route decorators need a live app
    mod = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    ns: dict = dict(injections)
    exec(compile(mod, str(GATEWAY), "exec"), ns)   # noqa: S102 — our own source
    return ns[name]


class _Req:
    """The two attributes get_all_models actually reads."""

    class state:            # noqa: D106
        client_source = "platform"


def _all_models():
    import logging

    # get_all_models never consults the local gateway — unlike the other three,
    # it emits ollama rows with their raw ids and no `local:` prefix. Verified
    # live: 13 models, 0 prefixed. That asymmetry is why the prefix assertion
    # below covers only the two catalogues that do apply it.
    def _context_window_for(_mid):
        return 128_000

    fn = _gateway_builder("get_all_models", {
        "Request": _Req,
        "_context_window_for": _context_window_for,
        "logger": logging.getLogger("test"),
    })
    return fn(_Req())


def _oai_models(monkeypatch):
    """26 free names, all of them .env model constants and two helpers.

    The injection list below doubles as a written record of exactly what
    GET /v1/models is built from today — every name in it is one Phase 8
    deletes. That is also why this endpoint is still on plan.html's deferred
    list (D58): it never consults the registry at all.
    """
    import logging

    consts = {
        "_APP_OWNER": "ainxt",
        "_OPENAI_LATEST": "vendor-o-latest",
        "_OPENAI_CODING": "vendor-o-coding",
        "_OPENAI_SIMPLE": "vendor-o-simple",
        "_OPENAI_TERA": "vendor-o-tera",
        "_OPENAI_LUNA": "vendor-o-luna",
        "_CLAUDE_PRIMARY": "vendor-c-primary",
        "_CLAUDE_HAIKU": "vendor-c-haiku",
        "_CLAUDE_OPUS": "vendor-c-opus",
        "_CLAUDE_OPUS_48": "vendor-c-opus-48",
        "_CLAUDE_OPUS_5": "vendor-c-opus-5",
        "_CLAUDE_SONNET_5": "vendor-c-sonnet-5",
        "_GEMINI_TEXT": "vendor-g-text",
        "_GEMINI_CODING_LITE": "vendor-g-lite",
        "_GEMINI_IMAGE": "vendor-g-image",
    }
    flags = {k: True for k in (
        "_ENABLE_OPUS", "_ENABLE_CLI_OPUS_48", "_ENABLE_CLI_OPUS_5",
        "_ENABLE_SONNET_5", "_ENABLE_GPT56_TERA", "_ENABLE_GPT56_LUNA",
    )}

    class _LocalGw:
        def list_models(self):
            return ["llama-test:1b"]

    fn = _gateway_builder("list_oai_models", {
        **consts, **flags,
        "_get_local_gw": lambda: _LocalGw(),
        "_max_out_for_oai": lambda _m: None,
        "logger": logging.getLogger("test"),
    })
    return fn()


# ── the four catalogues ─────────────────────────────────────────────────────


def _ide_models(monkeypatch):
    import routers.ide_router as ide

    class _LocalGw:
        def list_models(self):
            return ["llama-test:1b"]

    monkeypatch.setattr("gateway_local_llm.get_local_gateway",
                        lambda: _LocalGw(), raising=False)
    return ide.ide_get_models(_u={"email": "t@example.com"})


def _compat_models(monkeypatch):
    import routers.messages_compat_router as compat

    monkeypatch.setattr(compat, "_resolve_user",
                        lambda _r: {"user_id": "t", "email": "t@example.com"})

    class _Catalog:
        def all_models(self):
            return ["llama-test:1b"]

    monkeypatch.setattr("gateway_local_llm._catalog", _Catalog(), raising=False)

    class _R:
        headers: dict = {}
        class state:        # noqa: D106
            pass

    return anyio.run(compat.list_models_compat, _R())


CATALOGUES = {
    "GET /all-models":            lambda mp: _all_models(),
    "GET /v1/models (OpenAI)":    _oai_models,
    "GET /ide/models":            _ide_models,
    "GET /v1/models (CLI compat)": _compat_models,
}


# ── tests ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", list(CATALOGUES))
def test_no_tier_name_in_catalogue_response(name, monkeypatch):
    assert_no_tier_name(CATALOGUES[name](monkeypatch), name)


@pytest.mark.parametrize("name", list(CATALOGUES))
def test_the_catalogue_is_not_empty(name, monkeypatch):
    """Guards the assertion above from passing vacuously. A builder that
    raised and returned `{"providers": [{"provider": "Auto", ...}]}` would
    satisfy every tier-name check while showing the user nothing."""
    payload = CATALOGUES[name](monkeypatch)
    ids = {s for p, s in _strings(payload) if p.endswith((".id", ".modelId"))}
    assert len(ids) >= 3, f"{name} returned only {ids!r}"


@pytest.mark.parametrize("name", list(CATALOGUES))
def test_a_model_with_no_tier_assignment_is_still_offered(name, monkeypatch):
    """§H: tier assignment is the administrator's routing concern. A model
    with none is still a model the user may pick, and three of the four
    catalogues would have no way to say otherwise — but the one that filters
    on capabilities could acquire one by accident."""
    payload = CATALOGUES[name](monkeypatch)
    if name == "GET /v1/models (OpenAI)":
        pytest.skip("built from .env constants, never consults the registry (D58)")
    blob = json.dumps(payload)
    assert _UNASSIGNED["model_id"] in blob, (
        f"{name} dropped {_UNASSIGNED['model_id']}, whose capabilities are "
        f"None — §H says it must still be selectable"
    )


@pytest.mark.parametrize("name", ["GET /ide/models", "GET /v1/models (CLI compat)"])
def test_the_local_id_prefix_survives(name, monkeypatch):
    """§P requires the `local:` prefix be preserved unchanged. It is also the
    reason this file matches tier names EXACTLY rather than by substring —
    `local` is a legacy alias, and a substring scan would report the
    convention the plan protects as the defect."""
    blob = json.dumps(CATALOGUES[name](monkeypatch))
    assert "local:" in blob, f"{name} no longer emits the local: id prefix"


def test_the_tier_vocabulary_under_test_is_the_real_one():
    """Without this the parametrised tests above could pass by checking an
    empty set — e.g. if core.tiers stopped exporting ALL_TIERS."""
    assert len(TIER_NAMES) == 8, TIER_NAMES
    assert {"mini", "simple", "medium", "complex"} <= TIER_NAMES


def test_the_seed_really_does_put_tier_names_in_capabilities():
    """The premise of the synthetic row, asserted rather than assumed.

    If db/migrate.py stopped seeding tier-shaped values into
    capabilities.tier_tags, the row above would be testing a leak that cannot
    happen, and the whole file would be theatre.
    """
    migrate = (ROOT / "db" / "migrate.py").read_text(encoding="utf-8", errors="replace")
    i = migrate.index("_AC1_MODEL_ROLE_TAGS")
    body = migrate[i:i + 4000]
    leaked = sorted(t for t in TIER_NAMES if f'"{t}"' in body)
    assert leaked, (
        "db/migrate.py's role-tag seed no longer writes any tier name into "
        "capabilities — re-check whether capabilities is still a leak vector"
    )
