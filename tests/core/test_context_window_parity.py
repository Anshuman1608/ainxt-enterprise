# SPDX-License-Identifier: MIT
"""Phase 7 — the frontend carries no second copy of the context-window table.

``config/model_context_windows.json`` is authoritative: ``gateway.py``'s
``_load_context_config()`` reads it for routing, and ``db/migrate.py``'s seed
path writes ``capabilities.context_window`` from it. The picker's "· 200K"
badge is the same fact, and it used to be a **hand-copied** subset of that file
living in two components at once.

One caveat worth stating, because it decided the design: that seed only runs
for model rows it CREATES. Measured on the reference deployment, the capability
is set on **0 of 12** enabled models — every row predates it. So the badge
reads the capability when an administrator has declared one and falls back to
the same config-file family resolver otherwise; capability-only would have
deleted the badge everywhere rather than corrected it.

It had already drifted, which is the whole argument for this test rather than
for a comment:

    kimi      262144 in the config, tagged 128K in the UI  (wrong by half)
    glm       131072 in the config, no entry               (no badge)
    qwen      131072 in the config, no entry               (no badge)
    deepseek   65536 in the config, no entry               (no badge)
    llama, gemma, mistral, gpt-oss  — likewise

Nine of seventeen keys, one of them wrong. A model an administrator configured
got no badge, and a Kimi model got someone else's number.

Deliberately textual, following ``test_tier_vocabulary_parity.py``: importing
the JS is not possible from pytest, and shelling out to node would make a
Python test depend on a toolchain CI's test job does not install.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "model_context_windows.json"
COMPONENTS = ROOT / "ai-ui" / "src" / "components"
PICKER = ROOT / "ai-ui" / "src" / "utils" / "modelPicker.js"

# Every component that renders a model picker.
PICKER_FILES = ("Chat.jsx", "KbChat.jsx", "Office.jsx", "Code.jsx", "CoworkDesktop.jsx")

requires_ui = pytest.mark.skipif(not COMPONENTS.exists(), reason="ai-ui is not checked out")


def _config_keys() -> list[str]:
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    keys = list(data.get("context_windows", {}))
    assert keys, "config/model_context_windows.json has no context_windows"
    return keys


def _strip_comments(src: str) -> str:
    """Drop // and /* */ comments.

    These files now *document* the table they used to carry, naming the drifted
    keys, so a substring scan over raw source would report the explanation as
    the defect.
    """
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def test_the_config_file_is_the_one_the_backend_reads():
    """If this file moved, the badge and the router would disagree silently and
    every assertion below would pass by vacuously reading an empty table."""
    assert CONFIG.exists(), f"{CONFIG} is missing"
    gateway = (ROOT / "gateway.py").read_text(encoding="utf-8", errors="replace")
    assert "config" in gateway and "model_context_windows.json" in gateway


@requires_ui
@pytest.mark.parametrize("name", PICKER_FILES)
def test_no_picker_hardcodes_a_context_window(name: str):
    """The old table's shape was ``["gpt-5", "256K"]`` — a vendor substring
    paired with a window. One entry is enough to start drifting again."""
    path = COMPONENTS / name
    if not path.exists():
        pytest.skip(f"{name} not present")
    src = _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
    offenders = [k for k in _config_keys()
                 if re.search(rf"""\[\s*["']{re.escape(k)}["']\s*,""", src)]
    assert not offenders, (
        f"{name} pairs a context window with the model-name substring(s) "
        f"{offenders} — read capabilities.context_window off the model row "
        f"instead (utils/modelPicker.js::formatContextWindow)"
    )


@requires_ui
@pytest.mark.parametrize("name", PICKER_FILES)
def test_no_picker_declares_a_badge_table(name: str):
    path = COMPONENTS / name
    if not path.exists():
        pytest.skip(f"{name} not present")
    src = _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
    for banned in ("MODEL_CONTEXT_BADGE", "_modelContextBadge"):
        assert banned not in src, f"{name} still declares or calls {banned}"


@requires_ui
def test_the_shared_formatter_names_no_model():
    """``formatContextWindow`` takes a NUMBER. The moment it branches on a
    model name it has become the table again, in a new location."""
    src = _strip_comments(PICKER.read_text(encoding="utf-8", errors="replace"))
    body_start = src.index("export function formatContextWindow")
    body = src[body_start:src.index("\n}", body_start)]
    for key in _config_keys():
        assert key not in body, (
            f"formatContextWindow branches on the model-name substring {key!r}"
        )


@requires_ui
def test_the_badge_reads_the_key_every_hop_writes():
    """Closes the loop on the NAME. db/migrate.py writes
    capabilities.context_window, both catalogues emit `context_window`, and the
    picker consumes `m.context_window`. A rename at any hop leaves the badge
    blank with nothing failing anywhere."""
    migrate = (ROOT / "db" / "migrate.py").read_text(encoding="utf-8", errors="replace")
    assert 'cap["context_window"]' in migrate

    gateway = (ROOT / "gateway.py").read_text(encoding="utf-8", errors="replace")
    assert 'entry["context_window"] = _cw' in gateway

    registry = (ROOT / "core" / "llm_provider_registry.py").read_text(
        encoding="utf-8", errors="replace")
    assert 'entry["context_window"] = caps["context_window"]' in registry

    picker = PICKER.read_text(encoding="utf-8", errors="replace")
    assert "m.context_window" in picker


def test_the_catalogues_never_put_a_null_on_the_wire():
    """Emitting the key unconditionally would send ``null`` for every model
    whose window could not be resolved, which is a different thing from
    "unknown". Both catalogues guard the assignment.

    AST rather than text: the point is that the assignment is *conditional*.
    """
    import ast as _ast

    for path in (ROOT / "gateway.py", ROOT / "core" / "llm_provider_registry.py"):
        tree = _ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        guarded = False
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.If):
                continue
            if "context_window" not in _ast.dump(node.test) and not any(
                isinstance(t, _ast.Name) and t.id == "_cw"
                for t in _ast.walk(node.test)
            ):
                continue
            if "context_window" in " ".join(_ast.dump(b) for b in node.body):
                guarded = True
                break
        assert guarded, (
            f"{path.name} assigns context_window without first checking there "
            f"is a value to assign"
        )


def test_the_web_catalogue_falls_back_to_the_config_family_resolver():
    """The measured reason this fallback exists.

    ``capabilities.context_window`` is set on **0 of 12** enabled models on the
    reference deployment: Phase 2's seed path skips rows that already exist,
    and its backfill covered ``privacy_class``/``modality`` rather than this
    key. A capability-only badge would therefore have REMOVED the badge from
    every model rather than corrected it — strictly worse than the drifted
    table it replaced.

    So the admin capability wins and the config-file family resolver
    (``_context_window_for``, the same one this module routes on) answers when
    it is unset. That is also the precedence
    ``messages_compat_router::_with_context_window`` already uses for the CLI
    payload, so the two catalogues cannot disagree about one model.
    """
    gateway = (ROOT / "gateway.py").read_text(encoding="utf-8", errors="replace")
    assert '_cw = caps.get("context_window") or _context_window_for(m["model_id"])' in gateway, (
        "the web catalogue no longer falls back to the config-file resolver; on a "
        "deployment whose rows predate the capability backfill every badge "
        "disappears"
    )


def test_the_core_catalogue_does_not_import_the_gateway():
    """``get_cli_style_models`` deliberately emits the capability ONLY. It lives
    in ``core`` and the family resolver lives in ``gateway``; reading it here
    would invert the layering, and its one rendering consumer already applies
    the fallback itself."""
    registry = (ROOT / "core" / "llm_provider_registry.py").read_text(
        encoding="utf-8", errors="replace")
    assert "_context_window_for" not in registry
    compat = (ROOT / "routers" / "messages_compat_router.py").read_text(
        encoding="utf-8", errors="replace")
    assert "from gateway import _context_window_for" in compat, (
        "the CLI payload no longer resolves a window for rows with no "
        "capability set"
    )
