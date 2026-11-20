"""?cards=simple: every golfer-facing verdict has a plain sentence, and no sentence contains a number."""

from __future__ import annotations

import re
from pathlib import Path

from swingguru.verdicts import CHECKPOINTS, _slug, unsupported_claims

APP = (Path(__file__).resolve().parents[1] / "src" / "swingguru" / "viewer" / "app.js").read_text(encoding="utf-8")
BLOCK = APP[APP.index("var SIMPLE = {"):APP.index("function simpleSentence")]
SENTENCES = dict(re.findall(r'"([a-z_.]+)":\s*"([^"]+)"', APP[APP.index("var SIMPLE = {"):APP.index("var SIMPLE_LABEL = {")]))
OBSERVATION = dict(re.findall(r'(mid_back|top|downswing):\s*"([^"]+)"', BLOCK))


def test_every_checkpoint_verdict_has_a_sentence():
    handled_in_code = re.compile(r"^top\.posture\.(slight_)?loss_of_posture$")
    for cp in CHECKPOINTS:
        for band in cp.mapping.bands:
            verdict_id = f"{cp.mapping.mapping_id}.{_slug(band.label)}"
            assert verdict_id in SENTENCES or handled_in_code.match(verdict_id), verdict_id


def test_sentences_are_plain():
    posture = re.findall(r'"(You (?:rose|stood|dipped)[^"]+)"', APP)
    for text in [*SENTENCES.values(), *OBSERVATION.values(), *posture]:
        assert not re.search(r"\d|°|%", text), text
        assert not unsupported_claims(text), text
        assert text[0].isupper() and text.rstrip().endswith((".", "{when}."))


LABELS = dict(re.findall(r'"([a-z_.]+)":\s*"([^"]+)"', APP[APP.index("var SIMPLE_LABEL = {"):APP.index("function simpleLabel")]))


def test_every_checkpoint_verdict_has_a_short_label():
    handled_in_code = re.compile(r"^top\.posture\.(slight_)?loss_of_posture$")
    for cp in CHECKPOINTS:
        for band in cp.mapping.bands:
            verdict_id = f"{cp.mapping.mapping_id}.{_slug(band.label)}"
            assert verdict_id in LABELS or handled_in_code.match(verdict_id), verdict_id
    for text in LABELS.values():
        assert not re.search(r"\d|°|%", text) and len(text) <= 24 and not unsupported_claims(text), text


def test_label_polarity_matches_the_verdict():
    # Never imply a check passed when it failed: good bands get positive wording, others don't.
    good_words = ("good", "steady", "stayed back", "on plane", "looks good")
    for cp in CHECKPOINTS:
        for band in cp.mapping.bands:
            text = LABELS.get(f"{cp.mapping.mapping_id}.{_slug(band.label)}")
            if text is None:
                continue
            positive = any(w in text.lower() for w in good_words)
            assert positive == (band.status == "good"), (band.label, band.status, text)


def test_simple_cards_and_theme_are_opt_in():
    # Builds 12-14 never send these parameters, so their replay is unchanged.
    assert re.search(r"SIMPLE_CARDS = /\[\?&\]cards=simple", APP)
    assert re.search(r"theme=\(dark\|light\)", APP)
    assert "if (SIMPLE_CARDS) {" in APP
