"""Regression tests for category_styles.py reading the REAL nested
CategoryContext voice shape (challenge-testing-brief.md 3.1,
dataset/categories/*.json) instead of a flat shape that never exists in
the official dataset.

Live-repro'd bug: get_category_style() previously read
payload["tone"]/payload["banned_phrases"] at the top level. Every real
category push nests this under payload["voice"][...] instead, so every
real category context was silently falling through to the static
5-category default table -- discarding the real tone label, the full
taboo list, the allowed-vocabulary list, and the tone_examples entirely.
This is a live Category Fit scoring bug: the judge scores tone/vocabulary
fit against whatever CategoryContext was actually pushed, and the pushed
detail was never reaching the prompt.
"""
import json
from pathlib import Path

from category_styles import get_category_style

DATASET = Path(__file__).resolve().parents[2] / "magicpin-ai-challenge" / "dataset"


def _load_category(slug: str) -> dict:
    return json.load(open(DATASET / "categories" / f"{slug}.json"))


def test_real_dentists_payload_uses_nested_voice_tone_not_static_default():
    cat = _load_category("dentists")
    style = get_category_style("dentists", cat)
    # The real payload's voice.tone is "peer_clinical" -- distinct from
    # the static DEFAULT_CATEGORY_STYLES fallback ("clinical, reassuring,
    # precise"). If the fix regresses, this will silently read the
    # static fallback instead and this assertion will fail.
    assert style["tone"] == "peer_clinical"


def test_real_dentists_payload_uses_full_real_taboo_list():
    cat = _load_category("dentists")
    style = get_category_style("dentists", cat)
    # Real voice.vocab_taboo has 7 entries; the static default table only
    # has 3 generic ones. Confirms the real (larger, more specific) list
    # is what's actually reaching the prompt/banned-phrase checker.
    real_taboo = cat["voice"]["vocab_taboo"]
    assert style["banned_phrases"] == real_taboo
    assert len(style["banned_phrases"]) == 7
    assert "doctor approved" in style["banned_phrases"]


def test_vocab_allowed_and_tone_examples_are_surfaced():
    """These two fields didn't exist in the old return shape at all --
    confirms they're now passed through so vera_persona.py's prompt can
    use them."""
    cat = _load_category("dentists")
    style = get_category_style("dentists", cat)
    assert "fluoride varnish" in style["vocab_allowed"]
    assert any("JIDA" in ex for ex in style["tone_examples"])


def test_all_five_real_categories_use_real_nested_voice():
    for slug in ("dentists", "salons", "restaurants", "gyms", "pharmacies"):
        cat = _load_category(slug)
        style = get_category_style(slug, cat)
        assert style["tone"] == cat["voice"]["tone"], f"{slug}: static fallback leaked through"
        assert style["banned_phrases"] == cat["voice"]["vocab_taboo"], f"{slug}: taboo list not real"


def test_legacy_flat_shape_still_works_for_hand_written_fixtures():
    """A hand-written test fixture predating the real dataset (flat
    top-level tone/banned_phrases, no `voice` key) must still work --
    this is the shape some earlier unit tests in this repo use directly."""
    style = get_category_style("dentists", {"tone": "custom flat tone", "banned_phrases": ["x"]})
    assert style["tone"] == "custom flat tone"
    assert style["banned_phrases"] == ["x"]


def test_no_pushed_payload_falls_back_to_static_default():
    style = get_category_style("dentists", None)
    assert style["tone"] == "clinical, reassuring, precise"
    assert style["vocab_allowed"] == []
    assert style["tone_examples"] == []


def test_unknown_category_with_no_payload_gets_neutral_default():
    style = get_category_style("car_dealers", None)
    assert style["tone"] == "neutral, professional"
    assert style["banned_phrases"] == []
