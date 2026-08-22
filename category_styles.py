"""Per-category voice config (section 3.4): vocabulary, formality, banned
phrases. Config-driven so tone isn't hardcoded per message -- the LLM
prompt (or fallback templates) reads from here.

If a judge-pushed category context payload includes its own `tone` /
`banned_phrases` fields, those take precedence over this static default
(the pushed context is source-of-truth; this is just a safe baseline for
categories the judges haven't pushed context for yet).
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
    if pushed_payload:
        return {
            "tone": pushed_payload.get("tone", DEFAULT_CATEGORY_STYLES.get(category, {}).get("tone", "neutral, professional")),
            "banned_phrases": pushed_payload.get(
                "banned_phrases", DEFAULT_CATEGORY_STYLES.get(category, {}).get("banned_phrases", [])
            ),
        }
    return DEFAULT_CATEGORY_STYLES.get(category, {"tone": "neutral, professional", "banned_phrases": []})


def violates_banned_phrases(body: str, banned_phrases: list[str]) -> list[str]:
    lowered = body.lower()
    return [p for p in banned_phrases if p.lower() in lowered]
