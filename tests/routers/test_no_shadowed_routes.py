# SPDX-License-Identifier: MIT
"""D84 — two routers may not claim the same (method, path).

Starlette matches routes in registration order and the first match wins, so a
duplicate registration does not error: the later handler simply never runs.
`gateway.py::list_oai_models` sat in that state unnoticed for the whole
migration — `routers/messages_compat_router.py` has no internal prefix, so its
`@router.get("/v1/models")` landed on the same path `_v1` claimed 14.8k lines
later, and the earlier registration won. The dead function was still audited at
startup and still published in /openapi.json, which is why nothing looked wrong.

Measuring found it was one of ten such pairs, five of them shadowing a
gateway.py handler. The ratchet is shrink-only; the survivors are named below
so a fix has to update the record rather than quietly re-balance the count.

The resolver is static, and it is only worth anything if it agrees with the
running app. It was built against a live dump of `app.routes` and reproduces
that dump's collision set exactly — same ten pairs, same handlers, same
modules. Do not "simplify" it without re-checking against a live table.
"""

from __future__ import annotations

import ast
import collections
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "gateway.py"

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}

#: Measured 2026-10-01 against the live route table. May fall, never rise.
#: Was 10; D79 removed two, D94 the last eight.
BASELINE_SHADOWED = 0

KNOWN_SHADOWED: set = set()

#: Who serves each path that used to be registered twice (D94). Three of the
#: dead copies held what the live one lacked — an ownership check, the
#: thumbs-down fields, a correct SSO flag — so those were ported before the
#: copy went. Pinning the survivor catches the wrong side being deleted.
SURVIVORS = {
    ("DELETE", "/ainxt/v1/api/auth/sessions"): "routers/auth_router.py",
    ("DELETE", "/ainxt/v1/api/auth/sessions/{session_id}"): "routers/auth_router.py",
    ("GET", "/ainxt/v1/api/auth/sessions"): "routers/auth_router.py",
    ("GET", "/ainxt/v1/api/auth/sso/provider"): "routers/auth_router.py",
    ("GET", "/ainxt/v1/api/chats"): "routers/chat_router.py",
    ("GET", "/ainxt/v1/api/chats/{chat_id}/messages"): "routers/chat_router.py",
    ("POST", "/ainxt/v1/api/chat/messages/{message_id}/feedback"): "routers/chat_router.py",
    ("POST", "/ainxt/v1/api/index/submit"): "routers/index_router.py",
}


def _const(node):
    return node.value if isinstance(node, ast.Constant) else None


def _kwarg(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return _const(kw.value)
    return None


def _parse(path: pathlib.Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return None


def _router_prefixes(path: pathlib.Path) -> dict:
    """{var: prefix} for every APIRouter(...) assigned in a module."""
    out: dict = {}
    tree = _parse(path)
    if tree is None:
        return out
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        func = node.value.func
        if (getattr(func, "id", None) or getattr(func, "attr", None)) != "APIRouter":
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = _kwarg(node.value, "prefix") or ""
    return out


def _decorated_routes(path: pathlib.Path) -> list:
    """[(var, METHOD, path, handler)] from @var.method("path") decorators."""
    out: list = []
    tree = _parse(path)
    if tree is None:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if dec.func.attr not in HTTP_METHODS:
                continue
            if not isinstance(dec.func.value, ast.Name):
                continue
            route = _const(dec.args[0]) if dec.args else None
            if isinstance(route, str):
                out.append((dec.func.value.id, dec.func.attr.upper(), route, node.name))
    return out


@pytest.fixture(scope="module")
def mounted() -> list:
    """Every (METHOD, full_path, handler, module) gateway.py mounts."""
    gw_tree = _parse(GATEWAY)
    assert gw_tree is not None, "gateway.py does not parse"

    # alias -> (defining module, imported name), from gateway.py's own imports
    alias_source: dict = {}
    for node in ast.walk(gw_tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            module = ROOT / (node.module.replace(".", "/") + ".py")
            for name in node.names:
                alias_source[name.asname or name.name] = (module, name.name)

    includes = []
    for node in ast.walk(gw_tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "include_router" and node.args
                and isinstance(node.args[0], ast.Name)):
            includes.append((node.args[0].id, _kwarg(node, "prefix") or ""))

    gw_prefixes = _router_prefixes(GATEWAY)
    gw_routes = _decorated_routes(GATEWAY)

    out: list = []
    for alias, include_prefix in includes:
        if alias in gw_prefixes:                       # router defined in gateway.py
            module, prefix = GATEWAY, gw_prefixes[alias]
            rows = [r for r in gw_routes if r[0] == alias]
        else:
            source = alias_source.get(alias)
            if source is None or not source[0].exists():
                continue
            module, var = source
            prefix = _router_prefixes(module).get(var, "")
            rows = [r for r in _decorated_routes(module) if r[0] == var]
        for _var, method, route, handler in rows:
            out.append((method, include_prefix + prefix + route, handler,
                        module.relative_to(ROOT).as_posix()))

    # Routes hung straight off `app` carry no prefix.
    for var, method, route, handler in gw_routes:
        if var == "app":
            out.append((method, route, handler, "gateway.py"))
    return out


@pytest.fixture(scope="module")
def collisions(mounted: list) -> dict:
    seen: dict = collections.defaultdict(list)
    for method, path, handler, module in mounted:
        seen[(method, path)].append((handler, module))
    return {k: v for k, v in seen.items() if len(v) > 1}


def test_the_resolver_resolves_the_whole_app(mounted: list) -> None:
    """A resolver that finds nothing would make every assertion below vacuous."""
    assert len(mounted) > 700, (
        f"only {len(mounted)} routes resolved — the live table holds ~750, so "
        f"the import-alias or prefix join has broken"
    )


def test_no_new_shadowed_route(collisions: dict) -> None:
    assert len(collisions) <= BASELINE_SHADOWED, (
        f"{len(collisions)} shadowed (method, path) pairs, baseline "
        f"{BASELINE_SHADOWED}. A second registration of the same path is never "
        f"reached — Starlette takes the first. New: "
        f"{sorted(set(collisions) - KNOWN_SHADOWED)}"
    )


def test_the_shadowed_set_is_exactly_the_recorded_one(collisions: dict) -> None:
    """Pinned as a set, not a count, so fixing one and adding another fails."""
    assert set(collisions) == KNOWN_SHADOWED, (
        f"unexpected: {sorted(set(collisions) - KNOWN_SHADOWED)}; "
        f"fixed (update KNOWN_SHADOWED and BASELINE_SHADOWED): "
        f"{sorted(KNOWN_SHADOWED - set(collisions))}"
    )


@pytest.mark.parametrize("key", sorted(SURVIVORS))
def test_the_live_handler_is_the_one_that_survived(mounted: list, key) -> None:
    method, path = key
    owners = [m for meth, p, _h, m in mounted if (meth, p) == key]
    assert owners == [SURVIVORS[key]], f"{method} {path} is served by {owners}"


@pytest.mark.parametrize("path", ["/ainxt/v1/api/models", "/ainxt/v1/api/v1/models"])
def test_the_openai_catalogue_has_one_registration(mounted: list, path: str) -> None:
    """D79: the shadowed env-constant copy is gone, and may not come back."""
    owners = [(h, m) for method, p, h, m in mounted if p == path and method == "GET"]
    assert len(owners) == 1, f"GET {path} registered {len(owners)}x: {owners}"
    handler, module = owners[0]
    assert handler == "list_models_compat", handler
    assert module == "routers/messages_compat_router.py", module


def test_list_oai_models_is_gone(mounted: list) -> None:
    """It read env constants the registry never saw, and served no request."""
    assert "list_oai_models" not in {h for _m, _p, h, _mod in mounted}
    assert "def list_oai_models" not in GATEWAY.read_text(
        encoding="utf-8", errors="replace")
