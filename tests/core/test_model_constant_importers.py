# SPDX-License-Identifier: MIT
"""No application module may reach a model by importing its .env constant.

plan.html §P: *"Application-controlled tasks use only the 8 tiers"*, and its
CI-shaped restatement — *"every non-test, non-vendored file containing three
or more distinct vendor SKU literals must be either a provider adapter, a
cost/normalisation table, or an audit path — never routing or selection
logic."* A constant is the same coupling as a literal with a nicer name:
``from core.model_registry import CLAUDE_PRIMARY_MODEL`` is a module deciding
which vendor answers, which is the administrator's decision.

THE HOLE THIS FILE EXISTS TO CLOSE
----------------------------------
``scripts/ci/release_checks.py::check_tier_migration`` already ratchets the
modules §N.1 migrated — but it looks for ``{"model_hint": "<tier>"}`` dict
literals and nothing else. A module on that list may therefore import as many
SKU constants as it likes and stay green.

It did — six times when this file was written. Four have since been fixed as
Phase 8 prerequisites (the constants are deleted there, and each would have
become ``""`` rather than an error). The two left in
``MIGRATED_STILL_IMPORTING`` below are Phase 8's own work, with the reason
recorded against each.

WHY A RATCHET AND NOT A ZERO-RULE
---------------------------------
Measured: **13 modules, 116 import sites** (was 17 / 122). Four hold most of
it and all four are Phase 8's own targets — deleting the constants is that
phase's entire job, so a zero-rule today would be a rule nobody can satisfy
until after the phase that makes it satisfiable. The ratchet is the convention this
repository already uses for exactly this situation
(``release_checks._MODEL_LITERAL_BASELINE``): the count may fall, never rise.

The tail of modules with one to three imports each is what it actually
guards, and the tail is where a new coupling would appear.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import warnings

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "core" / "model_registry.py"


# ── the classification ──────────────────────────────────────────────────────
#
# Every public constant in core/model_registry.py is in exactly one bucket,
# and test_every_constant_is_classified proves it. That completeness check is
# the load-bearing part: without it a constant added tomorrow is silently
# outside the rule, which is precisely how review_engine.py stayed invisible.

#: Names a concrete vendor model. Phase 8 deletes every one of these.
SKU_NAME = frozenset({
    "CLAUDE_HAIKU", "CLAUDE_OPUS_46_MODEL", "CLAUDE_OPUS_48_MODEL",
    "CLAUDE_OPUS_5_MODEL", "CLAUDE_OPUS_MODEL", "CLAUDE_PRIMARY_MODEL",
    "CLAUDE_SONNET_5_MODEL",
    "GEMINI_CODING_LITE_MODEL", "GEMINI_IMAGE_MODEL", "GEMINI_TEXT_MODEL",
    "GEMINI_VISION_MODEL",
    "OPENAI_CODING_MODEL", "OPENAI_DEEP_RESEARCH", "OPENAI_DEEP_RESEARCH_MINI",
    "OPENAI_IMAGE_MODEL", "OPENAI_LATEST_MODEL", "OPENAI_LUNA_MODEL",
    "OPENAI_OSS_MODEL", "OPENAI_PRIMARY_MODEL", "OPENAI_SIMPLE_MODEL",
    "OPENAI_TERA_MODEL",
    "LOCAL_LLM_MODEL_NAME", "SOLUTION_MODEL", "VEO_MODEL",
})

#: A display label for one SKU. Also Phase 8's, and also a selection coupling:
#: a module that shows "Claude Opus 4.7" has decided what answered.
SKU_DISPLAY = frozenset({
    "CLAUDE_HAIKU_DISPLAY", "CLAUDE_OPUS_48_DISPLAY", "CLAUDE_OPUS_5_DISPLAY",
    "CLAUDE_OPUS_DISPLAY", "CLAUDE_PRIMARY_DISPLAY", "CLAUDE_SONNET_5_DISPLAY",
    "GEMINI_CODING_LITE_DISPLAY", "GEMINI_DISPLAY", "GEMINI_IMAGE_DISPLAY",
    "GEMINI_TEXT_DISPLAY",
    "OPENAI_CODING_DISPLAY", "OPENAI_LATEST_DISPLAY", "OPENAI_LUNA_DISPLAY",
    "OPENAI_OSS_DISPLAY", "OPENAI_SIMPLE_DISPLAY", "OPENAI_TERA_DISPLAY",
    "LOCAL_LLM_DISPLAY", "VEO_DISPLAY",
})

#: Explicitly permitted by §P — a cost/normalisation table, a deny-list at the
#: egress point, capability metadata, or a posture flag. None of these names a
#: model the caller then routes to.
PERMITTED = frozenset({
    # cost / normalisation
    "MODEL_COST_PER_1M", "MODEL_COST_PER_SECOND", "VEO_COST_PER_SECOND",
    # capability metadata (§I.4) — a property of a model, not a choice of one
    "MODEL_MAX_OUTPUT_TOKENS", "CLI_ADDRESSABLE_MODEL_PREFIXES",
    "LOCAL_VISION_MODELS",
    # deny-list at the egress point (§I.5, kept deliberately)
    "BLOCKED_MODELS",
    # posture / availability switches, not model identity
    "ENABLE_CHAT_OPUS", "ENABLE_CLI_OPUS_48", "ENABLE_CLI_OPUS_5",
    "ENABLE_GPT56_LUNA", "ENABLE_GPT56_TERA", "ENABLE_OPUS",
    "ENABLE_RAW_OPENAI_API", "ENABLE_SONNET_5", "VEO_ENABLED",
    "LLM_PROVIDER", "PRIMARY_VISION_PROVIDER", "FALLBACK_VISION_PROVIDER",
    # the legacy fallback chain. No application module imports it — only
    # model_router walks it internally, and tests/routers/
    # test_chat_auto_tier_mapping.py:158 already ratchets that. Phase 10's.
    "CHAT_FALLBACK_CHAIN",
    # SDLC charter + stage→tier map: tier vocabulary, which is the point
    "SDLC_GROUNDING_CHARTER", "SDLC_STAGE_TIERS",
})

GUARDED = SKU_NAME | SKU_DISPLAY


# ── the four Phase 8 targets ────────────────────────────────────────────────
#
# Allowlisted with the count each contributes, so "the ratchet is mostly these
# four" is a stated fact rather than something a reader has to rediscover.

ALLOWLIST = {
    "models/model_router.py":
        "the legacy resolver itself — _HINT_MAP and the governance-off "
        "fallback ladder ARE the constants. Emptied by Phase 8/10.",
    "gateway.py":
        "list_oai_models() (GET /v1/models) is 8 unconditional env constants "
        "plus 6 flag-gated, registry never consulted. Deferred under D58 and "
        "removed with the variables in Phase 8.",
    "routers/messages_compat_router.py":
        "_list_models_compat_env_fallback — the degraded-mode CLI catalogue, "
        "reached only when the registry is unreadable (D63).",
    "AgentStudio/backend/app/api/generation.py":
        "_cli_reference_models_env_fallback — the twin of the above, kept "
        "separate on purpose because core/ may be unimportable there (D63).",
}

#: Measured 2026-09-30. May fall, never rise. Was 17 / 122; lowered when the
#: four Phase-8-prerequisite modules stopped naming vendors.
BASELINE_MODULES = 13
BASELINE_SITES = 116

#: Modules that release_checks._PHASE6_MIGRATED_MODULES calls migrated and
#: that still reach a vendor by its constant. Both remaining entries belong to
#: Phase 8 itself, for the reason recorded against each.
#:
#: This set may SHRINK and may not GROW. A module arriving here is a module
#: that was declared migrated while still choosing its own vendor.
MIGRATED_STILL_IMPORTING = {
    "agents/sdlc_governance/config.py":
        "CLAUDE_OPUS_MODEL / CLAUDE_OPUS_46_MODEL, added to a DENY-list when "
        "ENABLE_OPUS is off. §I assigns ENABLE_OPUS and the BLOCKED_MODELS "
        "narrowing to Phase 8; removing these early re-enables Opus for the "
        "governance fixer. Phase 8's own work, not a prerequisite for it.",
    "routers/chat_router.py":
        "GEMINI_IMAGE_MODEL is only a MODEL_COST_PER_1M key — a cost table, "
        "which §P permits. VEO_MODEL is a warn-once deprecated override whose "
        "primary path is already resolve_media_model(VIDEO_GENERATION). Both "
        "die with the variables in Phase 8.",
}


# ── the sweep ───────────────────────────────────────────────────────────────


def _tracked_py() -> list[str]:
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [
        f for f in out.split()
        # tests legitimately name models; llm_proxy is vendored (§Q.5, U2)
        if not f.startswith(("tests/", "services/llm_proxy/"))
        and f != "core/model_registry.py"
    ]


def _importers() -> dict[str, set[str]]:
    """{module: {constant, ...}} for every SKU-name/display import.

    AST rather than grep, for the reason check_tier_migration gives: the
    migrated modules now NAME the old constants in comments explaining what
    was wrong with them, and a text scan would report the explanation as the
    defect.
    """
    hits: dict[str, set[str]] = {}
    for rel in _tracked_py():
        try:
            # Parsing every tracked file surfaces warnings that belong to
            # those files rather than to this test — sandbox/
            # sandbox_image_builder.py:485,521,556 carry invalid '\s' escapes
            # in non-raw strings, which Python is progressively hardening from
            # DeprecationWarning toward an error. Real, pre-existing, and not
            # this phase's to fix; silenced here so the suite output stays
            # about the ratchet.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse((ROOT / rel).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if not (node.module or "").endswith("model_registry"):
                continue
            named = {a.name for a in node.names} & GUARDED
            if named:
                hits.setdefault(rel, set()).update(named)
    return hits


@pytest.fixture(scope="module")
def importers():
    return _importers()


# ── tests ───────────────────────────────────────────────────────────────────


def test_every_constant_is_classified():
    """A constant in no bucket is a constant outside the rule.

    This is the assertion that keeps the file honest. Without it, adding
    `CLAUDE_OPUS_6_MODEL` tomorrow would widen the coupling with every test
    below still green.
    """
    tree = ast.parse(REGISTRY.read_text(encoding="utf-8", errors="replace"))
    declared = {
        t.id
        for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
        for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
        if isinstance(t, ast.Name) and not t.id.startswith("_")
    }
    classified = SKU_NAME | SKU_DISPLAY | PERMITTED
    unclassified = sorted(declared - classified)
    assert not unclassified, (
        f"core/model_registry.py declares {unclassified} which this file does "
        f"not classify. Put each in SKU_NAME (it names a vendor model), "
        f"SKU_DISPLAY (it labels one), or PERMITTED (§P allows it: a cost "
        f"table, the egress deny-list, capability metadata, or a flag)."
    )
    stale = sorted(classified - declared)
    assert not stale, (
        f"{stale} are classified here but no longer declared in "
        f"core/model_registry.py — drop them, or the buckets drift into fiction"
    )


def test_the_importer_count_does_not_rise(importers):
    """The ratchet."""
    modules, sites = len(importers), sum(len(v) for v in importers.values())
    detail = "\n".join(
        f"    {len(v):3d}  {k}" for k, v in sorted(importers.items(),
                                                   key=lambda kv: -len(kv[1]))
    )
    assert modules <= BASELINE_MODULES and sites <= BASELINE_SITES, (
        f"{modules} modules / {sites} import sites reach a model by its .env "
        f"constant, above the recorded {BASELINE_MODULES} / {BASELINE_SITES}.\n"
        f"{detail}\n"
        f"Ask core.llm_provider_registry for the model, or ask for a Tier. "
        f"If the rise is legitimate, say why in the commit message and raise "
        f"the baseline deliberately — never as a drive-by."
    )
    # Ratchets only work if they are lowered. Phase 8 will drop this a long way.
    assert modules >= 1, "sweep found nothing — is the AST walk still working?"


def _phase6_migrated_modules() -> set[str]:
    """Read the tuple out of release_checks.py without executing it.

    AST rather than import: that file is a CI entry point with module-level
    work, and running it from inside the test suite is both slow and a way to
    inherit its warnings. Only the literal is wanted.
    """
    tree = ast.parse((ROOT / "scripts" / "ci" / "release_checks.py")
                     .read_text(encoding="utf-8", errors="replace"))
    for n in tree.body:
        if not isinstance(n, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "_PHASE6_MIGRATED_MODULES"
                   for t in n.targets):
            continue
        return {e.value for e in n.value.elts             # type: ignore[attr-defined]
                if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    raise AssertionError(
        "release_checks.py no longer declares _PHASE6_MIGRATED_MODULES as a "
        "module-level literal — this test can no longer read what it guards"
    )


def test_no_new_migrated_module_imports_a_sku_constant(importers):
    """The regression this file was written for.

    "Migrated" has meant "no tier string in a model_hint" and nothing more.
    Modules on that list that still choose their own vendor by constant are
    recorded in MIGRATED_STILL_IMPORTING with what each does. The rule is
    that the set may shrink and may not grow — fixing one is a production
    change and belongs in its own commit, but declaring a NEW module migrated
    while it still names a vendor is the mistake this catches.
    """
    migrated = _phase6_migrated_modules()
    offenders = {m: sorted(c) for m, c in importers.items()
                 if m in migrated and m not in ALLOWLIST}

    new = {m: c for m, c in offenders.items() if m not in MIGRATED_STILL_IMPORTING}
    assert not new, (
        "these modules are newly listed as migrated in release_checks."
        "_PHASE6_MIGRATED_MODULES while still importing a vendor SKU "
        "constant:\n"
        + "\n".join(f"    {m}: {c}" for m, c in sorted(new.items()))
        + "\ncheck_tier_migration cannot see this — it reads "
          '{"model_hint": "<tier>"} dict literals only. Ask '
          "core.llm_provider_registry for the model, or ask for a Tier."
    )

    fixed = sorted(set(MIGRATED_STILL_IMPORTING) - set(offenders))
    assert not fixed, (
        f"{fixed} no longer import a SKU constant — delete them from "
        f"MIGRATED_STILL_IMPORTING so the list keeps describing reality. "
        f"That is the ratchet tightening."
    )


def test_the_recorded_offenders_are_all_declared_migrated():
    """Counterpart to the above: an entry that is not on the migrated list is
    not evidence of anything, and would make the set look worse than it is."""
    migrated = _phase6_migrated_modules()
    stray = sorted(set(MIGRATED_STILL_IMPORTING) - migrated)
    assert not stray, (
        f"{stray} are recorded here as 'declared migrated but still importing' "
        f"yet are not in _PHASE6_MIGRATED_MODULES at all"
    )


def test_the_allowlist_names_only_real_files():
    """A stale allowlist entry silently widens the rule."""
    missing = [p for p in ALLOWLIST if not (ROOT / p).exists()]
    assert not missing, f"allowlisted but gone: {missing} — drop the entries"


def test_the_allowlist_entries_are_all_still_importers(importers):
    """The counterpart: an allowlisted module that has stopped importing
    constants should leave the allowlist, so the list keeps describing where
    the coupling actually is."""
    idle = [p for p in ALLOWLIST if p not in importers]
    assert not idle, (
        f"{idle} no longer import any SKU constant — remove them from "
        f"ALLOWLIST and lower BASELINE_MODULES accordingly. That is the "
        f"ratchet doing its job."
    )


def test_the_four_allowlisted_modules_are_most_of_the_count(importers):
    """Records the shape of the debt, so nobody reads the baseline as slack.

    If this stops holding, the tail has grown — which is the thing the ratchet
    is actually watching.
    """
    total = sum(len(v) for v in importers.values())
    allowed = sum(len(importers.get(p, ())) for p in ALLOWLIST)
    assert allowed / total > 0.75, (
        f"the four Phase 8 targets now hold only {allowed}/{total} of the "
        f"SKU-constant imports; the tail has grown and is where the real "
        f"coupling now lives"
    )
