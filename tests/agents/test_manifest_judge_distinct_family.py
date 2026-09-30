# SPDX-License-Identifier: MIT
"""§N.1 step 10 (D44 / N10-c) — the manifest judge's cross-family constraint.

The SDLC manifest cross-validator judges the PLAN. Before this step it did so
by calling ``openai_model_for_tier()`` and then the OpenAI gateway directly,
which encoded "GPT judges Claude's plan" three ways at once: a hardcoded
provider, a hardcoded fallback to OPENAI_LATEST_MODEL, and a dispatch that
never went through ``route()`` at all.

Only one of those was the actual requirement — **the judge must not be from
the family that wrote the thing** — and that is §M.3b's ``distinct_from_family``.

The asymmetry that makes this delicate: ``require_role`` is a PREFERENCE in
``tier_resolver._survivors``, so a single-model deployment still gets an
answer. ``distinct_from_family`` is a hard filter in ``_reject_reason``, so a
single-*family* deployment gets ``NoEligibleModel``. §J.2/R9's rule is "warn,
do not block" — a single-provider estate is legitimate and must still be able
to run SDLC — so the judge has to catch that and proceed, saying plainly that
the cross-check is weaker than it looks.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
COPIES = ("agents/sdlc_pipeline/_phases.py", "agents/sdlc_pipeline/_core.py")


def _judge_fn(rel: str):
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8", errors="replace"))
    for node in tree.body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_phase_validate_manifest"):
            return node
    raise AssertionError(f"_phase_validate_manifest not found in {rel}")


# ── The constraint is applied ───────────────────────────────────────────────


@pytest.mark.parametrize("rel", COPIES)
def test_the_judge_asks_not_to_be_the_authors_family(rel):
    src = ast.dump(_judge_fn(rel))
    assert "distinct_from_family" in src
    assert "author_family" in src


@pytest.mark.parametrize("rel", COPIES)
def test_the_constraint_is_skipped_when_the_author_is_unknown(rel):
    """`model_family()` returns "" for a model the registry does not carry.
    Refusing to review because we cannot identify the author would turn an
    unknown into a blocked gate; the honest answer is to review without the
    constraint."""
    fn = _judge_fn(rel)
    guards = [n for n in ast.walk(fn)
              if isinstance(n, ast.If)
              and "_mv_author_family" in ast.dump(n.test)]
    assert guards, "distinct_from_family is applied unconditionally"


@pytest.mark.parametrize("rel", COPIES)
def test_the_no_eligible_model_case_warns_and_proceeds(rel):
    """N10-c. The single-provider deployment, which is the FIRST thing a
    customer with one vendor hits — not an edge case."""
    fn = _judge_fn(rel)
    dumped = ast.dump(fn)
    assert "NoEligibleModel" in dumped, (
        "nothing catches the hard-filter failure, so a single-family "
        "deployment fails the manifest gate instead of warning")
    # It must RETRY, not just log: a caught-and-returned "" is a skipped gate.
    pops = [n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", "") == "pop"
            and n.args and getattr(n.args[0], "value", "") == "distinct_from_family"]
    assert pops, "the constraint is never dropped, so the retry cannot succeed"


@pytest.mark.parametrize("rel", COPIES)
def test_the_judge_no_longer_forces_an_openai_model(rel):
    """openai_model_for_tier() returned (OPENAI_LATEST_MODEL, fell_back=True)
    for any tier with no OpenAI equivalent — so on a deployment with no OpenAI
    provider at all, the judge resolved to a model id that could not be
    dispatched, every time."""
    src = ast.dump(_judge_fn(rel))
    for gone in ("_omft", "openai_model_for_tier", "_get_openai", "_mv_fellback"):
        assert gone not in src, f"{rel} still references {gone}"


# ── The author family actually travels ──────────────────────────────────────


@pytest.mark.parametrize("rel", COPIES)
def test_the_plan_phase_passes_the_author_family_down(rel):
    """The constraint is only meaningful if the caller supplies the family,
    and the ONLY place that knows it is where the plan model was resolved."""
    src = (ROOT / rel).read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "_phase_validate_manifest"]
    assert calls, f"{rel} never calls the validator"
    for call in calls:
        kw = {k.arg for k in call.keywords}
        assert "author_family" in kw, (
            f"{rel}:{call.lineno} calls the judge without naming the plan's "
            f"author, so distinct_from_family is inert")


def test_model_family_resolves_and_fails_soft():
    from models.model_router import model_family

    assert model_family("") == ""
    assert model_family(None) == ""
    assert model_family("definitely-not-a-registered-model") == ""


def test_model_family_answers_for_a_registered_model():
    """Uses whatever this deployment has rather than naming a model: the
    point is that the lookup works, and asserting a specific id would make
    this test a statement about one machine's database."""
    from core.llm_provider_registry import get_enabled_models
    from models.model_router import model_family

    models = get_enabled_models()
    if not models:
        pytest.skip("no enabled models in the registry on this deployment")
    row = models[0]
    assert model_family(row["model_id"]) == row["family"]
