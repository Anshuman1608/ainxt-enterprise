# SPDX-License-Identifier: MIT
"""§N.1 step 9 — the Chat Auto path, asserted over the source.

AST and source assertions rather than unit tests because neither entry point
imports under pytest: ``gateway.py`` is 16k lines with ~80 routers attached at
module scope, and ``routers/kb_ask_router.py`` reaches back into it for
``cache_key`` / ``mask_pii``. That makes this the weakest evidence base of any
step in the migration, which is stated here rather than implied — the
compensating evidence is tests/models/test_acl_candidate_filter.py for the
mechanism and the manual field checks for the routing.

What must hold:

  * both entry points compute a TIER, not just a hint string;
  * ``_fp_hint`` survives as a str, because §N.1 step 11's ainxt-api block
    reads it and four log lines print it;
  * the 116-line CHAT_FALLBACK_CHAIN walk is gone from gateway.py, while the
    constant itself survives for the legacy streaming chain;
  * ``hint_to_model_id`` no longer decides who may use what;
  * every chat dispatch passes ``acl_filter=``, so a fifth entry point cannot
    be added without one — which is how /kb/ask came to have no check.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

GATEWAY = ROOT / "gateway.py"
KB_ASK = ROOT / "routers" / "kb_ask_router.py"
CHAT_WORKER = ROOT / "workers" / "chat_worker.py"
TOOLS = ROOT / "agents" / "tools.py"

# The four in-process chat entry points (D34). The CLI and IDE dispatchers are
# deliberately absent — see test_the_cli_and_ide_paths_are_untouched.
CHAT_MODULES = (GATEWAY, KB_ASK, CHAT_WORKER, TOOLS)


@pytest.fixture(scope="module")
def src() -> dict:
    return {p.name: p.read_text(encoding="utf-8", errors="replace") for p in CHAT_MODULES}


@pytest.fixture(scope="module")
def trees() -> dict:
    return {p.name: ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            for p in CHAT_MODULES}


def _code_names(tree: ast.AST) -> set:
    """Every identifier the CODE uses — variables, attributes, imported names.

    Comments and docstrings are not in the AST, which is the whole reason to
    ask it instead of the text. Four of the assertions below are "this name is
    no longer used", and the modules deliberately explain the old name in a
    comment right where the new one replaced it — the fifth time in this
    migration that a substring test has flagged the documentation of a fix as
    the defect.
    """
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            names.add(n.id)
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
        elif isinstance(n, ast.alias):
            names.add(n.name.split(".")[-1])
            if n.asname:
                names.add(n.asname)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, ast.keyword) and n.arg:
            names.add(n.arg)
        elif isinstance(n, ast.ImportFrom) and n.module:
            names.add(n.module.split(".")[-1])
    return names


# ── 9a — both precedence points produce a tier ────────────────────────────


def test_gateway_computes_a_tier_beside_the_hint(src):
    s = src["gateway.py"]
    # The hint line is unchanged, on purpose (see the _fp_hint test below).
    assert '_fp_hint = "complex" if _is_voice_platform else (_model_hint or "medium")' in s
    # …and a tier is derived for each of the three Auto sources.
    assert "_fp_tier = _Tier.COMPLEX" in s, "voice turns must ask for complex"
    assert "_fp_tier = _Tier.MEDIUM" in s, "the flat Auto default must ask for medium"
    assert "_fp_tier = _chat_complexity_route(_cil_tier).get(\"tier\")" in s, \
        "the CIL verdict must be mapped, not passed through as a hint"
    assert "_fp_route = (_tier_request(_fp_tier, _fp_hint) if _fp_tier is not None" in s


def test_kb_ask_computes_a_tier_beside_the_hint(src):
    s = src["kb_ask_router.py"]
    assert '_fp_hint = "complex" if q.voice_platform else (_model_hint or "medium")' in s
    assert "_fp_route = _kb_route_for(q.voice_platform, _model_hint)" in s


def test_the_two_precedence_points_still_read_identically(src):
    """The bug that made /kb/ask's missing ACL invisible: this line was copied
    verbatim from gateway.py, so the two endpoints looked equivalent while one
    of them enforced nothing. If they ever need to differ, they should differ
    visibly."""
    marker = '(_model_hint or "medium")'
    assert marker in src["gateway.py"]
    assert marker in src["kb_ask_router.py"]


def test_a_users_pick_is_never_turned_into_a_tier(src):
    """§G — the user's explicit model choice is a hard requirement and is not
    governed. Both files must leave _fp_tier None when _model_hint is set."""
    assert "elif not _model_hint:" in src["gateway.py"]
    assert "if model_hint:\n        return {\"model_hint\": model_hint}" in src["kb_ask_router.py"]


# ── 9a — _fp_hint stays a string (D39, risk N9-g) ─────────────────────────


def test_fp_hint_is_still_assigned_a_string(trees):
    """It has five non-dispatch readers, so it cannot become a Tier."""
    tree = trees["gateway.py"]
    assigns = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_fp_hint" for t in n.targets)
    ]
    assert assigns, "_fp_hint is gone — gateway.py:8203 needs it as a str"
    for n in assigns:
        # Either the conditional string expression or the CIL word; never a
        # Tier attribute access.
        rendered = ast.unparse(n.value)
        assert "_Tier." not in rendered, (
            f"_fp_hint assigned a Tier at line {n.lineno}: {rendered} — "
            f"the ainxt-api block lowercases and string-matches it"
        )


def test_the_ainxt_api_block_still_reads_fp_hint_as_a_string(src):
    """§N.1 step 11's block. Byte-identical is the requirement; this asserts
    the one line that would break if _fp_hint changed type."""
    assert '_tier = (_fp_hint or _raw_model or "auto").lower().strip()' in src["gateway.py"]
    assert "_ainxt_model = _ainxt_model_for(_tier) or _ainxt_model_for(\"default\")" \
        in src["gateway.py"]


# ── 9b — the walk is gone, the constant is not ────────────────────────────


def test_gateway_no_longer_walks_chat_fallback_chain(trees):
    """§M.5 — the tier's own priority-ordered candidate list replaces it."""
    assert "CHAT_FALLBACK_CHAIN" not in _code_names(trees["gateway.py"])
    assert "_AUTO_FALLBACK_CHAIN" not in _code_names(trees["gateway.py"])


def test_the_legacy_fallback_walk_is_gone():
    """CHAT_FALLBACK_CHAIN's walk went with the legacy TIER_MINI chain (Rev 22 8.3)."""
    mr = (ROOT / "models" / "model_router.py").read_text(encoding="utf-8")
    assert "CHAT_FALLBACK_CHAIN" not in mr


def test_the_gov_local_only_double_meaning_is_gone(trees):
    """The variable that caused the 403: it was set both where a working local
    fallback HAD been found and where nothing had, and the 403 fired on it
    either way. Finding a model still returned 403."""
    assert "_gov_local_only" not in _code_names(trees["gateway.py"])


def test_the_403_now_fires_on_the_tier_being_fully_blocked(src):
    s = src["gateway.py"]
    assert "_fp_blocked = (_fp_tier is not None" in s
    assert "_fp_fully_blocked(_fp_tier, _fp_acl)" in s
    # Same error code and body — nothing reads it, but there is no reason to
    # change a user-facing string while fixing when it appears.
    assert '"code": "GOVERNANCE_NO_MODEL_AVAILABLE"' in s


# ── 9b/9d — hint_to_model_id no longer gates access ───────────────────────


def test_hint_to_model_id_no_longer_decides_who_may_use_what(trees):
    """The root cause of all three defects. It maps a hint to an .env
    CONSTANT, which stopped being "the model that runs" when the registry
    arrived."""
    used = _code_names(trees["gateway.py"])
    for name in ("hint_to_model_id", "_gov_hint_to_model", "_auto_hint_to_model"):
        assert name not in used, (
            f"gateway.py still resolves an access-control target via {name} — "
            f"use resolve_pick_to_model_id (explicit) or acl_filter (auto)"
        )


def test_the_explicit_pick_resolves_through_the_registry(src):
    assert "_gov_model = _gov_resolve_picked_model(_model_hint)" in src["gateway.py"]
    # The local fallback must trigger on '' as well as None, because the old
    # map returned BOTH and only None was handled.
    assert "if not _gov_model and q.local_model:" in src["gateway.py"]


# ── 9c — _is_local_route reads the model, not the hint (N9-a) ─────────────


@pytest.mark.parametrize("name", ["gateway.py", "kb_ask_router.py"])
def test_is_local_route_no_longer_tests_the_hint_string(trees, src, name):
    """The sharpest regression edge in step 9. While "simple" meant the
    in-house model this string test was true by construction; now that
    Tier.SIMPLE holds whatever an administrator assigned, it calls a paid
    Claude turn local — hoisting a local-only system message onto it and
    HIDING ITS BUDGET CHIP, so the user is billed and the bar stops moving.

    Asserted as "no Compare node tests _fp_hint for membership of a tuple
    naming a tier", so that the comment which explains the old test — sitting
    directly above the new one in both files — is not itself the failure.
    """
    offenders = []
    for n in ast.walk(trees[name]):
        if not isinstance(n, ast.Compare):
            continue
        if not (isinstance(n.left, ast.Name) and n.left.id == "_fp_hint"):
            continue
        for op, comp in zip(n.ops, n.comparators):
            if not isinstance(op, (ast.In, ast.NotIn)):
                continue
            if isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                values = {e.value for e in comp.elts
                          if isinstance(e, ast.Constant)}
                if values & {"local", "simple", "mini"}:
                    offenders.append((n.lineno, ast.unparse(n)))
    assert not offenders, (
        f"{name} still decides local-ness from the hint string: {offenders}"
    )
    assert "_mr_is_local_route(_local_model, _fp_route)" in src[name]


@pytest.mark.parametrize("name,expr", [
    ("gateway.py", "_is_deployment_local(_gmeta.get(\"model\"))"),
    ("kb_ask_router.py", "_is_dep_local(_gmeta.get(\"model\"))"),
])
def test_the_budget_chip_asks_about_the_model_that_actually_ran(src, name, expr):
    """Post-dispatch, the real model is knowable. "Was this billable" is the
    only question that matters here and guessing it wrong hides a charge."""
    assert expr in src[name]


# ── 9e/N9-c — every chat dispatch carries the filter ──────────────────────


def _router_stream_calls(tree):
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
            continue
        if n.func.attr in ("stream", "async_stream"):
            out.append(n)
    return out


def test_the_user_facing_chat_dispatches_pass_an_acl_filter(trees, src):
    """The shape assertion, not a list of line numbers — which is what makes
    it survive the next copy of /ask. Scoped to the three modules that serve
    a user's own answer; agents/tools.py streams on the repo-scoped agent
    path and is covered by its own test file.
    """
    for name, hint in (("gateway.py", "_fp_route"),
                       # Phase 6.6 added two more user-facing dispatches in
                       # this same file — the CLI relay and the
                       # OpenAI-compatible endpoint. Both had NO governance
                       # check of any kind before it, for the same reason
                       # /kb/ask had none: each was written by copying a path
                       # that predated the ACL.
                       ("gateway.py", "_cli_route"),
                       ("gateway.py", "_oai_route"),
                       ("kb_ask_router.py", "_fp_route"),
                       ("chat_worker.py", "_answer_route")):
        calls = [c for c in _router_stream_calls(trees[name])
                 if any(isinstance(kw.value, ast.Name) and kw.value.id == hint.lstrip("*")
                        or (kw.arg is None and ast.unparse(kw.value) == hint)
                        for kw in c.keywords)]
        assert calls, f"{name}: no stream call splats {hint}"
        for c in calls:
            assert any(kw.arg == "acl_filter" for kw in c.keywords), (
                f"{name}:{c.lineno} dispatches a user's chat turn without "
                f"acl_filter= — this is how /kb/ask ended up unenforced"
            )


def test_the_platform_internal_calls_do_not_pass_one(src):
    """§M.4, the other half. A department rule governs the user's own turns;
    it must not be able to break conversation summarisation or the docx
    structuring pass, which serve the platform rather than the user."""
    s = src["chat_worker.py"]
    i_sum = s.index('cached_summary = _mr_sum.generate(')
    window = s[i_sum:i_sum + 300]
    assert "acl_filter" not in window, \
        "the summariser must not be ACL-filtered (§M.4)"


# ── Scope (D34) ───────────────────────────────────────────────────────────


def test_the_cli_and_ide_paths_are_migrated(src):
    """INVERTED by Phase 6.6.

    Step 9 excluded both by name (D34) and this test pinned them in place so
    the exclusion was deliberate rather than forgotten. Phase 6.6 migrated
    them, so the same test now guards the other direction — the move step 10
    made on tests/models/test_registry_helper_consumers.py, and for the same
    reason: a test that asserts "not yet" is worth exactly as much as the
    plan to do it, and worth nothing once it is done.

    The detail worth keeping from the old docstring: the IDE block was a
    SECOND dispatcher. It read RoutingDecision.tier and hand-called
    _get_openai()/_get_claude()/_get_local() on legacy TIER_* constants,
    bypassing the governed attempt chain — which is why migrating it meant
    deleting a dispatcher rather than swapping a hint. The detail the old
    docstring did NOT know: under governance those branches never ran at
    all, because a resolved tier returns TIER_GOVERNED.
    """
    s = src["gateway.py"]
    assert "decision = _mr.route(_prompt, model_hint=_route_hint)" not in s, \
        "the IDE dispatcher's discarded-pin route call is back"
    assert "_cli_tier = _Tier.MINI" in s, "the CLI path lost its tier request"
    assert "_oai_route = _tier_request(_Tier.COMPLEX, \"claude\")" in s, \
        "the browser-agent pin is a vendor literal again"
    # The full assertions live in the two Phase 6.6 files; these three are
    # here so that a revert fails the STEP 9 file too, where the exclusion
    # was originally written down.


def test_ide_router_is_not_in_the_ratchet():
    """Its _hint_map is §E's inbound boundary shim, kept until Phase 9/10 and
    gated on the note_legacy_alias counter reading zero. Listing it would make
    CI demand a migration the plan defers."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts" / "ci"))
    import release_checks as rc
    assert "routers/ide_router.py" not in rc._PHASE6_MIGRATED_MODULES
    assert "gateway.py" in rc._PHASE6_MIGRATED_MODULES


# ── The CIL vocabulary gate is unchanged ──────────────────────────────────


def test_the_cil_membership_gate_still_guards_the_mapping(src):
    """_PV2_TIER_HINTS is the intersection of the CIL's vocabulary and the
    router's, computed at import. Step 9 maps what passes through it but must
    not widen it — tests/cil/test_intent_vocabulary.py asserts the same line
    from the CIL side."""
    s = src["gateway.py"]
    assert "_PV2_TIER_HINTS = set(_CIL_COMPLEXITY) & set(_MR_HINT_MAP)" in s
    assert "if _cil_tier in _PV2_TIER_HINTS:" in s
    assert "_cil_tier = _CIL_RETIRED_COMPLEXITY.get(_cil_tier, _cil_tier)" in s
