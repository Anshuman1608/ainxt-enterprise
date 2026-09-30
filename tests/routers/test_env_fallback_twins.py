# SPDX-License-Identifier: MIT
"""Phase 7 — the two env-var model catalogues stay in step.

There are two, not one. ``plan.html``'s Phase 7 names only the first:

  * ``routers/messages_compat_router.py::_list_models_compat_env_fallback``
  * ``AgentStudio/backend/app/api/generation.py::_cli_reference_models_env_fallback``

Both build the CLI-shaped model catalogue from ``core.model_registry`` env
constants, both are reached only when ``core.llm_provider_registry`` cannot be
read, and both gate the same six per-SKU ``ENABLE_*`` flags.

**They are deliberately not unified.** The Agent Studio copy exists for the
case where ``core`` is not importable at all — its own ImportError branch says
so — so lifting the shared body into ``core/`` would make it unreachable in
precisely the situation it is for. Two copies with a test comparing them is the
same arrangement step 10 settled on for ``_core.py`` / ``_phases.py``, and for
the same reason: the duplication is load-bearing, the drift is not.

What drift looks like here, from the Agent Studio copy's own comment: it had
``OPENAI_LATEST_MODEL`` as ``"gpt-5-5"`` while the registry default was
``"gpt-5.5"`` — two strings for one concept, and nothing to notice.

Read off the source: neither module imports under pytest (one attaches
FastAPI routers, the other needs the Agent Studio package root).
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPAT = ROOT / "routers" / "messages_compat_router.py"
STUDIO = ROOT / "AgentStudio" / "backend" / "app" / "api" / "generation.py"

TWINS = (
    (COMPAT, "_list_models_compat_env_fallback"),
    (STUDIO, "_cli_reference_models_env_fallback"),
)

# The six per-SKU switches both copies gate on. Phase 8 removes these along
# with the variables; until then both must agree about them, because a flag
# honoured in one catalogue and ignored in the other means the CLI and Agent
# Studio pickers disagree about what the deployment offers.
SKU_FLAGS = (
    "ENABLE_OPUS",
    "ENABLE_CLI_OPUS_48",
    "ENABLE_CLI_OPUS_5",
    "ENABLE_SONNET_5",
    "ENABLE_GPT56_TERA",
    "ENABLE_GPT56_LUNA",
)


def _fn_source(path: pathlib.Path, name: str) -> str:
    src = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name}() not found in {path.name}")


def _strip_comments(body: str) -> str:
    """Drop comments and the docstring. Both copies now explain the flags in
    prose, so a raw scan would find the explanation rather than the code."""
    body = re.sub(r'"""[\s\S]*?"""', "", body, count=1)   # docstring
    return re.sub(r"^\s*#.*$", "", body, flags=re.M)


def _flags_in(body: str) -> set[str]:
    """Which SKU flags a body actually reads.

    Word-boundary matched, not `in`. A plain containment test cannot see a
    flag being RENAMED — "ENABLE_GPT56_LUNA" is a substring of
    "ENABLE_GPT56_LUNA_REMOVED" — which is exactly the revert this file exists
    to catch, and it went undetected until the revert was run.
    """
    return {f for f in SKU_FLAGS if re.search(rf"\b{re.escape(f)}\b", body)}


@pytest.fixture(scope="module")
def bodies() -> dict[str, str]:
    return {path.name: _strip_comments(_fn_source(path, name)) for path, name in TWINS}


# ── Both exist and are still the degraded path ─────────────────────────────


@pytest.mark.parametrize("path,name", TWINS)
def test_the_fallback_exists(path: pathlib.Path, name: str):
    if not path.exists():
        pytest.skip(f"{path} is not checked out")
    assert _fn_source(path, name)


@pytest.mark.parametrize("path,name", TWINS)
def test_the_registry_is_still_primary(path: pathlib.Path, name: str):
    """The fallback must stay a fallback. If a caller reaches it without first
    trying the registry, a deployment configured purely through the admin
    screen gets an env-var catalogue that is mostly blank — the bug that
    switching these callers to the registry fixed."""
    if not path.exists():
        pytest.skip(f"{path} is not checked out")
    src = path.read_text(encoding="utf-8", errors="replace")
    assert "core.llm_provider_registry" in src
    # The fallback is called from inside an exception path / None-check, never
    # unconditionally.
    caller = src[: src.index(f"def {name}")]
    assert f"{name}()" in caller, f"{name} is defined but never reached"


# ── The two agree ──────────────────────────────────────────────────────────


def test_both_gate_the_same_sku_flags(bodies):
    """The drift this file exists to catch. A flag read by one catalogue and
    not the other means the CLI picker and the Agent Studio picker offer
    different models on the same deployment."""
    if len(bodies) < 2:
        pytest.skip("only one twin is checked out")
    per_file = {name: _flags_in(body) for name, body in bodies.items()}
    names = list(per_file)
    assert per_file[names[0]] == per_file[names[1]], (
        f"the two env fallbacks gate different per-SKU flags: "
        f"{names[0]}={sorted(per_file[names[0]])} vs "
        f"{names[1]}={sorted(per_file[names[1]])}"
    )


def test_the_flags_are_all_still_honoured(bodies):
    """D63: Phase 7 keeps the gates and Phase 8 removes them with the
    variables. Removing them here first would make a disabled SKU offered AND
    servable while the registry — and therefore every other governance
    mechanism — is unreachable.

    When Phase 8 lands, this test is the one that should fail, and SKU_FLAGS
    is where to record it.
    """
    if not bodies:
        pytest.skip("no twin is checked out")
    for name, body in bodies.items():
        missing = sorted(set(SKU_FLAGS) - _flags_in(body))
        assert not missing, (
            f"{name} no longer honours {missing}. If this is Phase 8, remove "
            f"the flag from SKU_FLAGS and from core/model_registry.py in the "
            f"same change — not from the catalogue alone."
        )


def test_neither_copy_was_lifted_into_core(bodies):
    """The duplication is the point (see the module docstring). A shared helper
    imported from ``core`` would be unimportable on the one path the Agent
    Studio copy serves."""
    if "generation.py" not in bodies:
        pytest.skip("Agent Studio is not checked out")
    body = bodies["generation.py"]
    assert "from core.model_registry import" in body, (
        "the Agent Studio fallback no longer reads the env constants directly; "
        "if it now imports a shared helper, confirm that helper is importable "
        "when `core` is not on the path — which is the case this branch exists for"
    )
