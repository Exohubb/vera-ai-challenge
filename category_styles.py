"""Per-category voice config (section 3.4): vocabulary, formality, banned
phrases. Config-driven so tone isn't hardcoded per message -- the LLM
prompt (or fallback templates) reads from here.

If a judge-pushed category context payload includes its own voice fields,
those take precedence over this static default (the pushed context is
source-of-truth; the static table is just a safe baseline for a category
the judge hasn't pushed context for yet, or a request that arrives before
any category context has landed).

IMPORTANT -- real dataset shape vs. flat shape: the official
CategoryContext payload (challenge-testing-brief.md 3.1,
dataset/categories/*.json) nests everything the model needs under a
`voice` object:

    payload["voice"] = {
        "tone": "peer_clinical",
        "register": "respectful_collegial",
        "code_mix": "hindi_english_natural",
        "vocab_allowed": [...],
        "vocab_taboo": [...],
        "salutation_examples": [...],
        "tone_examples": [...],
    }

An earlier version of this module read flat top-level `payload["tone"]`
and `payload["banned_phrases"]` keys instead. Those keys never exist
anywhere in the real dataset -- every real category push was silently
falling straight through to the static DEFAULT_CATEGORY_STYLES table
below, discarding the richer real voice profile entirely (the real
`peer_clinical` tone, the full 7-item taboo list, the allowed-vocabulary
list, and the worked tone_examples that show the model exactly how the
category should sound). This is a live Category Fit scoring bug, not a
cosmetic one: the judge's own rubric scores tone/vocabulary fit against
whatever CategoryContext was actually pushed, and the pushed voice detail
was never reaching the prompt.

get_category_style() now reads the real nested `voice` shape first and
falls back to a legacy flat shape (for any hand-written test fixture that
predates the real dataset) before falling back to the static default
table. `vocab_allowed` and `tone_examples` are new fields surfaced here
that didn't exist in the old return shape at all -- callers that only
read `tone`/`banned_phrases` are unaffected (dict.get with defaults), and
vera_persona.py's prompt builder now folds the two new fields in whenever
they're present, giving Nova the category's own worked examples of what
"sounding right" looks like instead of just a one-line tone description.
"""
from __future__ import annotations

DEFAULT_CATEGORY_STYLES: dict[str, dict] = {
    "dentists": {
        "tone": "clinical, reassuring, precise",
        "banned_phrases": ["guaranteed cure", "pain-free forever", "miracle"],
    },
    "salons": {
        "tone": "visual, upbeat, trend-aware",
        "banned_phrases": ["guaranteed results", "instant transformation"],
    },
    "restaurants": {
        "tone": "timely, appetite-driven, casual",
        "banned_phrases": ["best in the city", "world-famous"],
    },
    "gyms": {
        "tone": "motivational but honest, momentum-focused",
        "banned_phrases": ["guaranteed weight loss", "overnight results"],
    },
    "pharmacies": {
        "tone": "utility-first, compliant, no medical claims",
        "banned_phrases": ["cures", "guaranteed relief", "miracle drug"],
    },
}


def get_category_style(category: str, pushed_payload: dict | None = None) -> dict:
    """Returns {tone, banned_phrases, vocab_allowed, tone_examples} for
    prompt construction and post-generation banned-phrase checking.

    Precedence for each field, checked independently so a payload that
    only populates some voice fields still benefits from the ones it
    does have:
      1. The real dataset's nested `payload["voice"][...]` shape.
      2. A legacy flat `payload["tone"]` / `payload["banned_phrases"]`
         shape (kept for any hand-written fixture that predates the real
         dataset -- never present in the official dataset itself).
      3. The static per-category default table below.
      4. A generic neutral default, for a category with no static entry
         and no pushed context at all.
    """
    default = DEFAULT_CATEGORY_STYLES.get(category, {"tone": "neutral, professional", "banned_phrases": []})
    if not pushed_payload:
        return {**default, "vocab_allowed": [], "tone_examples": []}

    voice = pushed_payload.get("voice")
    voice = voice if isinstance(voice, dict) else {}

    tone = voice.get("tone") or pushed_payload.get("tone") or default.get("tone", "neutral, professional")
    banned_phrases = (
        voice.get("vocab_taboo")
        or pushed_payload.get("banned_phrases")
        or default.get("banned_phrases", [])
    )
    vocab_allowed = voice.get("vocab_allowed") or []
    tone_examples = voice.get("tone_examples") or []

    return {
        "tone": tone,
        "banned_phrases": banned_phrases,
        "vocab_allowed": vocab_allowed,
        "tone_examples": tone_examples,
    }


def violates_banned_phrases(body: str, banned_phrases: list[str]) -> list[str]:
    lowered = body.lower()
    return [p for p in banned_phrases if p.lower() in lowered]
