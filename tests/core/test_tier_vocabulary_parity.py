# SPDX-License-Identifier: MIT
"""The admin screen's tier list must match core/tiers.py exactly.

ai-ui/src/utils/tierGovernance.js carries its own copy of the eight tier
names. It has to: the screen renders all eight rows even when the API returns
fewer, because a tier that silently disappears from the table is the class of
failure this whole migration removes — so it cannot derive the list from the
response it is checking.

That makes it a second place the vocabulary is written down, which is exactly
the drift this migration exists to prevent. This test closes the loop: Python
stays authoritative, and the copy is verified rather than trusted. If they
ever disagree, the screen would offer a ninth tier the database CHECK rejects,
or hide a real one.

Deliberately textual. Importing the JS is not possible from pytest, and
shelling out to node would make a Python test depend on a Node toolchain that
CI's test job does not install.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.tiers import ALL_TIERS

JS = Path(__file__).resolve().parents[2] / "ai-ui" / "src" / "utils" / "tierGovernance.js"


def _js_tiers() -> list[str]:
    src = JS.read_text(encoding="utf-8")
    m = re.search(r"export const ALL_TIERS = Object\.freeze\(\[(.*?)\]\)", src, re.S)
    assert m, "ALL_TIERS is no longer a frozen array literal in tierGovernance.js"
    return re.findall(r'"([^"]+)"', m.group(1))


@pytest.mark.skipif(not JS.exists(), reason="ai-ui is not checked out")
def test_the_javascript_tier_list_matches_the_python_enum() -> None:
    assert _js_tiers() == [t.value for t in ALL_TIERS]


@pytest.mark.skipif(not JS.exists(), reason="ai-ui is not checked out")
def test_the_javascript_cross_family_tier_is_a_real_tier() -> None:
    """§M.3b's warning names a tier; a typo would silently disable it."""
    src = JS.read_text(encoding="utf-8")
    m = re.search(r"export const CROSS_FAMILY_TIERS = Object\.freeze\(\[(.*?)\]\)", src, re.S)
    assert m, "CROSS_FAMILY_TIERS is no longer a frozen array literal"
    named = re.findall(r'"([^"]+)"', m.group(1))
    assert named, "at least one tier must carry the cross-provider warning"
    assert set(named) <= {t.value for t in ALL_TIERS}


@pytest.mark.skipif(not JS.exists(), reason="ai-ui is not checked out")
def test_the_javascript_reviewer_tier_is_a_real_tier() -> None:
    src = JS.read_text(encoding="utf-8")
    m = re.search(r'export const REVIEWER_TIER = "([^"]+)"', src)
    assert m, "REVIEWER_TIER is no longer a string literal"
    assert m.group(1) in {t.value for t in ALL_TIERS}
