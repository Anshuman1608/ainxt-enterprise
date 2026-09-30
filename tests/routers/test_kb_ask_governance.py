# SPDX-License-Identifier: MIT
"""Phase 7 — ``/kb/ask`` refuses an explicitly picked model the user may not use.

``routers/kb_ask_router.py`` is ``gateway.py``'s ``/ask`` forked
character-for-character, and the fork did not bring the governance block with
it. §N.1 step 9 noticed half of that and wired the **Auto-path** candidate
filter (``acl_filter``); the **explicit-pick** 403 was still missing, so naming
a blocked model in the request body was never checked on this endpoint at all.

It stayed invisible because the two halves failed together. ``KbChat.jsx``
treated an empty allowlist as "the response has not arrived" rather than
"everything is blocked", so a fully-blocked user was shown the entire
catalogue — and when they picked from it, this endpoint served it. Fixing only
the picker would have left a direct ``POST /kb/ask`` able to name any model, so
both halves land together and are asserted separately.

Assertions are over the source. ``kb_ask_router.py`` pulls in the KB retrieval
stack at import time and does not import under pytest, the same constraint
steps 9 and 6.6 recorded for ``gateway.py``.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
KB = ROOT / "routers" / "kb_ask_router.py"
GATEWAY = ROOT / "gateway.py"


@pytest.fixture(scope="module")
def src() -> str:
    return KB.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def fn(src: str) -> ast.AST:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "kb_ask_ai":
            return node
    raise AssertionError("kb_ask_ai() not found in kb_ask_router.py")


# ── The check exists ───────────────────────────────────────────────────────


def test_the_endpoint_checks_an_explicit_pick(fn):
    """The whole finding: this call was absent, so a department rule could not
    reach a /kb/ask turn that named its own model."""
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", "") in ("_gov_filter", "filter_allowed_models")
    ]
    assert calls, (
        "kb_ask_ai never calls filter_allowed_models — an explicitly picked "
        "model is dispatched without an access check"
    )


def test_it_resolves_the_pick_through_the_registry(src):
    """D37. ``hint_to_model_id()`` answers an .env CONSTANT, which is falsy for
    five of the eight tier heads on this deployment — and a falsy value skips
    the check. That is how the same block on /ask was silently optional before
    step 9. ``resolve_pick_to_model_id`` answers the id that is actually
    dispatched.

    Read off the AST, not the text: the block deliberately NAMES the helper it
    replaced, in a comment, so a substring scan over the source reports the
    explanation as the defect."""
    assert "resolve_pick_to_model_id" in src

    imported: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if name:
                imported.add(name)
    assert "hint_to_model_id" not in imported, (
        "the ACL target is resolved through the pre-registry .env map, which is "
        "falsy for models that exist and would skip the check"
    )


def test_it_returns_the_same_code_as_the_chat_path(src):
    """The clients treat this as one error. A different code or status here
    would mean the KB path needs its own handling for the same condition."""
    assert "MODEL_GOVERNANCE_BLOCKED" in src
    assert "model_not_allowed" in src
    assert "status_code=403" in src


def test_it_handles_the_local_model_case(src):
    """A "local"/"simple" hint carries the real model name in ``local_model``,
    so resolving the hint alone yields nothing to check. /ask has always
    special-cased this; a copy that dropped it would pass every local pick."""
    assert 'f"local:{q.local_model}"' in src


# ── Where it sits ──────────────────────────────────────────────────────────


def test_the_check_runs_before_retrieval(src):
    """/ask checks immediately after deriving the hint, well before any
    expensive work. Checking after retrieval would bill an embedding search and
    a rerank for a request that is about to 403 — and, worse, would leave the
    ordering free to drift behind the LLM call."""
    gov = src.index("MODEL GOVERNANCE ENFORCEMENT")
    retrieval = src.index("run_kb_retrieval(")
    assert gov < retrieval, (
        "the governance check is placed after KB retrieval runs"
    )


def test_it_only_fires_on_an_explicit_pick(src):
    """§G: an Auto turn is governed by the candidate filter further down, which
    is a different mechanism with a different failure mode (it degrades to the
    next candidate rather than 403-ing). Applying both to one turn would 403 a
    user whose Auto turn had a perfectly good second candidate."""
    assert "if _model_hint:" in src


def test_it_fails_open(src):
    """Governance never takes KB chat down. Same rule as /ask and as step 9's
    ACL filter one screen below this block."""
    block = src[src.index("MODEL GOVERNANCE ENFORCEMENT"):
                src.index("END MODEL GOVERNANCE ENFORCEMENT")]
    assert "fail-open" in block
    assert "except Exception" in block


# ── Parity with the path it was copied from ────────────────────────────────


def test_the_auto_path_filter_is_still_there(src):
    """Step 9's work. The explicit-pick check is additional to it, not a
    replacement — removing either leaves a whole class of turn ungoverned."""
    assert "acl_filter_for" in src
    assert "acl_filter" in src


def test_both_paths_use_the_same_helpers_as_the_chat_endpoint():
    """The two endpoints are a known fork. When they disagree about which
    helper answers "may this user use this model", one of them is wrong — and
    this file exists because that already happened once."""
    kb = KB.read_text(encoding="utf-8", errors="replace")
    gw = GATEWAY.read_text(encoding="utf-8", errors="replace")
    for helper in ("filter_allowed_models", "resolve_pick_to_model_id",
                   "MODEL_GOVERNANCE_BLOCKED"):
        assert helper in kb and helper in gw, (
            f"{helper} is used by only one of the two forked ask endpoints"
        )
