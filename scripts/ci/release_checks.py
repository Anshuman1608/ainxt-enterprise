#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
Release-readiness checks for AiNxt Enterprise.

Every check here exists because the corresponding defect actually shipped. Each
one is cheap, needs no services, and runs the same way locally as it does in CI:

    python scripts/ci/release_checks.py                  # run everything
    python scripts/ci/release_checks.py --list           # what is available
    python scripts/ci/release_checks.py readme-links     # just these
    python scripts/ci/release_checks.py --skip secrets   # all but these
    python scripts/ci/release_checks.py --warn-only      # report, never fail

Configuration — scripts/ci/checks.toml, or environment variables, or flags.
Precedence: flags > environment > config file > defaults.

    RELEASE_CHECKS_SKIP=secrets,readme-links
    RELEASE_CHECKS_WARN_ONLY=1
    RELEASE_CHECKS_INTERNAL_PATTERN=npci|acme-corp
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = Path(__file__).resolve().parent / "checks.toml"

DEFAULTS = {
    # Substring/regex identifying your organisation's internal infrastructure.
    # Set this to your own org so the externalization checks are meaningful.
    "internal_pattern": "npci",
    # Files whose internal-reference hits are tolerated (docs describing history,
    # and the generated single-page doc bundle).
    "internal_allowlist": "documentation.html,docs/,compliance/,scripts/ci/,.github/",
    # Documents whose relative links must all resolve.
    "linked_docs": "README.md,docs/GETTING_STARTED.md,compliance/README.md,SUPPORT.md",
    "shell_scripts": "install.sh,stop-local.sh,compliance/generate-sbom.sh",
    # The env template(s) that must stay clean. .env.example is the only one:
    # the unreferenced 1027-variable `env.example` dump that used to sit beside
    # it was deleted (DOC-008), so there is nothing left to exclude.
    "env_files": ".env.example",
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            import tomllib
            with CONFIG_PATH.open("rb") as fh:
                for k, v in (tomllib.load(fh).get("checks") or {}).items():
                    cfg[k] = ",".join(v) if isinstance(v, list) else str(v)
        except Exception as exc:                      # never fail on config
            print(f"  ! could not read {CONFIG_PATH.name}: {exc}")
    for k in list(cfg):
        env = os.getenv("RELEASE_CHECKS_" + k.upper())
        if env:
            cfg[k] = env
    return cfg


def csv(cfg: dict, key: str) -> list[str]:
    return [x.strip() for x in cfg.get(key, "").split(",") if x.strip()]


def tracked(*globs: str) -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", *globs], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        return [l for l in out.splitlines() if l]
    except Exception:
        return []


# ── Checks ──────────────────────────────────────────────────────────────────
# Each returns a list of failure strings. Empty list means pass.

def check_docs_tracked(cfg) -> list[str]:
    """docs/ must be committed. A bare `docs/` in .gitignore once excluded all 590
    files, so the published repo would have carried no documentation at all."""
    n = len(tracked("docs/"))
    return [] if n else ["docs/ contains no tracked files — is it gitignored again?"]


def check_readme_links(cfg) -> list[str]:
    """Every relative link in the docs a newcomer follows must resolve. README linked
    docs/GETTING_STARTED.md and compliance/ for months; neither existed."""
    bad = []
    for doc in csv(cfg, "linked_docs"):
        p = ROOT / doc
        if not p.exists():
            bad.append(f"{doc}: listed as a linked doc but missing")
            continue
        for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", p.read_text(encoding="utf-8", errors="replace")):
            target = m.group(1).split("#")[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            # ../../<something> is GitHub's repo-relative shorthand (e.g.
            # ../../security/advisories/new). It resolves on github.com, not on disk.
            if target.startswith("../../"):
                continue
            if not (p.parent / target).exists():
                bad.append(f"{doc}: broken link -> {target}")
    return bad


def check_env_duplicates(cfg) -> list[str]:
    """A key defined twice in .env.example silently wins from its last occurrence,
    which is how OLLAMA_URL kept reverting to localhost inside the container."""
    bad = []
    for name in csv(cfg, "env_files"):
        p = ROOT / name
        if not p.exists():
            continue
        seen: dict[str, int] = {}
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^([A-Z_][A-Z0-9_]*)=", line)
            if m:
                seen[m.group(1)] = seen.get(m.group(1), 0) + 1
        for k, n in sorted(seen.items()):
            if n > 1:
                bad.append(f"{name}: {k} defined {n} times")
    return bad


def check_orphan_migrations(cfg) -> list[str]:
    """Every _part_* function in db/migrate.py must be called. Eight were defined and
    never invoked, so a month of migrations never ran on any fresh install."""
    p = ROOT / "db" / "migrate.py"
    if not p.exists():
        return ["db/migrate.py not found"]
    src = p.read_text(encoding="utf-8", errors="replace")
    defined = set(re.findall(r"^def (_part_[a-z0-9_]+)\(", src, re.M))
    called = set(re.findall(r"(?<!def )\b(_part_[a-z0-9_]+)\(\)", src))
    return [f"db/migrate.py: {n}() is defined but never called" for n in sorted(defined - called)]


def check_lockfile_registry(cfg) -> list[str]:
    """No lockfile may resolve dependencies through internal infrastructure. All 489
    entries of the ABStudio lockfile once pointed at an internal Nexus, which left
    the frontend unbuildable outside the corporate network."""
    pat = re.compile(cfg["internal_pattern"], re.I)
    bad = []
    for f in tracked("*package-lock.json", "*yarn.lock", "*pnpm-lock.yaml", "*Cargo.lock", "*poetry.lock"):
        try:
            hits = sum(1 for line in (ROOT / f).read_text(encoding="utf-8", errors="replace").splitlines()
                       if pat.search(line))
        except OSError:
            continue
        if hits:
            bad.append(f"{f}: {hits} line(s) reference internal infrastructure")
    return bad


def check_internal_urls(cfg) -> list[str]:
    """No source file may hardcode an internal hostname. An API-key expiry email
    mailed every external user a link to an internal host they cannot reach."""
    pat = re.compile(r"https?://[A-Za-z0-9.-]*(?:" + cfg["internal_pattern"] + r")[A-Za-z0-9.-]*", re.I)
    allow = tuple(csv(cfg, "internal_allowlist"))
    bad = []
    for f in tracked("*.py", "*.js", "*.jsx", "*.ts", "*.tsx", "*.yml", "*.yaml", "*.sh"):
        if any(f.startswith(a) or f == a for a in allow):
            continue
        try:
            text = (ROOT / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if s.startswith(("#", "//", "*")):        # comments are documentation
                continue
            m = pat.search(line)
            if m:
                bad.append(f"{f}:{i}: hardcoded internal URL {m.group(0)}")
    return bad


def check_secrets(cfg) -> list[str]:
    """No tracked file may carry a populated credential. Placeholders are fine."""
    placeholder = re.compile(
        r"^\s*$|^(\"\")|<|your|change|xxx|placeholder|replace|example|dummy|"
        r"not-needed|sk-\.\.\.|\$\{|sk-local|auto|none|true|false|[0-9.]+$",
        re.I)
    keyish = re.compile(r"^([A-Z_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z_]*)=(.*)$")
    bad = []
    for f in csv(cfg, "env_files") + tracked("*.yml", "*.yaml", "*.toml", "*.ini"):
        try:
            text = (ROOT / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            m = keyish.match(line.strip())
            if not m:
                continue
            value = re.sub(r"\s+#.*$", "", m.group(2)).strip().strip('"').strip("'")
            if value and not placeholder.match(value):
                bad.append(f"{f}:{i}: {m.group(1)} looks populated — use a placeholder")
    return bad


def check_python_syntax(cfg) -> list[str]:
    """Cheapest possible guard against a broken commit."""
    bad = []
    import warnings
    for f in tracked("*.py"):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                ast.parse((ROOT / f).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            bad.append(f"{f}:{exc.lineno}: {exc.msg}")
        except OSError:
            pass
    return bad


def check_model_hint_coverage(cfg) -> list[str]:
    """Every model id the API advertises must map to a routing hint. Four ids did
    not, so a caller asking for one model was silently served another — including
    claude-haiku-4-5 being answered by the far costlier Sonnet."""
    g = ROOT / "gateway.py"
    if not g.exists():
        return []
    src = g.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"_OAI_MODEL_MAP\s*=\s*\{(.*?)\n\}", src, re.S)
    if not m:
        return ["gateway.py: _OAI_MODEL_MAP not found"]
    prefixes = [k for k, _ in re.findall(r'^\s*"([^"]+)"\s*:\s*([^,\n]+),', m.group(1), re.M)]
    fn = re.search(r"def list_oai_models\(.*?\n(?=\n@|\ndef |\Z)", src, re.S)
    body = fn.group(0) if fn else ""

    # The ids advertised in that body are almost all import aliases (e.g.
    # `from core.model_registry import CLAUDE_PRIMARY_MODEL as _CLAUDE_PRIMARY`)
    # whose value is an env var that is blank until an operator configures it —
    # there is no literal string to test against `prefixes` in a fresh checkout.
    # Coverage is therefore structural, not value-based: every alias id this
    # function advertises must have its own `_OAI_MODEL_MAP[_ALIAS] = "..."`
    # assignment (right after the dict literal in gateway.py), so whatever the
    # alias resolves to at runtime is a key in the map by construction. That
    # block is what previously went missing for gpt-5.6-terra/-luna.
    covered_aliases = set(re.findall(r"_OAI_MODEL_MAP\[(_[A-Z][A-Z_0-9]*)\]\s*=", src))
    advertised_aliases = set(re.findall(r'\{"id":\s*(_[A-Z][A-Z_0-9]*)', body))
    literal_ids = set(re.findall(r'\{"id":\s*"([a-z0-9.\-]+)"', body))
    if not advertised_aliases and not literal_ids:
        return ["gateway.py: could not resolve any advertised model id — "
                "this check would pass trivially, so failing instead"]

    bad = [f"gateway.py: advertised id {alias} has no _OAI_MODEL_MAP[{alias}] = ... entry"
           for alias in sorted(advertised_aliases - covered_aliases)]
    for mid in sorted(literal_ids - {"auto", "default", "local", "inhouse", "in-house"}):
        if not any(mid.lower().startswith(p) for p in prefixes):
            bad.append(f"gateway.py: advertised model id {mid!r} has no _OAI_MODEL_MAP entry")
    return bad


def check_docs_panel_coverage(cfg) -> list[str]:
    """The in-app Docs panel must describe every sidebar feature, and nothing else.

    It had drifted to 9 of 27 features while still documenting four screens that
    had been removed, so a user clicking them read about something that no longer
    existed. Comparing the two files is cheap; noticing by eye evidently is not.
    """
    sb = ROOT / "ai-ui" / "src" / "components" / "Sidebar.jsx"
    dp = ROOT / "ai-ui" / "src" / "components" / "DocsPanel.jsx"
    if not sb.exists() or not dp.exists():
        return []
    sb_src = sb.read_text(encoding="utf-8", errors="replace")
    dp_src = dp.read_text(encoding="utf-8", errors="replace")

    # Entries can be commented out — `agents` and `skill-proposals` are, while
    # remaining routed and reachable by URL. Matching the commented lines too
    # would make this check blind to exactly the drift it exists to catch, so
    # strip line comments before parsing.
    sb_live = "\n".join(
        "" if ln.lstrip().startswith("//") else ln
        for ln in sb_src.splitlines()
    )
    sidebar = [
        (m.group(1), m.group(2), m.group(3))
        for m in re.finditer(
            r'\{\s*view:\s*"([^"]+)"\s*,\s*icon:\s*\w+\s*,\s*label:\s*"([^"]+)"([^}]*)\}',
            sb_live,
        )
    ]
    # Views that are routed in App.jsx but deliberately absent from the sidebar.
    # A card for one of these is fine — the screen exists and can be reached —
    # but a card for something neither listed nor routed is documenting nothing.
    app = ROOT / "ai-ui" / "src" / "App.jsx"
    routed = set()
    if app.exists():
        routed = set(re.findall(r'path="/([a-z0-9\-]+)"',
                                app.read_text(encoding="utf-8", errors="replace")))

    # "docs" is the panel itself; it does not document itself.
    views = [v for v, _, _ in sidebar if v != "docs"]
    labels = {v: l for v, l, _ in sidebar}
    flags = {
        v: ("desktopOnly" in r and "true" in r, "beta" in r)
        for v, _, r in sidebar
    }

    cards = re.findall(r'^\s{4}id: "([^"]+)",', dp_src, re.M)
    if not views or not cards:
        return ["DocsPanel/Sidebar: could not parse either file — this check "
                "would pass trivially, so failing instead"]

    bad = []
    for c in cards:
        if c not in views and c not in routed:
            bad.append(f"DocsPanel.jsx: card {c!r} is neither in the Sidebar nor routed "
                       f"in App.jsx — it documents a screen that does not exist")
    for v in views:
        if v not in cards:
            bad.append(f"DocsPanel.jsx: sidebar feature {v!r} ({labels.get(v)}) is undocumented")

    # Labels and the desktop-only / beta badges must agree, or the panel tells a
    # user a feature works in the browser when it does not.
    card_labels = dict(re.findall(
        r'id: "([^"]+)",\s*\n\s*icon: \w+,\s*\n\s*label: "([^"]+)"', dp_src))
    for v, lbl in card_labels.items():
        if v in labels and labels[v] != lbl:
            bad.append(f"DocsPanel.jsx: {v!r} labelled {lbl!r}, sidebar says {labels[v]!r}")

    for block in re.split(r"\n  \{\n", dp_src):
        m = re.search(r'id: "([^"]+)"', block)
        if not m:
            continue
        v = m.group(1)
        if v not in flags:
            continue
        got = ("desktopOnly: true" in block, "beta: true" in block)
        if got != flags[v]:
            bad.append(f"DocsPanel.jsx: {v!r} flags {got} disagree with Sidebar {flags[v]} "
                       f"(desktopOnly, beta)")

    # A duplicated card renders twice and shadows whichever copy is stale. This
    # check used to test coverage only, so `llm-provider-config` shipped twice
    # with two different descriptions.
    import collections
    all_ids = re.findall(r'^\s*id:\s*"([a-z0-9-]+)"', dp_src, re.M)
    for card_id, n in sorted(collections.Counter(all_ids).items()):
        if n > 1:
            bad.append(f"DocsPanel.jsx: card {card_id!r} is defined {n} times — remove the duplicate")

    return bad


# The README marks desktop-only features with this glyph.
_DESKTOP_MARKER = "✅"


def check_readme_feature_table(cfg) -> list[str]:
    """The README's feature table must match the sidebar it claims to describe.

    The table carries a desktop-only marker per screen, and must list every
    screen and no others. Both go stale silently: a reader trusts "works in a
    browser" and finds the feature missing, or reads about a screen that was
    removed. Cheap to compare, so compare it.

    The table used to carry the access level too; that column was dropped as
    noise for a reader who cannot change it anyway, so it is no longer checked.
    """
    sb = ROOT / "ai-ui" / "src" / "components" / "Sidebar.jsx"
    rm = ROOT / "README.md"
    if not sb.exists() or not rm.exists():
        return []

    sb_src = sb.read_text(encoding="utf-8", errors="replace")
    # Commented-out entries are not part of the product surface.
    live = "\n".join(
        "" if ln.lstrip().startswith("//") else ln for ln in sb_src.splitlines()
    )
    truth = {}
    for m in re.finditer(
        r'\{\s*view:\s*"([^"]+)"\s*,\s*icon:\s*\w+\s*,\s*label:\s*"([^"]+)"([^}]*)\}',
        live,
    ):
        _, label, rest = m.groups()
        truth[label] = "desktopOnly" in rest and "true" in rest

    rm_src = rm.read_text(encoding="utf-8", errors="replace")
    # Bound the parse to this one table. A pattern loose enough to match its
    # rows also matches the Features and HSM tables elsewhere in the README, and
    # then reports every row of those as an unknown screen.
    rm_lines = rm_src.split("\n")
    try:
        hdr = next(i for i, l in enumerate(rm_lines)
                   if l.startswith("| Screen | What it is for |"))
    except StopIteration:
        return ["README.md: the feature table header was not found — this check "
                "would pass trivially, so failing instead"]
    rows = []
    for l in rm_lines[hdr + 2:]:
        if not l.startswith("|"):
            break
        m = re.match(r"^\| \*\*([^*]+)\*\* \|[^|]*\|[^|]*\|([^|]*)\|", l)
        if m:
            rows.append((m.group(1), m.group(2)))
    if not rows or not truth:
        return ["README/Sidebar: could not parse the feature table or the sidebar — "
                "this check would pass trivially, so failing instead"]

    bad = []
    seen = set()
    for label, desktop_col in rows:
        label = label.strip()
        seen.add(label)
        if label not in truth:
            bad.append(f"README.md: feature table lists {label!r}, which is not in the Sidebar")
            continue
        want_desktop = truth[label]
        has_marker = _DESKTOP_MARKER in desktop_col
        if has_marker != want_desktop:
            shown = "yes" if has_marker else "no"
            expected = "yes" if want_desktop else "no"
            bad.append(f"README.md: {label!r} desktop-only marker disagrees with Sidebar "
                       f"(README={shown}, Sidebar={expected})")
    for label in truth:
        if label not in seen:
            bad.append(f"README.md: sidebar feature {label!r} is missing from the feature table")
    return bad


# Vendor model IDs that may legitimately appear as literals, and only here:
# the registry is the single place that maps an env var to a default model.
_MODEL_LITERAL_ALLOWED = {
    "core/model_registry.py",
    "services/llm_proxy/core/model_registry.py",
    "scripts/ci/release_checks.py",
}

# Recorded count of vendor model literals outside the registry, measured at the
# time this check was written. See check_model_literals for why it is a ratchet
# rather than zero.
_MODEL_LITERAL_BASELINE = 198

_MODEL_LITERAL_RE = re.compile(
    r'"(claude-[a-z0-9.\-]+'
    r'|gpt-[0-9][a-z0-9.\-]*'
    r'|gemini-[0-9][a-z0-9.\-]*'
    r'|o[0-9]-[a-z\-]+'
    r'|dall-e-[0-9]'
    r'|veo-[a-z0-9.\-]+)"',
    re.I,
)


def check_model_literals(cfg) -> list[str]:
    """No NEW hardcoded vendor model ID outside core/model_registry.py.

    The platform is meant to be provider-agnostic: which model serves which tier
    is configuration, so that one codebase serves a public deployment and a
    self-hosted one without either inheriting the other's choices. A model ID
    written into a module defeats that — it cannot be overridden by `.env`, and
    an operator reading the config file cannot tell it is being ignored.

    198 such literals already exist across 25 modules. Failing on all of them
    would make this check permanently red and therefore ignored, so it is a
    ratchet: the count may fall, never rise. Lower _MODEL_LITERAL_BASELINE as
    modules are migrated to import from the registry.

    Registry defaults themselves are a separate, tracked item — blanking them is
    a behaviour change requiring the test suite, not a hygiene fix.
    """
    per_file: dict[str, int] = {}
    for f in tracked("*.py"):
        if f in _MODEL_LITERAL_ALLOWED:
            continue
        if f.startswith("tests/") or "/tests/" in f:
            continue
        try:
            src = (ROOT / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        n = len(_MODEL_LITERAL_RE.findall(src))
        if n:
            per_file[f] = n

    total = sum(per_file.values())
    if total <= _MODEL_LITERAL_BASELINE:
        return []

    worst = sorted(per_file.items(), key=lambda kv: -kv[1])[:5]
    detail = ", ".join(f"{f} ({n})" for f, n in worst)
    return [
        f"hardcoded vendor model IDs outside the registry rose to {total}, "
        f"above the recorded baseline of {_MODEL_LITERAL_BASELINE}. "
        f"Import the model from core.model_registry instead of writing the ID "
        f"into the module. Highest counts: {detail}"
    ]


# ── Phase 6: the tier migration's exit criterion, enforced per module ─────
#
# plan.html's Phase 6 exit criterion is that `grep -rn 'model_hint="'` returns
# only the inbound API boundary and test fixtures. That is the END state after
# all eleven §N.1 steps; enforcing it wholesale today would fail on the seven
# steps still to come. So the check is scoped to the modules already migrated
# and the list GROWS as each later step lands — a ratchet, not a cliff.
#
# What it catches: a new call site added to a migrated module using the old
# vocabulary, which would quietly opt that feature back out of governance.
_PHASE6_MIGRATED_MODULES = (
    # §N.1 step 1 — memory & summarisation
    "memory/chat_summarizer.py",
    "memory/postgres_memory.py",
    "core/context_manager.py",
    # §N.1 step 2 — classification paths
    "cil/intent.py",
    "models/classifier.py",
    "models/doc_intent.py",
    "models/query_rewriter.py",
    "models/router.py",
    "agents/router_agent.py",
    # §N.1 step 3 — structured-output agents
    "agents/review_engine.py",
    "agents/orchestrator.py",
    "agents/advanced_reasoning.py",
    # §N.1 step 4 — leaf feature modules
    "routers/skills_router.py",
    "routers/presenton_router.py",
    "routers/broadcast_router.py",
    "routers/cowork_tasks_router.py",
    "routers/threads_router.py",
    "routers/projects_router.py",
    "workers/knowledge_graph_worker.py",
    "workers/cowork_task_worker.py",
    "workers/meeting_worker.py",
    "connectors/mcp_bridge.py",
    "integrations/teams_sdk_app.py",
    "services/teams_adapter.py",
    "services/skill_synthesis.py",
    "services/digest_service.py",
    "tools/n8n_autonomous_builder.py",
    # §N.1 step 6 — CodeWiki, index enrichment, retrieval. The last two were
    # in no step's list: leaf consumers in the step-4 mould that step 4 missed,
    # adopted here so they stop being rediscovered.
    "workers/index_worker.py",
    "workers/codewiki_worker.py",
    "models/hybrid_retriever.py",
    "sandbox/self_healing_engine.py",
    "workers/secure_code_gate_worker.py",
    # §N.1 step 9 — the Chat Auto path. Every in-process chat entry point.
    # routers/ide_router.py is NOT listed: its _hint_map is the inbound
    # boundary shim §E keeps until Phase 9/10.
    #
    # Phase 6.6 migrated the last two holdouts in this file — the CLI direct
    # relay and the OpenAI-compatible endpoint's dispatchers. Listing
    # gateway.py has never said much about them and still does not: they pass
    # VARIABLES (_cli_route, _oai_route, _model_hint), and this check reads
    # keyword literals. That gap is covered by AST assertions in
    # tests/routers/test_gateway_cli_direct_tiers.py,
    # test_oai_compat_dispatch_tiers.py and test_oai_tool_channel.py, not
    # here — a blanket "no _get_claude() in a migrated module" rule would
    # false-positive on the CLI image branch, which legitimately needs a
    # gateway the router has no entry point for.
    "gateway.py",
    "routers/kb_ask_router.py",
    "routers/chat_router.py",
    "workers/chat_worker.py",
    "agents/tools.py",
    # §N.1 step 8 — the document pipeline, plus the last two consumers of
    # core/model_registry.py's SDLC CLI helpers that were not themselves SDLC.
    "workers/doc_worker.py",
    "workers/doc_worker_agent.py",
    "agents/doc_generator_agent.py",
    "services/doc_reviser.py",
    "services/feedback_processor.py",
    "routers/coach_router.py",
    # §N.1 step 10 — the SDLC pipeline, in-process and CLI.
    #
    # Both halves of the sdlc_pipeline package are listed, not just the half
    # that runs: _core.py and _phases.py duplicate 20 top-level functions
    # (~1,819 lines) that the 2026-08-04 extraction copied rather than moved,
    # and step 10 migrated BOTH copies identically. Listing only the live one
    # would leave the dead one free to drift back to the .env path and then be
    # re-wired — which is how the two manifest validators came to differ.
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
    # Not SDLC. agents/react_engine.py and its one consumer carried the last
    # "solution"/"complex" literals outside SDLC, and the check could not see
    # them: they arrived as synthesis_hint=/iteration_hint= on a constructor.
    # Widening the keyword set below to guard step 10's own `hint=` convention
    # made them visible, so they are migrated and listed here rather than left
    # to fail the build.
    "agents/react_engine.py",
)

# The eight tier names plus the legacy aliases that MEAN one of them. A raw
# model id in model_hint= is a user-explicit pick and stays legal forever.
_PHASE6_TIER_HINTS = (
    "simple", "local", "local_mini", "mini", "medium", "complex",
    "haiku", "claude", "sonnet", "gpt", "coding", "agents",
    "solution", "opus", "deep", "vision",
)


# ModelRouter's public entry points, plus the one higher-level API that now
# accepts a tier of its own: agents.orchestrator.OrchestratorAgent.run().
#
# `run` is a common method name, so note WHY including it is safe: this check
# only fires on a call that passes `model_hint=<tier literal>`, and nothing
# else named run() takes that keyword. It was exempt until Phase 6.5 item 1
# gave run() a tier= parameter — before that, flagging its callers would have
# reported them for a gap in the callee with no local fix available.
_ROUTER_ENTRY_POINTS = frozenset({
    "generate", "async_generate", "stream", "async_stream",
    "generate_structured", "route", "run",
    # §N.1 step 10. SDLC does not call the router directly from its stages —
    # it calls its own _llm() shortcut, twice defined, and the state machine
    # wraps that again in _llm_traced(). Those are the routing decision for
    # every SDLC stage, so the check has to treat them as entry points or it
    # ratchets nothing in the largest module it is pointed at. ReactEngine is
    # here for the same reason: the tier arrives on its CONSTRUCTOR.
    "_llm", "_llm_traced", "ReactEngine",
})

# The keyword names that carry a routing decision. `model_hint` was the only
# one until §N.1 step 10 — which is why threads_router.py sat on the migrated
# list for six steps while still passing synthesis_hint="solution", and why
# SDLC's own `hint=` convention was invisible. Safe to include a name as
# generic as "hint" ONLY because the scan is already gated on
# _ROUTER_ENTRY_POINTS above.
_ROUTING_KEYWORDS = frozenset({
    "model_hint", "hint", "synthesis_hint", "iteration_hint",
})


def _tier_hint_message(rel: str, lineno: int, hint: str, kw: str = "model_hint") -> str:
    # `kw` names the keyword that actually carried it. Reporting every finding
    # as model_hint= sent a reader of a synthesis_hint= regression looking for
    # a keyword that is not on the line.
    return (
        f"{rel}:{lineno}: {kw}={hint!r} — this module was migrated in "
        f"Phase 6; use tier=Tier.X with legacy_hint={hint!r} so governance "
        f"applies and the flag-off path is unchanged"
    )


def check_tier_migration(cfg) -> list[str]:
    """No migrated module may go back to model_hint="<tier>" (plan.html Phase 6).

    Scoped to the modules §N.1 steps 1-5 have already converted; the tuple
    grows as later steps land. A raw model id is still legal — that is a
    user-explicit pick, not an application tier request.
    """
    # AST, not grep: these modules now CARRY the old hint strings in comments
    # and docstrings, on purpose — "the old model_hint=\"simple\" said X" is
    # exactly the explanation a reader needs. A text scan would flag the
    # documentation of the fix as the defect.
    bad: list[str] = []
    for rel in _PHASE6_MIGRATED_MODULES:
        path = ROOT / rel
        if not path.exists():
            bad.append(f"{rel}: listed as migrated but the file is gone — "
                       f"update _PHASE6_MIGRATED_MODULES")
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            bad.append(f"{rel}: does not parse ({exc})")
            continue
        # ── The splat idiom ──────────────────────────────────────────────
        #
        # Step 6 introduced `_route = {"model_hint": ...}` / `generate(**_route)`
        # because "resolve a tier" and "use this hint" cannot both be a
        # string. The call scan below is blind to it — it reads keywords, and
        # a splatted local is not one — so the ratchet reported ZERO hits in
        # agents/tools.py, the single most important file §N.1 step 9 changes.
        # A check that cannot see the idiom the migration is written in is not
        # ratcheting anything.
        #
        # Flagged wherever the dict LITERAL appears rather than only at the
        # call, because following the variable would need dataflow and the
        # pair is indefensible anywhere in a migrated module. Comments and
        # docstrings that quote the old hint are untouched: they are not Dict
        # nodes, which is the same reason this check parses instead of greps.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "model_hint"
                        and isinstance(v, ast.Constant)
                        and v.value in _PHASE6_TIER_HINTS):
                    bad.append(_tier_hint_message(rel, node.lineno, v.value))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Only the entry points that actually make the routing decision
            # (see _ROUTER_ENTRY_POINTS). `model_hint` is also a parameter on
            # APIs that merely forward it, and flagging those would report a
            # caller for a gap in its callee.
            callee = (node.func.attr if isinstance(node.func, ast.Attribute)
                      else getattr(node.func, "id", ""))
            if callee not in _ROUTER_ENTRY_POINTS:
                continue
            for kw in node.keywords:
                # keyword form: generate(..., model_hint="complex"),
                # _llm(..., hint="solution"), ReactEngine(synthesis_hint=…)
                if kw.arg in _ROUTING_KEYWORDS:
                    if (isinstance(kw.value, ast.Constant)
                            and kw.value.value in _PHASE6_TIER_HINTS):
                        bad.append(_tier_hint_message(rel, node.lineno,
                                                      kw.value.value, kw.arg))
                    continue                  # a raw model id, or a variable
    return bad


CHECKS = {
    "docs-tracked":         check_docs_tracked,
    "readme-links":         check_readme_links,
    "env-duplicates":       check_env_duplicates,
    "orphan-migrations":    check_orphan_migrations,
    "lockfile-registry":    check_lockfile_registry,
    "internal-urls":        check_internal_urls,
    "secrets":              check_secrets,
    "python-syntax":        check_python_syntax,
    "model-hint-coverage":  check_model_hint_coverage,
    "model-literals":       check_model_literals,
    "docs-panel-coverage":  check_docs_panel_coverage,
    "readme-feature-table": check_readme_feature_table,
    "tier-migration":       check_tier_migration,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="AiNxt release-readiness checks")
    ap.add_argument("only", nargs="*", help="run only these checks")
    ap.add_argument("--list", action="store_true", help="list checks and exit")
    ap.add_argument("--skip", default="", help="comma-separated checks to skip")
    ap.add_argument("--warn-only", action="store_true", help="report but always exit 0")
    args = ap.parse_args()

    if args.list:
        for name, fn in CHECKS.items():
            first = (fn.__doc__ or "").strip().splitlines()[0]
            print(f"  {name:22s} {first}")
        return 0

    cfg = load_config()
    warn_only = args.warn_only or os.getenv("RELEASE_CHECKS_WARN_ONLY", "") not in ("", "0", "false")
    skip = {s.strip() for s in (args.skip or os.getenv("RELEASE_CHECKS_SKIP", "")).split(",") if s.strip()}
    selected = [n for n in (args.only or CHECKS) if n not in skip]

    unknown = [n for n in selected if n not in CHECKS]
    if unknown:
        print(f"unknown check(s): {', '.join(unknown)}\nAvailable: {', '.join(CHECKS)}")
        return 2

    print(f"AiNxt release checks — internal_pattern={cfg['internal_pattern']!r}"
          f"{'  (warn-only)' if warn_only else ''}\n")
    total = 0
    for name in selected:
        try:
            failures = CHECKS[name](cfg)
        except Exception as exc:
            failures = [f"check raised {type(exc).__name__}: {exc}"]
        if failures:
            total += len(failures)
            print(f"  FAIL  {name}  ({len(failures)})")
            for f in failures[:25]:
                print(f"          {f}")
            if len(failures) > 25:
                print(f"          … and {len(failures) - 25} more")
        else:
            print(f"  ok    {name}")

    print()
    if total:
        print(f"{total} problem(s) found."
              + ("  Exiting 0 because warn-only is set." if warn_only else ""))
        return 0 if warn_only else 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
