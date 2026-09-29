# SPDX-License-Identifier: MIT
"""Phase 6 §N.1 step 5 — image-vs-video when the classifier says both.

The reported symptom: "generate a video of a tiger" produced an image, every
time. The cause was not the classifier being wrong in isolation — it set
img_intent AND vid_intent to "generate" at an identical 0.65 confidence, and
the gateway simply evaluated the image gate first and returned. There was no
comparison of the two confidences anywhere.

So the bug was not "image won", it was "nothing chose". These tests are
about the choosing.

⚠ THESE TESTS ARE NOT FIELD EVIDENCE. (plan.html Phase 6.5, item 5.)
On the live deployment the tie-break never fired: once the classifier moved to
the assigned Haiku it stopped hedging (img=none, vid=0.95), so there was no tie
to break, and the fix that actually resolved the reported bug was the tier
migration giving video-generation an eligible model. This helper is correct
insurance for a weaker classifier — an SLM that returns 0.65 on both, which is
exactly what llama3.2:1b did — and nothing more than that has been observed.
Do not read the green ticks below as "the tie-break works in production".

gateway.py writes to /var/lib/ainxt at import time and is not importable
under pytest, so _media_intent_winner is loaded from source in isolation —
the same constraint tests/agents/test_gateway_passthrough_logic.py works
around. Loading just this function keeps the assertions on real code rather
than a reimplementation of it.
"""

from __future__ import annotations

import ast
import pathlib
import types

import pytest


def _load_winner():
    """Exec only _media_intent_winner and its module-level constants."""
    src = (pathlib.Path(__file__).resolve().parents[2] / "gateway.py").read_text(
        encoding="utf-8", errors="ignore")
    tree = ast.parse(src)
    wanted_names = {"_MEDIA_TIEBREAK_MIN_CONFIDENCE", "_VIDEO_WORDS", "_IMAGE_WORDS"}
    picked: list = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in wanted_names for t in node.targets
        ):
            picked.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "_media_intent_winner":
            picked.append(node)
    assert len(picked) == 4, f"expected 3 constants + 1 function, got {len(picked)}"

    import logging
    ns: dict = {"logger": logging.getLogger("test")}
    exec(compile(ast.Module(body=picked, type_ignores=[]), "gateway.py", "exec"), ns)
    return ns["_media_intent_winner"]


winner = _load_winner()


def _state(img="none", img_conf=0.0, vid="none", vid_conf=0.0):
    return types.SimpleNamespace(
        img_intent=img, img_confidence=img_conf,
        vid_intent=vid, vid_confidence=vid_conf,
    )


# ── Rule 1: only one intent fires — unchanged from before the tie-break ────


def test_image_only_still_routes_to_image():
    assert winner(_state(img="generate", img_conf=0.9), "draw a tiger") == "image"


def test_video_only_still_routes_to_video():
    assert winner(_state(vid="generate", vid_conf=0.9), "a tiger running") == "video"


def test_neither_intent_routes_nowhere():
    assert winner(_state(), "what is the capital of France") is None


def test_below_the_confidence_floor_does_not_fire():
    """0.5 is the floor and it is exclusive, exactly as both gates had it."""
    assert winner(_state(img="generate", img_conf=0.5), "draw a tiger") is None
    assert winner(_state(img="generate", img_conf=0.51), "draw a tiger") == "image"


def test_a_non_generate_intent_never_fires():
    """img_intent has other values ("edit", "none"); only generation routes."""
    assert winner(_state(img="edit", img_conf=0.99), "fix this image") is None


# ── Rule 2: both fire, confidences differ — the more confident one wins ────


def test_the_more_confident_intent_wins():
    s = _state(img="generate", img_conf=0.65, vid="generate", vid_conf=0.9)
    assert winner(s, "make me something") == "video"


def test_the_more_confident_intent_wins_the_other_way_too():
    s = _state(img="generate", img_conf=0.95, vid="generate", vid_conf=0.6)
    assert winner(s, "make me something") == "image"


# ── Rule 3: exact tie — the medium the user actually named ────────────────


def test_an_exact_tie_is_broken_by_the_word_video():
    """The reported bug, as a test. Identical 0.65 on both, and the user
    said "video" — which is precisely the case that used to return image."""
    s = _state(img="generate", img_conf=0.65, vid="generate", vid_conf=0.65)
    assert winner(s, "Generate a video of tiger") == "video"


@pytest.mark.parametrize("word", ["clip", "animate", "animation", "footage",
                                  "movie", "reel", "gif"])
def test_other_motion_words_also_break_the_tie_to_video(word):
    s = _state(img="generate", img_conf=0.65, vid="generate", vid_conf=0.65)
    assert winner(s, f"make a {word} of a tiger") == "video"


def test_an_exact_tie_is_broken_by_the_word_image():
    s = _state(img="generate", img_conf=0.65, vid="generate", vid_conf=0.65)
    assert winner(s, "Generate an image of a tiger") == "image"


def test_a_prompt_naming_both_media_keeps_the_old_default():
    """Genuinely ambiguous ("a video and a poster"), so neither word decides.
    Falling back to image is the pre-existing behaviour, kept deliberately:
    a tie-break should resolve the cases it can and not invent a preference
    for the ones it cannot."""
    s = _state(img="generate", img_conf=0.65, vid="generate", vid_conf=0.65)
    assert winner(s, "a video and a poster of a tiger") == "image"


def test_a_tie_with_no_medium_word_keeps_the_old_default():
    s = _state(img="generate", img_conf=0.65, vid="generate", vid_conf=0.65)
    assert winner(s, "a tiger in the snow") == "image"


# ── Robustness: the gate must not raise on a malformed conv_state ─────────


def test_a_missing_conv_state_is_not_an_error():
    assert winner(None, "anything") is None


def test_a_non_numeric_confidence_does_not_raise():
    """conv_state comes from parsed model JSON; a string confidence has to
    degrade to "not fired", not take down the /ask handler."""
    assert winner(_state(img="generate", img_conf="high"), "draw a tiger") is None
