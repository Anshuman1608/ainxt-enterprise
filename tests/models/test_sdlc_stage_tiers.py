# SPDX-License-Identifier: MIT
"""§N.1 step 10 — the SDLC stage table.

SDLC_STAGE_MODEL_DEFAULTS mapped 17 stage names to router HINT strings, read
through sdlc_stage_hint() and resolved against the .env model constants.
SDLC_STAGE_TIERS maps the stages that have a caller to a CAPABILITY, and an
administrator picks the model.

Two things this file pins that are easy to lose:

  * the tiers come from what each stage DOES (plan.html §D.2), not from which
    model happens to sit in a tier today. A mapping derived from the live
    assignment table goes stale the first time an administrator edits a row —
    which is exactly what happened to Revision 11's correction 6.
  * the twelve inert stages are GONE, not carried forward. An operator who set
    SDLC_MODEL_DESIGN believed they had changed something and had not.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from core.model_registry import SDLC_STAGE_TIERS
from core.tier_resolver import ROLE_REVIEW
from core.tiers import Tier

ROOT = pathlib.Path(__file__).resolve().parents[2]


# The table, written out independently of the implementation so a change to
# either has to be made here too. (stage, tier, legacy_hint, constraints)
EXPECTED = {
    # Bounded structured extraction that must parse — §D.2 `simple`.
    "locate":            (Tier.SIMPLE,  "haiku",    {}),
    "normalize":         (Tier.SIMPLE,  "haiku",    {}),
    # Agentic code generation against visible code — §D.2 `complex`.
    "coder":             (Tier.COMPLEX, "complex",  {}),
    # A review GATE: the role lives inside the tier (§M.3a).
    "code_review":       (Tier.COMPLEX, "solution", {"require_role": ROLE_REVIEW}),
    # Judges a plan, so it needs what wrote it. The cross-FAMILY requirement is
    # supplied per call by the caller that knows the author (§M.3b), not here.
    "manifest_validate": (Tier.COMPLEX, "deep",     {}),
}

# Declared by the old table and read by nothing. Listed by name so that
# re-adding one is a deliberate act with a test to update.
INERT_BEFORE_STEP_10 = (
    "classify", "fixer", "noncode", "exploration", "analyze", "design",
    "plan", "solution_review", "synthesis", "cross_model_review", "diagnose",
    "pre_coding_build",
)


def test_the_table_is_exactly_the_five_live_stages():
    assert set(SDLC_STAGE_TIERS) == set(EXPECTED)


@pytest.mark.parametrize("stage", sorted(EXPECTED))
def test_each_stage_maps_to_the_capability_it_needs(stage):
    assert SDLC_STAGE_TIERS[stage] == EXPECTED[stage]


@pytest.mark.parametrize("stage", INERT_BEFORE_STEP_10)
def test_the_inert_stages_are_gone(stage):
    """They had no caller. Keeping them would keep advertising twelve knobs
    that are not wired to anything, inside the table the migration exists to
    make honest."""
    assert stage not in SDLC_STAGE_TIERS


def test_only_the_review_gate_asks_for_the_review_role():
    """D43, and the behaviour change worth stating: BEFORE this step, _llm()'s
    default hint was "solution", which _LEGACY_TO_GOVERNED maps to
    (COMPLEX, require_role="review"). So every hintless SDLC call — every
    synthesis, every agent fallback, the self-review repair loop — was
    competing for the reviewer. Now only the gate asks."""
    with_role = {s for s, (_t, _h, c) in SDLC_STAGE_TIERS.items() if c.get("require_role")}
    assert with_role == {"code_review"}


def test_no_stage_asks_for_a_tier_that_does_not_exist():
    for stage, (tier, _hint, _c) in SDLC_STAGE_TIERS.items():
        assert isinstance(tier, Tier), f"{stage} does not name a Tier"


def test_every_legacy_hint_is_one_the_router_understands():
    """D15/D50. `legacy_hint` is dispatched verbatim with governance off, and
    _coerce_tier RAISES on a hint that is not a known alias — so a typo here
    is not a wrong model, it is an exception on the first SDLC run of a
    flag-off deployment."""
    from core.tiers import LEGACY_INBOUND_ALIASES

    for stage, (_tier, hint, _c) in SDLC_STAGE_TIERS.items():
        assert hint in LEGACY_INBOUND_ALIASES, f"{stage}: {hint!r} is not a router hint"


def test_the_constraints_are_things_the_resolver_actually_reads():
    """A constraint key the resolver does not know would be silently dropped
    by Constraints(**kw) — or, worse, raise TypeError at dispatch time on a
    stage nobody has a test for."""
    import dataclasses

    from core.tier_resolver import Constraints

    known = {f.name for f in dataclasses.fields(Constraints)}
    for stage, (_t, _h, constraints) in SDLC_STAGE_TIERS.items():
        unknown = set(constraints) - known
        assert not unknown, f"{stage}: Constraints has no field(s) {unknown}"


# ── The resolver that reads the table ───────────────────────────────────────


def test_sdlc_stage_route_returns_splattable_kwargs():
    from models.model_router import sdlc_stage_route

    route = sdlc_stage_route("coder")
    assert route == {"tier": Tier.COMPLEX, "legacy_hint": "complex"}


def test_sdlc_stage_route_carries_the_constraint_through():
    from models.model_router import sdlc_stage_route

    assert sdlc_stage_route("code_review")["require_role"] == ROLE_REVIEW


def test_an_unknown_stage_is_not_silently_given_a_tier():
    """sdlc_stage_hint's `default="complex"` gave an unregistered stage a
    confident answer. An empty hint means "classify this prompt yourself",
    which is the honest one — and it is logged."""
    from models.model_router import sdlc_stage_route

    assert sdlc_stage_route("no_such_stage") == {"model_hint": ""}


def test_the_per_stage_env_pin_still_wins(monkeypatch):
    """D47. §I restricts SDLC_MODEL_<STAGE> to the eight tier names, but that
    is Phase 8 — doing it here would break an operator's pin on upgrade."""
    from models.model_router import sdlc_stage_route

    monkeypatch.setenv("SDLC_MODEL_CODER", "my-inhouse-qwen")
    assert sdlc_stage_route("coder") == {"model_hint": "my-inhouse-qwen"}


def test_a_pinned_stage_does_not_also_get_the_constraint(monkeypatch):
    """An operator who names a model has answered the question. Adding
    require_role on top would be the router second-guessing an explicit
    instruction — and require_role is meaningless without a tier to rank
    within."""
    from models.model_router import sdlc_stage_route

    monkeypatch.setenv("SDLC_MODEL_CODE_REVIEW", "gpt-5.5")
    route = sdlc_stage_route("code_review")
    assert route == {"model_hint": "gpt-5.5"}
    assert "require_role" not in route


# ── The old mechanism is gone ───────────────────────────────────────────────


def _source_modules():
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("venv/", ".git/", "node_modules/", "tests/",
                           "AgentStudio/frontend/")):
            continue
        yield rel, path


def test_nothing_imports_sdlc_stage_hint_any_more():
    """Parsed, not grepped: the migrated modules deliberately NAME the old
    resolver in comments explaining what was wrong with it, and a text scan
    would report the documentation of the fix as the defect. That trap has
    now caught tests in five separate steps of this migration."""
    offenders = []
    for rel, path in _source_modules():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "core.model_registry":
                if any(a.name == "sdlc_stage_hint" for a in node.names):
                    offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], f"sdlc_stage_hint is still imported by {offenders}"


def test_the_old_defaults_dict_is_gone():
    import core.model_registry as mr

    assert not hasattr(mr, "SDLC_STAGE_MODEL_DEFAULTS")
    assert not hasattr(mr, "sdlc_stage_hint")
