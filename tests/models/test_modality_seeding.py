# SPDX-License-Identifier: MIT
"""capabilities.modality must be seeded wherever an llm_models row is born.

Found while testing Phase 6 on a live deployment, and worth writing down
because the symptom looked nothing like the cause:

    The Tiers screen offered NO models for video-generation or image-input,
    even though the registry held two Veo models and a dozen Claudes.

The resolver was right and the data was empty. `capabilities.modality` was
set in exactly one place — db/migrate.py's one-time backfill — while the
Providers screen's "Sync models" button created rows through a completely
different path that never set it. On the deployment in question the
migration ran at 09:39 and the sync imported 59 capability-blind models at
09:42, so the backfill had already been and gone.

`tier_resolver.modality_of()` reads an absent modality as text-only (§L.3a,
fail-safe and correct), so all 59 became invisible to the three modality
tiers, permanently, with no error anywhere.

The fix is one heuristic with one home, applied on every creation path.
These tests assert the home and the paths, not the heuristic's taste.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from core.tiers import (
    MODALITY_IMAGE_IN,
    MODALITY_IMAGE_OUT,
    MODALITY_SOURCE_DECLARED,
    MODALITY_SOURCE_INFERRED,
    MODALITY_TEXT,
    MODALITY_VIDEO_OUT,
    modality_for_model_id,
    seed_modality,
)
from core.tier_resolver import modality_of

ROOT = pathlib.Path(__file__).resolve().parents[2]


# ── The heuristic answers the three questions the tiers ask ───────────────


@pytest.mark.parametrize("model_id,expected", [
    ("veo-3.1-generate-preview",      MODALITY_VIDEO_OUT),
    ("veo-3.1-fast-generate-preview", MODALITY_VIDEO_OUT),
    ("gemini-3.1-flash-image",        MODALITY_IMAGE_OUT),
    ("dall-e-3",                      MODALITY_IMAGE_OUT),
    ("claude-sonnet-5",               MODALITY_IMAGE_IN),
    ("gpt-5.4",                       MODALITY_IMAGE_IN),
])
def test_the_models_that_caused_the_bug_are_now_eligible(model_id, expected):
    assert expected in modality_for_model_id(model_id)


def test_everything_is_text_capable():
    """The conservative floor: a model we know nothing about is still a text
    model, so the five text tiers never lose a candidate to this heuristic."""
    for mid in ("llama3.2:1b", "some-unknown-model", "", "mystery-v9"):
        assert modality_for_model_id(mid) == [MODALITY_TEXT]


def test_a_modality_is_only_added_on_a_positive_signal():
    """Over-claiming is the dangerous direction: it lets the resolver pick a
    model that cannot do the job, which is the failure the filter exists to
    prevent. A false negative just means an admin sets it by hand."""
    caps = modality_for_model_id("llama3.2:1b")
    assert MODALITY_VIDEO_OUT not in caps
    assert MODALITY_IMAGE_OUT not in caps
    assert MODALITY_IMAGE_IN not in caps


def test_the_heuristic_round_trips_through_the_resolver():
    """The seed and the reader have to agree on the vocabulary — a seed that
    emits a token modality_of() does not recognise would be silently inert."""
    for mid in ("veo-3.1-generate-preview", "claude-sonnet-5", "llama3.2:1b"):
        seeded = modality_for_model_id(mid)
        assert modality_of({"modality": seeded}) == seeded


def test_every_value_the_heuristic_emits_is_admin_api_legal():
    """The admin API validates capabilities.modality against a fixed set. A
    seed outside that set would make a discovered row unsaveable by hand."""
    from routers.llm_provider_admin_router import _CAP_MODALITIES
    for mid in ("veo-3.1-generate-preview", "gemini-3.1-flash-image",
                "claude-sonnet-5", "gpt-5.4", "dall-e-3", "llama3.2:1b"):
        assert set(modality_for_model_id(mid)) <= _CAP_MODALITIES


# ── One home, and every creation path uses it ─────────────────────────────


def test_the_heuristic_is_not_duplicated_in_migrate():
    """It used to live as two byte-identical copies inside db/migrate.py and
    nowhere else — which is precisely why the third caller that needed it
    (model discovery) silently did without."""
    src = (ROOT / "db" / "migrate.py").read_text(encoding="utf-8", errors="ignore")
    assert 'out.append("video-out")' not in src, (
        "db/migrate.py has re-grown its own copy of the modality heuristic; "
        "import core.tiers.modality_for_model_id instead")
    assert src.count("modality_for_model_id") >= 2


# Every non-test module that constructs an LLMModel row. Derived rather than
# hand-listed, because a hand-list is how this test came to check only the
# admin router while db/bootstrap_llm_providers.py quietly inserted
# capability-blind rows at install time — found on a live deployment, where
# llama3.2:1b (the model assigned to the highest-volume tier) carried nothing
# but {"billing_tier": "free"}.
def _llm_model_creation_files() -> list[pathlib.Path]:
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "venv/", ".venv/", "AgentStudio/")):
            continue
        try:
            src = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "LLMModel(" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        if any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "LLMModel"
               for n in ast.walk(tree)):
            found.append(path)
    return found


def test_the_set_of_creation_paths_is_the_one_we_think_it_is():
    """A new file that inserts models is a new place the modality can go
    missing. Listed explicitly so adding one is a deliberate act — the
    assertion below then covers it automatically."""
    rels = {p.relative_to(ROOT).as_posix() for p in _llm_model_creation_files()}
    assert rels == {
        "db/bootstrap_llm_providers.py",          # install.sh, at install time
        "db/migrate.py",                          # Part AC1 seeding
        "routers/llm_provider_admin_router.py",   # discovery, manual add, ollama pull
    }, f"the set of LLMModel(...) creation paths changed: {sorted(rels)}"


@pytest.mark.parametrize("rel", [
    "routers/llm_provider_admin_router.py",
    "db/bootstrap_llm_providers.py",
    "db/migrate.py",
])
def test_every_llm_models_creation_path_seeds_modality(rel):
    """The real assertion. Any code that constructs an LLMModel row must give
    it a modality, or that model is text-only to the resolver forever.

    Walks each file's AST for `LLMModel(...)` constructions and checks the
    capabilities each one passes. A new creation path added without a modality
    fails here rather than six weeks later as "the dropdown is empty".
    """
    path = ROOT / rel
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    src_lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()

    constructions = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "LLMModel"
    ]
    assert constructions, "no LLMModel(...) construction found — did the file move?"

    for node in constructions:
        caps = next((k.value for k in node.keywords if k.arg == "capabilities"), None)
        assert caps is not None, (
            f"{path.name}:{node.lineno}: LLMModel(...) passes no capabilities at all")
        # Either an inline dict containing "modality", or a name bound to a
        # dict that had modality setdefault-ed onto it a few lines above.
        # Either seed_modality(...) wrapping the dict inline, an inline dict
        # that names "modality" itself, or a name that seed_modality() was
        # called on a few lines above.
        if isinstance(caps, ast.Call):
            assert getattr(caps.func, "id", "") == "seed_modality", (
                f"{path.name}:{node.lineno}: LLMModel(...) builds capabilities "
                f"from a call that is not seed_modality()")
        elif isinstance(caps, ast.Dict):
            keys = {k.value for k in caps.keys if isinstance(k, ast.Constant)}
            assert "modality" in keys, (
                f"{path.name}:{node.lineno}: LLMModel(...) builds capabilities "
                f"inline without a modality — this row will be text-only to "
                f"the tier resolver")
        else:
            assert isinstance(caps, ast.Name), (
                f"{path.name}:{node.lineno}: unrecognised capabilities shape")
            window = "\n".join(src_lines[max(0, node.lineno - 40):node.lineno])
            seeded_here = f"seed_modality({caps.id}" in window
            # db/migrate.py builds its caps in _capabilities_for(), which
            # seeds on the way out. Accept the assignment from it, since
            # test_the_backfill_records_provenance_too pins that function.
            seeded_upstream = f"{caps.id} = _capabilities_for(" in window
            assert seeded_here or seeded_upstream, (
                f"{path.name}:{node.lineno}: capabilities `{caps.id}` reaches "
                f"LLMModel(...) without seed_modality() being called on it")


def test_the_sync_merge_path_repairs_existing_blind_rows():
    """Discovery must also backfill rows it has seen before. Without this the
    59 already-imported models would stay invisible until someone re-ran a
    database migration, which is not a thing an administrator should have to
    know to do."""
    src = (ROOT / "routers" / "llm_provider_admin_router.py").read_text(
        encoding="utf-8", errors="ignore")
    assert "seed_modality(merged, mid)" in src


# ── Provenance: the guess says that it is a guess (Phase 6.5 item 4) ──────


def test_an_inferred_modality_is_marked_as_one():
    """The whole of item 4. A genuinely text-only model and a model the
    heuristic gave up on both store exactly ["text"], so without a marker
    written at seed time there is no way to ask which is which — and a wrong
    guess removes a model from the image/video tiers with no error anywhere."""
    caps = seed_modality({}, "veo-3.1-generate-preview")
    assert caps["modality"] == [MODALITY_TEXT, MODALITY_VIDEO_OUT]
    assert caps["modality_source"] == MODALITY_SOURCE_INFERRED


def test_an_administrator_declaration_is_marked_as_one():
    caps = seed_modality({"modality": ["video-out"]}, "mystery-v9", declared=True)
    assert caps["modality_source"] == MODALITY_SOURCE_DECLARED
    # Declaring does not re-guess: the admin's list is the answer.
    assert caps["modality"] == ["video-out"]


def test_declaring_never_overwrites_the_administrators_modality():
    """An admin may legitimately declare something the heuristic would never
    produce — a vision model whose id says nothing, an image model that also
    reads images. Re-running the guess here would erase exactly the case the
    editor exists to fix."""
    caps = seed_modality({"modality": ["text", "image-in"]}, "veo-3.1-generate-preview",
                         declared=True)
    assert caps["modality"] == ["text", "image-in"]
    assert MODALITY_VIDEO_OUT not in caps["modality"]


def test_a_resync_does_not_downgrade_a_declaration():
    """Model discovery re-runs over rows it has already seen, on every press
    of "Sync models". If the merge path reset the marker, every confirmation
    an administrator had made would be erased the next time anyone synced —
    and doctor.sh would go back to asking them to do work they had done."""
    row = {"modality": ["video-out"], "modality_source": MODALITY_SOURCE_DECLARED}
    assert seed_modality(row, "veo-3.1-generate-preview") == {
        "modality": ["video-out"], "modality_source": MODALITY_SOURCE_DECLARED,
    }


def test_an_existing_modality_is_left_alone_marker_or_not():
    """Rows seeded before provenance existed carry a modality and no marker.
    Backfilling one would be a lie: nobody confirmed it. Absent and "inferred"
    read the same to every consumer, which is why the checks test for the
    absence of "declared" rather than the presence of "inferred"."""
    row = {"modality": ["text"]}
    assert seed_modality(row, "gemini-3.1-flash-image") == {"modality": ["text"]}


def test_the_marker_is_admin_api_legal():
    """The admin API validates the governed capability keys. A marker outside
    its vocabulary would make a seeded row unsaveable by hand — the same class
    of bug as an illegal modality value."""
    from routers.llm_provider_admin_router import _validate_capabilities
    for source in (MODALITY_SOURCE_INFERRED, MODALITY_SOURCE_DECLARED):
        _validate_capabilities({"modality": ["text"], "modality_source": source})
    with pytest.raises(ValueError):
        _validate_capabilities({"modality_source": "guessed"})


def test_the_backfill_records_provenance_too():
    """db/migrate.py's AC4 backfill is an inference like any other. It used to
    write a bare `caps["modality"] = ...`, which would have produced rows
    indistinguishable from confirmed ones."""
    src = (ROOT / "db" / "migrate.py").read_text(encoding="utf-8", errors="ignore")
    assert 'caps["modality"] = ' not in src, (
        "db/migrate.py writes modality directly again — go through "
        "core.tiers.seed_modality so the guess is recorded as a guess")
    assert src.count("_seed_modality(") >= 2
