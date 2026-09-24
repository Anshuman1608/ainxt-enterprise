# SPDX-License-Identifier: MIT
# ============================================================
# core/feature_registry.py — catalogue invariants.
#
# The catalogue is code-seeded, so the things that can go wrong are all
# declaration mistakes that would surface as silent misrouting rather than an
# error. The two that matter most:
#
#   1. default_capability is a LIVE fallback, consulted BEFORE the call site's
#      own literal. So seeding one that resolves to a different tier than the
#      literal those call sites pass today would change which vendor serves
#      that feature the moment the table landed. test_default_capability_*
#      below is what stops that.
#   2. A capability name that is not in models/model_router.py's _HINT_MAP
#      would be passed to route() as an unrecognised hint and fall through to
#      complexity classification, quietly ignoring the admin's choice.
# ============================================================

from __future__ import annotations

import pytest

from core.feature_registry import (
    CAPABILITIES, DATA_CLASSIFICATIONS, FEATURES, FEATURES_BY_KEY,
    categories, feature_keys, get_feature,
)


def test_keys_are_unique() -> None:
    keys = [f.feature_key for f in FEATURES]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, f"duplicate feature_key(s): {dupes}"


def test_keys_are_dotted_lowercase() -> None:
    # The key becomes an env var suffix (AINXT_FEATURE_MODEL_<KEY>) and a
    # literal in call sites, so the shape has to be predictable.
    import re
    bad = [f.feature_key for f in FEATURES
           if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)+", f.feature_key)]
    assert not bad, f"feature keys must be dotted lowercase: {bad}"


def test_keys_fit_the_column() -> None:
    # feature_registry.feature_key and model_usages.feature_key are VARCHAR(64).
    too_long = [f.feature_key for f in FEATURES if len(f.feature_key) > 64]
    assert not too_long, f"feature keys exceed VARCHAR(64): {too_long}"


def test_every_feature_has_display_name_category_and_module() -> None:
    for f in FEATURES:
        assert f.display_name, f.feature_key
        assert f.category, f.feature_key
        assert f.owning_module, f.feature_key


def test_declared_capabilities_are_all_valid() -> None:
    bad = [(f.feature_key, f.default_capability) for f in FEATURES
           if f.default_capability and f.default_capability not in CAPABILITIES]
    assert not bad, f"unknown default_capability: {bad}"


def test_declared_classifications_are_all_valid() -> None:
    bad = [(f.feature_key, f.max_data_classification) for f in FEATURES
           if f.max_data_classification not in DATA_CLASSIFICATIONS]
    assert not bad, f"unknown max_data_classification: {bad}"


# ── the two invariants that prevent silent misrouting ────────────────────────

def test_capability_vocabulary_matches_the_router() -> None:
    from models.model_router import _HINT_MAP

    missing = [c for c in CAPABILITIES if c not in _HINT_MAP]
    assert not missing, (
        f"capability name(s) {missing} are not keys in _HINT_MAP, so route() "
        f"would ignore them and fall through to complexity classification"
    )


def test_data_classification_ladder_matches_rag_acl() -> None:
    import core.rag_acl as acl

    # core/rag_acl.py is the authoritative ladder. profiles/routing.py has its
    # own lowercase 4-tier variant; this must follow rag_acl, not that one.
    src = open(acl.__file__).read()
    for level in DATA_CLASSIFICATIONS:
        assert level in src, f"{level} is not a core/rag_acl.py classification"


def test_local_only_capability_is_the_router_local_tier() -> None:
    from models.model_router import TIER_SIMPLE, _HINT_MAP

    # A CONFIDENTIAL+ feature resolving to "local-only" must actually stay
    # on-premise; TIER_SIMPLE is the in-house GPU path.
    assert _HINT_MAP["local-only"] == TIER_SIMPLE


# Each seeded default_capability, and the tier literal the call sites for that
# feature pass today. The capability MUST resolve to the identical tier, or the
# feature changes vendor on deploy. Derived from the actual literals in the
# source at the time of seeding.
_CAPABILITY_MUST_EQUAL_LITERAL = {
    "local-only": "simple",
    "fast":       "haiku",
    "balanced":   "complex",
    "expert":     "solution",
}


@pytest.mark.parametrize("capability, literal", sorted(_CAPABILITY_MUST_EQUAL_LITERAL.items()))
def test_default_capability_resolves_to_the_same_tier_as_the_literal(
    capability: str, literal: str,
) -> None:
    from models.model_router import _HINT_MAP

    assert _HINT_MAP[capability] == _HINT_MAP[literal], (
        f"seeding default_capability={capability!r} would route those call "
        f"sites to {_HINT_MAP[capability]!r} instead of the {_HINT_MAP[literal]!r} "
        f"they use today — a behaviour change on deploy, not a default"
    )


def test_only_verified_capabilities_are_seeded() -> None:
    # "medium"/"gpt" (TIER_MEDIUM, OpenAI) have no capability equivalent, so
    # features whose call sites use them are deliberately left at None rather
    # than seeded with the nearest name, which would switch them to Anthropic.
    seeded = {f.default_capability for f in FEATURES if f.default_capability}
    unverified = seeded - set(_CAPABILITY_MUST_EQUAL_LITERAL)
    assert not unverified, (
        f"default_capability {sorted(unverified)} is seeded but has no verified "
        f"tier-equality assertion above. Either add one, or leave the feature "
        f"at None so it falls through to the call site's literal."
    )


def test_features_needing_confidential_data_are_not_pinned_to_cloud() -> None:
    # A feature that may see CONFIDENTIAL+ data is pinned on-premise at runtime
    # by route()'s privacy floor, so declaring a cloud-ish default for it would
    # be misleading in the admin UI even though the floor would win.
    for f in FEATURES:
        if f.max_data_classification in ("CONFIDENTIAL", "RESTRICTED", "PCI_SENSITIVE"):
            assert f.default_capability in (None, "local-only"), (
                f"{f.feature_key} may process {f.max_data_classification} data "
                f"but declares default_capability={f.default_capability!r}"
            )


# ── lookups ──────────────────────────────────────────────────────────────────

def test_lookup_helpers() -> None:
    assert get_feature("skills.generate") is not None
    assert get_feature("no.such.feature") is None
    assert set(feature_keys()) == set(FEATURES_BY_KEY)
    assert len(categories()) == len({f.category for f in FEATURES})
    # categories() preserves first-declaration order (the UI's group order)
    assert categories()[0] == FEATURES[0].category

# ── the catalogue must match the call sites ──────────────────────────────────

def _referenced_keys() -> set[str]:
    """Every feature_key passed to resolve_feature_model() in non-test code."""
    import pathlib
    import re
    import subprocess

    root = pathlib.Path(__file__).resolve().parents[2]
    out = subprocess.run(
        ["grep", "-rn", "--include=*.py", "resolve_feature_model(", "."],
        cwd=root, capture_output=True, text=True,
    ).stdout
    keys: set[str] = set()
    for line in out.splitlines():
        path, _, text = line.split(":", 2)
        path = path.lstrip("./")
        if path.startswith("tests/") or "/tests/" in path:
            continue
        m = re.search(r'resolve_feature_model\(\s*"([^"]+)"', text)
        if m:
            keys.add(m.group(1))
    return keys


def test_every_declared_feature_is_actually_referenced() -> None:
    """No dead rows.

    A feature an admin can assign but that no call site resolves is worse than
    no row at all: the assignment appears to take effect and silently does
    nothing. chat.respond and sdlc.patch were removed for exactly this reason —
    see the note at the top of core/feature_registry.py.
    """
    dead = sorted(set(FEATURES_BY_KEY) - _referenced_keys())
    assert not dead, (
        f"declared but never passed to resolve_feature_model(): {dead}. "
        f"Either wire the call site or remove the declaration."
    )


def test_every_referenced_key_is_declared() -> None:
    """No silent no-ops.

    resolve_feature_model() returns the caller's `default` for an unknown key,
    so a typo'd or undeclared key fails silently — the feature simply never
    becomes assignable and nothing reports it.
    """
    undeclared = sorted(_referenced_keys() - set(FEATURES_BY_KEY))
    assert not undeclared, (
        f"passed to resolve_feature_model() but not declared in FEATURES: "
        f"{undeclared}. These resolve to the call site's default forever."
    )
