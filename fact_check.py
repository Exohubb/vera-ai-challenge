"""Post-generation fact-verification (section 3.3).

Extracts numeric claims (currency amounts, percentages, plain numbers,
week-search-counts) from a composed message body and verifies each one
appears verbatim somewhere in the source context payloads. If any number
in the message can't be traced back to the context, verification fails
and the caller should regenerate or fall back.
"""
from __future__ import annotations

import json
import re

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _extract_numbers(text: str) -> set[str]:
    """Extract normalized numeric tokens from text (strip commas)."""
    return {m.group(0).replace(",", "") for m in _NUMBER_RE.finditer(text)}


def _extract_source_numbers(*sources: dict | list | str | None) -> set[str]:
    found: set[str] = set()
    for src in sources:
        if src is None:
            continue
        blob = src if isinstance(src, str) else json.dumps(src)
        found |= _extract_numbers(blob)
    return found


def verify_numeric_claims(body: str, *context_sources: dict | list | str | None) -> tuple[bool, list[str]]:
    """Returns (is_valid, unverifiable_numbers). A number in the body is
    considered valid if it appears verbatim (after comma-stripping) in any
    of the supplied context sources."""
    body_numbers = _extract_numbers(body)
    if not body_numbers:
        return True, []
    source_numbers = _extract_source_numbers(*context_sources)
    unverifiable = sorted(n for n in body_numbers if n not in source_numbers)
    return (len(unverifiable) == 0), unverifiable


_IMPERATIVE_CTA_RE = re.compile(
    r"\breply\s+(\d|yes|no|stop|confirm)\b|\bwant me to\b|\blet me know\b",
    re.IGNORECASE,
)

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def find_urls(body: str) -> list[str]:
    """The official testing brief's own failure-mode table (Example F.4)
    treats ANY url in a message body as a hard fail ("Meta would reject
    it", -3 penalty), even though challenge-brief.md's own §5 constraint
    reads more permissively ("URLs allowed when they add clear value").
    Since nothing in the actual dataset ever supplies a real URL to cite
    verbatim, any URL that shows up in a composed message is necessarily
    invented by the model -- so this doubles as both a fabrication check
    and the stricter of the two documented rules. Returns the list of
    URL-like substrings found (empty list = clean)."""
    return _URL_RE.findall(body or "")


def count_ctas(body: str) -> int:
    """Rough heuristic: count distinct asks. Most CTAs end in a question
    mark, but multi_choice booking/slot CTAs are often phrased as an
    imperative with no "?" at all -- e.g. case study 2's gold example:
    "Reply 1 for Wed, 2 for Thu, or tell us a time that works." (zero "?",
    one clear CTA). Count "?" as usual, but if there are none, check for
    an imperative CTA phrasing and count that as exactly one ask instead
    of zero -- otherwise a perfectly good multi_choice message fails the
    "has no CTA" quality gate."""
    q_count = body.count("?")
    if q_count > 0:
        return q_count
    return 1 if _IMPERATIVE_CTA_RE.search(body) else 0
