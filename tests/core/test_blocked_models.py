# SPDX-License-Identifier: MIT
"""D83 — the deny-list applies to selection, not only to admission.

``BLOCKED_MODELS`` was enforced on the request path and ignored on the
selection path: ``get_enabled_models()`` never consulted it, and every
catalogue, ``get_model()``, ``get_default_model_id()``, the tier resolver and
``model_router`` read through that one function. Three callers had patched it
locally; ~17 had not. Measured on a live deployment, the result was a
catalogue offering ``claude-opus-4-6`` and a handler answering
``400 Model 'claude-opus-4-6' is not available`` for it.

Two more evaded the list entirely, on a date suffix — it is compared with
``in``, and ``/discover-models`` syncs ``claude-sonnet-4-5-20250929`` where
the list says ``claude-sonnet-4-5``.

**These tests stub ``_load_from_db``, not ``get_enabled_models``.** The filter
lives inside the latter, so a fixture that replaces it — as
``tests/routers/test_catalogue_no_tier_names.py``'s autouse ``_registry``
fixture does — removes the thing under test and passes no matter what. That
is the failure mode ``test_routing_wiring.py`` carried for four revisions.
Do not "simplify" these fixtures up a layer.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import core.llm_provider_registry as reg
import core.model_registry as mreg

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The live deny-list entries this file reasons about, so a change to the real
#: set cannot quietly make a case vacuous.
_RETIRED = "claude-opus-4-6"
_RETIRED_UNDATED = "claude-sonnet-4-5"


@pytest.fixture
def deny(monkeypatch):
    """Replace BLOCKED_MODELS wholesale, so these cases do not drift with the
    deployment's env flags."""
    def _set(*ids):
        monkeypatch.setattr(mreg, "BLOCKED_MODELS", set(ids))
    return _set


# ── the matcher ─────────────────────────────────────────────────────────────


def test_an_exact_id_is_blocked(deny):
    deny(_RETIRED)
    assert mreg.is_blocked_model(_RETIRED)


def test_an_unrelated_id_is_not(deny):
    deny(_RETIRED)
    assert not mreg.is_blocked_model("claude-sonnet-5-5")


@pytest.mark.parametrize("dated,undated", [
    ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),
    ("claude-opus-4-5-20251101", "claude-opus-4-5"),
])
def test_a_dated_snapshot_of_a_retired_model_is_blocked(deny, dated, undated):
    """The defect: both of these are enabled in the live registry under the
    display names "Claude Sonnet 4.5" and "Claude Opus 4.5", and both were
    being advertised and dispatched while the list named them retired."""
    deny(undated)
    assert mreg.is_blocked_model(dated)


def test_a_dated_snapshot_of_a_model_that_is_NOT_retired_survives(deny):
    """The false-positive case, and the reason the rule strips rather than
    prefix-matches. claude-haiku-4-5-20251001 is assigned to two tiers on the
    live deployment; a rule that caught it would take them both out."""
    deny(_RETIRED_UNDATED, _RETIRED)
    assert not mreg.is_blocked_model("claude-haiku-4-5-20251001")


@pytest.mark.parametrize("mid", [
    "claude-sonnet-4-5-preview",     # a suffix, but not digits
    "claude-sonnet-4-5-2025092",     # seven
    "claude-sonnet-4-5-202509291",   # nine
    "claude-sonnet-4-5-2025-09-29",  # dashed
    "claude-sonnet-4-520250929",     # no separator
])
def test_only_a_trailing_eight_digit_group_is_stripped(deny, mid):
    deny(_RETIRED_UNDATED)
    assert not mreg.is_blocked_model(mid)


def test_a_bare_eight_digit_suffix_does_not_block_an_unlisted_base(deny):
    """The rule can only ever fire when the STRIPPED id is itself an
    operator-authored entry, so an unrelated id ending in eight digits is
    unaffected. This is what bounds N17-c."""
    deny(_RETIRED)
    assert not mreg.is_blocked_model("vendor-model-20250101")


def test_the_id_is_not_case_folded(deny):
    deny(_RETIRED)
    assert not mreg.is_blocked_model(_RETIRED.upper())


def test_surrounding_whitespace_is_ignored(deny):
    deny(_RETIRED)
    assert mreg.is_blocked_model(f"  {_RETIRED}  ")


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_empty_id_answers_exactly_what_plain_membership_answered(deny, empty):
    """NOT a correctness improvement, deliberately.

    "" IS in BLOCKED_MODELS on an admin-only install — the blank SKU constants
    get added under their flags — and gateway_claude.py:118 records what
    changing that costs: the check ran at CLASS-DEFINITION time, so "" matching
    made the module unimportable and EVERY Claude model failed, not one. The
    matcher must reproduce today's answer in both directions.
    """
    deny(_RETIRED)
    assert not mreg.is_blocked_model(empty)

    deny(_RETIRED, "")
    assert mreg.is_blocked_model(empty)


# ── the selection path ──────────────────────────────────────────────────────


def _row(model_id: str, **kw) -> dict:
    """Shaped like a _load_from_db() row."""
    row = {
        "id": f"uuid-{model_id}",
        "model_id": model_id,
        "display_name": model_id.title(),
        "capabilities": {},
        "is_default": False,
        "sort_order": 0,
        "provider_id": "p1",
        "provider_slug": "vendor",
        "provider_name": "VendorCo",
        "family": "anthropic",
        "base_url": None,
    }
    row.update(kw)
    return row


_ROWS = [
    _row("good-1"),
    _row(_RETIRED),                        # exact deny-list hit
    _row("good-2"),
    _row("claude-sonnet-4-5-20250929"),    # dated hit
    _row("claude-haiku-4-5-20251001"),     # dated near-miss, must survive
]


@pytest.fixture
def db_rows(monkeypatch, deny):
    """Stub BELOW the filter. See the module docstring."""
    deny(_RETIRED, _RETIRED_UNDATED)
    written: list = []
    monkeypatch.setattr(reg, "_read_cache", lambda: None)
    monkeypatch.setattr(reg, "_load_from_db", lambda: [dict(r) for r in _ROWS])
    monkeypatch.setattr(reg, "_write_cache", lambda models: written.append(models))
    return written


def test_get_enabled_models_drops_the_blocked_rows(db_rows):
    ids = [m["model_id"] for m in reg.get_enabled_models()]
    assert ids == ["good-1", "good-2", "claude-haiku-4-5-20251001"]


def test_the_surviving_order_is_the_registrys_own(db_rows):
    """sort_order then created_at is a documented convention get_model() and
    db/migrate.py both rely on; filtering must not reshuffle it."""
    ids = [m["model_id"] for m in reg.get_enabled_models()]
    assert ids == [i for i in (r["model_id"] for r in _ROWS) if i in ids]


def test_the_shared_cache_is_not_poisoned(db_rows):
    """D87's ordering requirement, and the hardest revert to catch.

    BLOCKED_MODELS is built from env flags PER PROCESS; the KV is shared by
    every gateway worker. Filter before the write and one worker's flags decide
    another worker's catalogue — which looks perfectly correct in any
    single-process test, including every other test in this file.
    """
    reg.get_enabled_models()
    assert db_rows, "nothing was written to the cache"
    cached = [m["model_id"] for m in db_rows[-1]]
    assert cached == [r["model_id"] for r in _ROWS], (
        "the deny-list was applied before _write_cache(); the shared cache now "
        "carries one process's env flags")


def test_the_channel_filter_still_applies(monkeypatch, deny):
    deny(_RETIRED)
    rows = [
        _row("cli-only", capabilities={"channels": ["cli"]}),
        _row("everywhere"),
        _row(_RETIRED, capabilities={"channels": ["cli"]}),
    ]
    monkeypatch.setattr(reg, "_read_cache", lambda: [dict(r) for r in rows])

    assert [m["model_id"] for m in reg.get_enabled_models(channel="cli")] == \
        ["cli-only", "everywhere"]
    assert [m["model_id"] for m in reg.get_enabled_models(channel="api")] == \
        ["everywhere"]


def test_get_model_inherits_the_filter(db_rows):
    """The one that closes D81's loop: _oai_explicit_model_id() resolves
    through get_model(), so without this a blocked id would still be handed to
    the router untranslated."""
    assert reg.get_model("good-1") is not None
    assert reg.get_model(_RETIRED) is None
    assert reg.get_model("claude-sonnet-4-5-20250929") is None


def test_get_cli_style_models_inherits_the_filter(db_rows):
    assert [m["id"] for m in reg.get_cli_style_models()] == \
        ["good-1", "good-2", "claude-haiku-4-5-20251001"]


def test_get_default_model_id_cannot_return_a_blocked_model(monkeypatch, deny):
    """is_default is an admin flag on the row; nothing stopped it landing on a
    retired one, and every caller that asks "what should I use by default"
    would then have got an id the request path refuses."""
    deny(_RETIRED)
    rows = [_row(_RETIRED, is_default=True), _row("good-1")]
    monkeypatch.setattr(reg, "_read_cache", lambda: [dict(r) for r in rows])
    assert reg.get_default_model_id() == "good-1"


def test_blocked_enabled_models_reports_exactly_the_complement(db_rows):
    withdrawn = [m["model_id"] for m in reg.blocked_enabled_models()]
    assert withdrawn == [_RETIRED, "claude-sonnet-4-5-20250929"]
    kept = {m["model_id"] for m in reg.get_enabled_models()}
    assert not (kept & set(withdrawn))
    assert len(kept) + len(withdrawn) == len(_ROWS)


# ── one matcher, not five ───────────────────────────────────────────────────

#: The one module that may still test membership directly.
#:
#: gateway_claude.py's is a CLASS-DEFINITION-time check on the CLAUDE_MODEL env
#: constant, not on a caller's pick. Widening it means a deployment that pinned
#: a dated retired id fails to import the module at all, taking every Claude
#: model with it — the failure its own comment at :118 describes. Phase 8
#: deletes the constant.
ALLOWED_DIRECT = {
    "gateway_claude.py",
}

#: services/llm_proxy/ is a VENDORED service with its own
#: core/model_registry.py and its own BLOCKED_MODELS; it cannot import this
#: one. U2 reduced its scope to "holds defaults, not routing" and Phase 8
#: deletes its constants rather than re-architecting it.
SKIP_SUBTREES = ("services/llm_proxy/",)

SWEEP = ("core", "models", "routers", "services", "agents", "store", "db",
         "gateway.py", "gateway_claude.py", "gateway_openai.py",
         "gateway_local_llm.py")


def _membership_tests(path: pathlib.Path) -> list[int]:
    """Lines doing `x in BLOCKED_MODELS` / `x not in BLOCKED_MODELS`."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            continue
        for comp in node.comparators:
            # `in BLOCKED_MODELS`, `in set(BLOCKED_MODELS)` and `in blocked`
            # where `blocked` was bound from it — db/migrate.py used the last
            # shape, which is why this matches the local alias too.
            for sub in ast.walk(comp):
                if isinstance(sub, ast.Name) and (
                        sub.id.endswith("BLOCKED_MODELS") or sub.id == "blocked"):
                    hits.append(node.lineno)
    return hits


def test_the_deny_list_is_consulted_through_one_matcher():
    """Four independent copies of the membership test is how the selection and
    admission paths came to disagree. Shrink-only: a fifth may not appear."""
    offenders: dict[str, list[int]] = {}
    for top in SWEEP:
        p = ROOT / top
        files = sorted(p.rglob("*.py")) if p.is_dir() else ([p] if p.exists() else [])
        for f in files:
            rel = f.relative_to(ROOT).as_posix()
            if rel == "core/model_registry.py" or rel in ALLOWED_DIRECT:
                continue
            if any(rel.startswith(s) for s in SKIP_SUBTREES):
                continue
            lines = _membership_tests(f)
            if lines:
                offenders[rel] = lines
    assert offenders == {}, (
        f"these test BLOCKED_MODELS directly instead of calling "
        f"core.model_registry.is_blocked_model(): {offenders}")


@pytest.mark.parametrize("rel", sorted(ALLOWED_DIRECT))
def test_every_named_survivor_really_is_one(rel):
    """A named exception that no longer applies is a baseline that stopped
    measuring. If one of these is cleaned up, delete its entry."""
    assert _membership_tests(ROOT / rel), (
        f"{rel} is on ALLOWED_DIRECT but no longer tests BLOCKED_MODELS "
        f"directly — remove it from the list")


# ── the operator signal (D89) ───────────────────────────────────────────────


def _gateway_fn(name: str):
    tree = ast.parse((ROOT / "gateway.py").read_text(encoding="utf-8",
                                                     errors="replace"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"gateway.py has no {name}()")


def test_the_boot_audit_is_actually_wired():
    """An audit that is defined and never called is the shape this whole
    change exists to stop: something that looks enforced and is not. The
    blocked rows now vanish silently from every picker, and this log line is
    the only thing that tells the operator why."""
    called = {getattr(n.func, "id", "") for n in ast.walk(_gateway_fn("startup"))
              if isinstance(n, ast.Call)}
    assert "_audit_blocked_but_enabled" in called, (
        "gateway.py defines the deny-list audit but startup() never calls it")


def test_the_boot_audit_reads_the_registry_not_its_own_list():
    """Re-deriving the blocked set here would be a fifth copy of the rule and
    could disagree with the filter it is explaining."""
    fn = _gateway_fn("_audit_blocked_but_enabled")
    imported = {a.name for n in ast.walk(fn) if isinstance(n, ast.ImportFrom)
                for a in n.names}
    assert "blocked_enabled_models" in imported
    assert not (imported & {"BLOCKED_MODELS", "is_blocked_model"}), (
        "the audit re-derives the blocked set instead of asking the registry "
        "for the rows it actually withdrew")


def test_doctor_asks_the_gateway_rather_than_the_database():
    """BLOCKED_MODELS is built from env flags at import, so no SQL can see
    it — a run_sql check here would report every deployment clean."""
    doctor = (ROOT / "doctor.sh").read_text(encoding="utf-8", errors="replace")
    cmd = [ln for ln in doctor.splitlines() if "blocked_enabled_models" in ln]
    assert len(cmd) == 1, "doctor.sh has no deny-list check"
    assert "docker exec ainxt-gateway" in cmd[0]
    assert "run_sql" not in cmd[0]
    # All three outcomes, or an operator learns nothing in two of them.
    lines = doctor.splitlines()
    titles = [ln for ln in lines if "llm deny-list clear" in ln]
    assert len(titles) == 3, f"expected skip/warn/pass, found {len(titles)}"
    assert sorted(ln.split()[0] for ln in titles) == ["pass", "skip", "warno"]

    # And the branches must be decided by the gateway's answer. A check whose
    # conditions no longer read it still prints all three titles.
    i = next(n for n, ln in enumerate(lines) if "blocked_enabled_models" in ln)
    guards = [ln for ln in lines[i:i + 4] if ln.lstrip().startswith(("if ", "elif "))]
    assert len(guards) == 2 and all("blocked_raw" in g for g in guards), (
        f"the deny-list branches ignore the gateway's answer: {guards}")
