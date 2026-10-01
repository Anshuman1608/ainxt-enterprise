# SPDX-License-Identifier: MIT
"""§M.3 cross-provider review actually reaches two providers.

`_run_review(self, model, prompt)` took a model and never used it — every
future ran the same `tier=Tier.SIMPLE` request, so `multi_model_consensus`
scored a model against itself and `POST /review`'s `models` override was
accepted and ignored. Present since the initial commit; the Phase 6 migration
only swapped `model_hint="simple"` for the tier and kept the bug.

`ReviewResult.model` was equally decorative: it labelled each half with a SKU
constant that had not been dispatched.
"""

from __future__ import annotations

import pytest

from agents.review_engine import ReviewEngine


class _Cands:
    """Stand-in for core.tier_resolver.ResolvedModel."""

    def __init__(self, model_id, family):
        self.model_id = model_id
        self.family = family


@pytest.fixture
def dispatched(monkeypatch):
    """Capture every (model_hint, tier) the engine dispatches."""
    seen = []

    class _Router:
        def generate(self, prompt, **kw):
            seen.append(kw)
            return '{"issues": [], "summary": "ok", "score": 1.0}'

    monkeypatch.setattr("models.model_router.get_router", lambda: _Router())
    return seen


def _patch_candidates(monkeypatch, rows, distinct=None):
    from core import tier_resolver as tr

    def _fake(tier, c=None, **kw):
        if c is not None and getattr(c, "distinct_from_family", None):
            if distinct is None:
                raise tr.NoEligibleModel(tier, c, {})
            return distinct
        return rows

    monkeypatch.setattr(tr, "resolve_tier_candidates", _fake)


def test_the_two_reviewers_are_different_models(monkeypatch, dispatched):
    _patch_candidates(monkeypatch, [
        _Cands("claude-haiku-4-5", "anthropic"),
        _Cands("gemini-3-flash", "google"),
    ])
    ReviewEngine().multi_model_consensus(code="x = 1", review_type="security")

    hints = [kw.get("model_hint") for kw in dispatched]
    assert len(hints) == 2, f"expected two dispatches, got {hints}"
    assert hints[0] != hints[1], (
        "both reviewers dispatched the same model — this is the defect: the "
        "consensus score compares a model with itself and always reads 1.0"
    )
    assert set(hints) == {"claude-haiku-4-5", "gemini-3-flash"}


def test_the_second_reviewer_is_a_different_family(monkeypatch, dispatched):
    """§M.3b — a model must not mark its own family's homework."""
    _patch_candidates(
        monkeypatch,
        [_Cands("claude-haiku-4-5", "anthropic"),
         _Cands("claude-sonnet-5", "anthropic")],
        distinct=[_Cands("gemini-3-flash", "google")],
    )
    pair = ReviewEngine()._default_reviewer_pair()
    assert pair == ["claude-haiku-4-5", "gemini-3-flash"], (
        "the tier's own list was all one family, so the resolver must be "
        "re-asked with distinct_from_family rather than settling for it"
    )


def test_a_single_family_deployment_reviews_once(monkeypatch, dispatched):
    """One honest reviewer beats two calls to the same model."""
    _patch_candidates(
        monkeypatch,
        [_Cands("claude-haiku-4-5", "anthropic")],
        distinct=None,
    )
    result = ReviewEngine().multi_model_consensus(code="x = 1")

    assert len(dispatched) == 1
    assert len(result.model_results) == 1
    assert result.consensus_score == 1.0


def test_a_caller_supplied_model_list_is_honoured(monkeypatch, dispatched):
    """POST /review documents `models` as "Override default model pair"."""
    ReviewEngine().multi_model_consensus(
        code="x = 1", models=["model-a", "model-b"])

    assert [kw.get("model_hint") for kw in dispatched] == ["model-a", "model-b"]


def test_the_result_is_labelled_with_the_model_that_ran(monkeypatch, dispatched):
    _patch_candidates(monkeypatch, [
        _Cands("claude-haiku-4-5", "anthropic"),
        _Cands("gemini-3-flash", "google"),
    ])
    result = ReviewEngine().multi_model_consensus(code="x = 1")

    labelled = {r.model for r in result.model_results}
    assert labelled == {"claude-haiku-4-5", "gemini-3-flash"}


def test_no_eligible_model_degrades_rather_than_raising(monkeypatch, dispatched):
    from core import tier_resolver as tr

    def _raise(tier, c=None, **kw):
        raise tr.NoEligibleModel(tier, c or tr.Constraints(), {})

    monkeypatch.setattr(tr, "resolve_tier_candidates", _raise)
    result = ReviewEngine().multi_model_consensus(code="x = 1")

    assert result.consensus_score == 0.0
    assert not dispatched


def test_the_engine_names_no_vendor_constant():
    """The import that MIGRATED_STILL_IMPORTING recorded is gone."""
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[2]
           / "agents" / "review_engine.py").read_text()
    named = {
        a.name
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.ImportFrom)
        and (n.module or "").endswith("model_registry")
        for a in n.names
    }
    assert not named, f"review_engine.py imports SKU constants again: {named}"
