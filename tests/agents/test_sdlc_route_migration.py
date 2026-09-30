# SPDX-License-Identifier: MIT
"""§N.1 step 10 — the SDLC modules ask for tiers, and BOTH copies do.

Asserted over the AST rather than by importing: none of these modules imports
cleanly under pytest (Jira/GitLab clients, store bindings, a Kafka producer),
which is also why SDLC has no behavioural test coverage at all — 33,688 lines
across 21 modules, and before this file the only three tests in the tree that
mentioned SDLC were the two helper ratchets steps 6 and 8 left behind.

The section that matters most is the last one. agents/sdlc_pipeline/_core.py
and _phases.py define 20 of the same top-level functions — ~1,819 duplicated
lines that the 2026-08-04 "extraction" copied instead of moved. The _core
copies are unreachable today (the live entry points import the _phases ones by
name), and the two have ALREADY diverged once: _phases's manifest validator
grew a router path that _core's never got. Step 10 changed both identically,
and this file is what stops them drifting again.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Every module §N.1 step 10 touched, plus the two it deliberately did not.
SDLC_MODULES = (
    "agents/sdlc_pipeline/_core.py",
    "agents/sdlc_pipeline/_phases.py",
    "agents/sdlc_state_machine.py",
    "agents/sdlc_patch_engine.py",
    "agents/sdlc_normalizer.py",
    "agents/sdlc_context.py",
    "agents/sdlc_governance/config.py",
    "agents/sdlc_governance/pipeline.py",
    "workers/sdlc_worker.py",
    "agents/brd_fsd_pipeline.py",
)

# The vocabulary that must no longer appear as a routing literal.
TIER_WORDS = {"simple", "mini", "medium", "complex", "haiku", "solution",
              "deep", "local", "vision", "opus", "sonnet", "gpt", "claude"}

ROUTING_KEYWORDS = {"model_hint", "hint", "synthesis_hint", "iteration_hint"}

DISPATCH_NAMES = {"generate", "stream", "async_generate", "async_stream",
                  "generate_structured", "route", "_llm", "_llm_traced"}


def _tree(rel: str) -> ast.AST:
    return ast.parse((ROOT / rel).read_text(encoding="utf-8", errors="replace"))


@pytest.mark.parametrize("rel", SDLC_MODULES)
def test_no_routing_keyword_carries_a_tier_literal(rel):
    """The keyword form, over every call — not just the router's entry points.

    Broader than the CI ratchet on purpose: the ratchet has to stay narrow
    because `hint` is a common parameter name repo-wide, but inside these ten
    files a tier word on any *_hint keyword is a migration that did not
    happen.
    """
    bad = []
    for node in ast.walk(_tree(rel)):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if (kw.arg in ROUTING_KEYWORDS
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value in TIER_WORDS):
                bad.append(f"{rel}:{node.lineno} {kw.arg}={kw.value.value!r}")
    assert bad == [], f"unmigrated routing literals: {bad}"


@pytest.mark.parametrize("rel", SDLC_MODULES)
def test_no_dict_literal_carries_a_tier_hint(rel):
    """The splat idiom D33 was written for."""
    bad = []
    for node in ast.walk(_tree(rel)):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and k.value == "model_hint"
                    and isinstance(v, ast.Constant) and v.value in TIER_WORDS):
                bad.append(f"{rel}:{node.lineno} {v.value!r}")
    assert bad == [], f"unmigrated dict literals: {bad}"


@pytest.mark.parametrize("rel", SDLC_MODULES)
def test_no_default_parameter_value_is_a_tier_literal(rel):
    """The shape the ratchet cannot see at all, and the one that bit hardest
    here: `def _llm(prompt, hint="solution")` and
    `def _llm_traced(self, phase, prompt, hint="complex")` put the routing
    decision in a SIGNATURE. Two defaults for one call path, and they
    disagreed — the state machine's wrapper said "complex" while the function
    it wrapped said "solution".
    """
    bad = []
    for node in ast.walk(_tree(rel)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        pairs = list(zip(args.args[len(args.args) - len(args.defaults):], args.defaults))
        pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
        for arg, default in pairs:
            if (arg.arg in ROUTING_KEYWORDS
                    and isinstance(default, ast.Constant)
                    and default.value in TIER_WORDS):
                bad.append(f"{rel}:{node.lineno} def {node.name}({arg.arg}={default.value!r})")
    assert bad == [], f"a routing decision is hiding in a signature: {bad}"


def test_the_sdlc_stage_table_is_the_only_stage_vocabulary():
    """Every stage name any SDLC module asks for must be in the table.

    sdlc_stage_route() logs and routes unhinted for an unknown stage, which is
    honest at runtime but silent — a typo'd stage name would quietly become
    "let the router classify it". The table is small enough to check against.
    """
    from core.model_registry import SDLC_STAGE_TIERS

    asked = set()
    for rel in SDLC_MODULES:
        for node in ast.walk(_tree(rel)):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", getattr(node.func, "attr", "")) in
                        ("sdlc_stage_route", "_sdlc_model")
                    and node.args
                    and isinstance(node.args[0], ast.Constant)):
                asked.add(node.args[0].value)
    assert asked, "no stage lookups found — has the helper been renamed?"
    unknown = asked - set(SDLC_STAGE_TIERS)
    assert not unknown, f"stage(s) asked for but not in SDLC_STAGE_TIERS: {unknown}"


def test_the_cli_spawns_resolve_through_a_tier():
    """The eleven `--model` sites. Each takes its id from one of the named
    phase helpers, all of which go through cli_tier_model_id.

    This is the half of step 10 that closed a real governance gap: before it,
    every one of these read an .env constant and the Admin > Tiers screen had
    no effect on the phases that write the code.
    """
    allowed = {"cli_classify_model", "cli_plan_model", "cli_implement_model",
               "cli_coder_model", "cli_tier_model_id",
               # One level down: agents/sdlc_governance/config.py::review_model
               # and ::fix_model resolve through cli_tier_model_id and
               # cli_coder_model respectively, and apply their own deprecated
               # SDLC_GOVERNANCE_*_MODEL pin on top.
               "review_model", "fix_model"}
    found, bad = 0, []
    for rel in SDLC_MODULES:
        for node in ast.walk(_tree(rel)):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "model":
                    continue
                if isinstance(kw.value, ast.Call):
                    name = getattr(kw.value.func, "id",
                                   getattr(kw.value.func, "attr", ""))
                    if name in allowed:
                        found += 1
                    else:
                        bad.append(f"{rel}:{node.lineno} model={name}(...)")
                elif isinstance(kw.value, ast.Constant):
                    bad.append(f"{rel}:{node.lineno} model={kw.value.value!r}")
    assert found >= 8, f"expected the CLI spawn sites, found {found}"
    assert bad == [], f"a CLI spawn does not resolve through a tier: {bad}"


# ── D49: the duplicated halves must stay in step ────────────────────────────


def _toplevel_defs(rel: str) -> dict:
    tree = _tree(rel)
    return {n.name: n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


CORE_DEFS = _toplevel_defs("agents/sdlc_pipeline/_core.py")
PHASE_DEFS = _toplevel_defs("agents/sdlc_pipeline/_phases.py")
DUPLICATED = sorted(set(CORE_DEFS) & set(PHASE_DEFS))


def test_the_duplication_is_still_what_we_think_it_is():
    """If this number moves, somebody either deleted the dead half (good —
    delete this file's D49 section with it) or added a third copy (bad)."""
    assert len(DUPLICATED) == 20, (
        f"_core.py and _phases.py now share {len(DUPLICATED)} top-level "
        f"functions, not 20: {DUPLICATED}")


@pytest.mark.parametrize("name", DUPLICATED)
def test_both_copies_agree_about_routing(name):
    """The specific drift this step could cause. Whichever copy runs, the
    routing has to be the same — so the set of tier requests and stage
    lookups inside the two definitions must match exactly.

    Not a full source comparison: the copies already differ in ways step 10
    did not introduce (_phases's manifest validator is 31 lines longer), and
    asserting byte-equality would fail on a divergence that predates this
    change and is not this change's to fix.
    """
    def _routing_calls(node):
        out = set()
        for n in ast.walk(node):
            if not isinstance(n, ast.Call):
                continue
            fname = getattr(n.func, "id", getattr(n.func, "attr", ""))
            if fname in ("sdlc_stage_route", "_sdlc_model", "tier_request",
                         "_tier_request", "cli_tier_model_id",
                         "cli_plan_model", "cli_coder_model",
                         "cli_classify_model", "cli_implement_model", "_cpm"):
                args = tuple(a.value for a in n.args if isinstance(a, ast.Constant))
                kws = tuple(sorted(
                    (k.arg, getattr(k.value, "value", "<expr>")) for k in n.keywords))
                out.add((fname, args, kws))
        return out

    core_calls = _routing_calls(CORE_DEFS[name])
    phase_calls = _routing_calls(PHASE_DEFS[name])
    assert core_calls == phase_calls, (
        f"{name}() routes differently in _core.py and _phases.py — "
        f"only in _core: {core_calls - phase_calls}; "
        f"only in _phases: {phase_calls - core_calls}. Step 10 migrated both "
        f"copies deliberately (D49); change both or delete one.")


@pytest.mark.parametrize("copy", ["agents/sdlc_pipeline/_core.py",
                                  "agents/sdlc_pipeline/_phases.py"])
def test_both_manifest_validators_take_the_author_family(copy):
    """§M.3b. The cross-check is only a cross-check if the judge is not from
    the family that wrote the thing — and the parameter is the only way that
    fact travels from the PLAN phase to the validator."""
    fn = _toplevel_defs(copy)["_phase_validate_manifest"]
    names = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
    assert "author_family" in names


@pytest.mark.parametrize("copy", ["agents/sdlc_pipeline/_core.py",
                                  "agents/sdlc_pipeline/_phases.py"])
def test_neither_manifest_validator_dispatches_around_the_router(copy):
    """The ungoverned site step 10 removed: `_mr._get_openai()` followed by
    `_gw.generate(model=…)` reached a provider without passing through
    route(), so the manifest judge was the one SDLC model that governance
    could not see, could not audit and could not block."""
    src = ast.dump(_toplevel_defs(copy)["_phase_validate_manifest"])
    assert "_get_openai" not in src


# ── The class of bug that got through ───────────────────────────────────────


@pytest.mark.parametrize("rel", SDLC_MODULES)
def test_no_call_to_a_method_the_class_does_not_have(rel):
    """Caught in production, not here: step 10 renamed
    NormalizationAgent._model() to ._route() and missed a second caller in a
    log line, so the first real SDLC run died with
    "'NormalizationAgent' object has no attribute '_model'".

    Nothing else in this suite could have seen it. The AST tests above check
    what the routing literals ARE, not whether the surrounding code still
    resolves; the unit tests cannot import these modules; and pyflakes reads
    names, not attributes. A rename with a missed caller is invisible to all
    three — so this check exists at the only level that can see it.

    Deliberately conservative: classes with a base this file cannot resolve
    are skipped entirely, because an inherited method is not a defect and
    guessing would make the check noisy enough to be turned off.
    """
    tree = _tree(rel)
    bad = []
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        if [b for b in cls.bases if not (isinstance(b, ast.Name) and b.id == "object")]:
            continue                      # inherits from something we cannot see
        defined = set()
        for n in ast.walk(cls):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(n.name)
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                            and t.value.id == "self"):
                        defined.add(t.attr)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Attribute):
                if isinstance(n.target.value, ast.Name) and n.target.value.id == "self":
                    defined.add(n.target.attr)
        for n in ast.walk(cls):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "self"
                    and n.func.attr not in defined):
                bad.append(f"{rel}:{n.lineno} self.{n.func.attr}() is not defined "
                           f"on {cls.name}")
    assert bad == [], f"dangling self-call(s): {bad}"
