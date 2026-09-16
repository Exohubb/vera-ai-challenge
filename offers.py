"""Normalizes merchant offers and category offer-catalog entries into a
common {name, price, raw_title} shape.

Why this exists: the official dataset's real shapes are:
  - MerchantContext.offers:      {"id": ..., "title": "Dental Cleaning @ \u20b9299",
                                   "status": "active", "started": "..."}
  - CategoryContext.offer_catalog: {"id": ..., "title": "Dental Cleaning @ \u20b9299",
                                     "value": "299", "audience": ..., "type": ...}

Earlier code in this repo (composer.py._first_offer, fallback.py._offers)
assumed a much simpler shape -- flat "name" and "price" keys -- which
never existed in the real dataset. Against real data those functions
always returned an empty offer list, silently disabling the entire
mandatory-offer-citation system (the highest-leverage decision-quality
rule in the whole spec) for every merchant. This module parses the
"<Service Name> @ \u20b9<amount>" pattern actually used throughout the
dataset (and falls back gracefully for offers with no embedded price,
like "Free Consultation" or "3 FREE Trial Classes").
"""
from __future__ import annotations

import json
import re

# Matches "<name> @ \u20b9<amount>" allowing thousands separators, e.g.
# "Dental Cleaning @ \u20b9299", "Root Canal @ \u20b92,999 (single rooted)".
_PRICE_PATTERN = re.compile(r"^(?P<name>.+?)\s*@\s*\u20b9\s*(?P<price>[\d,]+)")
# Matches "Free Home Delivery > \u20b9499" style thresholds -- has a price
# but it's a threshold, not the offer's own price; treat as no-price offer
# (citing ">\u20b9499" as "the offer costs \u20b9499" would be a factual error).
_THRESHOLD_PATTERN = re.compile(r"[<>]\s*\u20b9")


def _parse_title(title: str) -> tuple[str, int | None]:
    """Returns (name, price). price is None when the title has no clean
    single embedded price (free offers, threshold-based offers, percentage
    discounts) -- callers must never invent a price in that case."""
    title = title.strip()
    if _THRESHOLD_PATTERN.search(title):
        return title, None
    m = _PRICE_PATTERN.match(title)
    if m:
        try:
            price = int(m.group("price").replace(",", ""))
        except ValueError:
            return title, None
        return m.group("name").strip(), price
    return title, None


def normalize_merchant_offers(merchant_payload: dict | None) -> list[dict]:
    """Returns active offers from MerchantContext.offers as
    [{"name": ..., "price": int|None, "raw_title": ..., "id": ...}, ...].
    Only offers with status=="active" are included (or offers with no
    status field at all, for hand-written test fixtures that predate the
    real dataset shape) -- expired/paused offers must never be cited as
    if they were currently available."""
    if not isinstance(merchant_payload, dict):
        return []
    offers = merchant_payload.get("offers")
    if not isinstance(offers, list):
        return []
    out = []
    for o in offers:
        if not isinstance(o, dict):
            continue
        status = o.get("status")
        if status is not None and status != "active":
            continue
        title = o.get("title") or o.get("name")
        if not title:
            continue
        if o.get("price") is not None and o.get("name"):
            # Already-normalized shape (hand-written test fixtures use
            # this directly) -- respect it as-is rather than re-parsing.
            out.append({"name": o["name"], "price": o.get("price"), "raw_title": title, "id": o.get("id")})
            continue
        name, price = _parse_title(str(title))
        out.append({"name": name, "price": price, "raw_title": str(title), "id": o.get("id")})
    return out


def normalize_category_offer_catalog(category_payload: dict | None) -> list[dict]:
    """Returns [{"name", "price", "raw_title", "id"}] from
    CategoryContext.offer_catalog. `value` is the catalog's own price
    string field (may be "0" for free services) -- prefer it over
    re-parsing the title when present and numeric."""
    if not isinstance(category_payload, dict):
        return []
    catalog = category_payload.get("offer_catalog")
    if not isinstance(catalog, list):
        return []
    out = []
    for o in catalog:
        if not isinstance(o, dict):
            continue
        title = o.get("title")
        if not title:
            continue
        price = None
        value = o.get("value")
        if isinstance(value, str) and value.isdigit():
            price = int(value)
        elif isinstance(value, (int, float)):
            price = int(value)
        if price is None:
            name, price = _parse_title(str(title))
        else:
            name, _ = _parse_title(str(title))
        out.append({"name": name, "price": price if price else None, "raw_title": str(title), "id": o.get("id")})
    return out


_SEMANTIC_STOPWORDS = {
    "active", "adult", "area", "available", "customer", "customers", "day", "days",
    "first", "free", "high", "last", "month", "offer", "package", "plan", "program",
    "service", "the", "this", "today", "trial", "week", "with", "your",
}

_SERVICE_CONCEPTS = (
    {"cleaning", "dental", "fluoride", "scaling"},
    {"pizza", "bogo"},
    {"thali", "corporate", "lunch"},
    {"kids", "yoga", "class", "classes"},
    {"membership", "gym", "class", "classes", "yoga"},
)


def _semantic_tokens(value: str) -> set[str]:
    tokens = set()
    for token in re.findall(r"[a-z0-9]+", value.lower().replace("_", " ")):
        if len(token) < 3 or token in _SEMANTIC_STOPWORDS:
            continue
        tokens.add(token)
        # Minimal plural normalization for service names (implant/implants)
        # without turning words like "classes" into invalid stems.
        if len(token) > 4 and token.endswith("s") and not token.endswith(("ss", "ses")):
            tokens.add(token[:-1])
    return tokens


def select_relevant_offer(
    merchant_payload: dict | None,
    trigger_kind: str | None = None,
    trigger_fact: str | None = None,
    trigger_payload: dict | None = None,
    customer_payload: dict | None = None,
    *,
    priced_only: bool = True,
) -> dict | None:
    """Return only an offer with positive semantic evidence.

    Direct token overlap is strongest. A small category-neutral concept
    bridge handles equivalent service wording such as fluoride/cleaning.
    Crucially, there is no first-offer fallback when relevance is zero.
    """
    offers = normalize_merchant_offers(merchant_payload)
    if priced_only:
        offers = [offer for offer in offers if offer.get("price") is not None]
    if not offers:
        return None

    source = " ".join(
        part for part in (
            trigger_kind or "",
            trigger_fact or "",
            json.dumps(trigger_payload or {}, sort_keys=True, default=str),
            json.dumps(customer_payload or {}, sort_keys=True, default=str),
        ) if part
    )
    source_tokens = _semantic_tokens(source)
    if not source_tokens:
        return None

    best_offer = None
    best_score = 0
    for offer in offers:
        offer_tokens = _semantic_tokens(str(offer.get("name", "")))
        direct_hits = offer_tokens & source_tokens
        bridge_hits = 0
        for concepts in _SERVICE_CONCEPTS:
            if source_tokens & concepts and offer_tokens & concepts:
                bridge_hits = max(bridge_hits, 1)
        score = len(direct_hits) * 3 + bridge_hits
        if score > best_score:
            best_offer, best_score = offer, score
    return best_offer if best_score > 0 else None


_PHRASE_TRIM_RE = re.compile(r"\s*(?:@|>|<|\(|\u20b9).*$")

# Words that turn a mention into a CLAIM: the offer is theirs, it exists, it
# can be booked. Everything else -- "should I draft", "how about", "want me to
# set up" -- is a proposal, which is the correct move when the merchant has
# nothing suitable, and must not be flagged.
_ASSERTS_OWNERSHIP_RE = re.compile(
    r"\byour\b|\bour\b|\byou have\b|\bwe have\b|\bavailable\b|\bbook now\b|\bbook it\b"
    r"|\bis live\b|\bcurrently (?:running|on)\b|\bdon'?t forget\b|\bclaim\b|\bredeem\b"
    r"|\baapka\b|\baapke\b|\bhamara\b|\bhamare\b",
    re.IGNORECASE,
)


def _citable_phrase(title: str) -> str:
    """The part of an offer title a message would actually say out loud.

    "Free Delivery > \u20b9500"            -> "Free Delivery"
    "Buy 1 Pizza Get 1 Free (Tue-Thu)" -> "Buy 1 Pizza Get 1 Free"
    "Dental Cleaning @ \u20b9299"          -> "Dental Cleaning"
    Needed because the observed leak said "Free Delivery", not the full
    threshold-bearing catalog title, so matching the raw title found nothing.
    """
    return _PHRASE_TRIM_RE.sub("", str(title or "")).strip(" -\u2014,;:")


def _significant_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z0-9]+", text) if len(w) > 2]


def _sentence_containing(body: str, phrase: str) -> str | None:
    """The sentence in `body` that mentions `phrase`, or None. Ownership is
    judged per sentence so a proposal in one sentence can't excuse a claim in
    another, and vice versa."""
    lowered_phrase = phrase.lower()
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", body or ""):
        if lowered_phrase in sentence.lower():
            return sentence
    return None


def find_category_offer_leakage(body: str, merchant_payload: dict | None, category_payload: dict | None) -> list[str]:
    """Detects the specific failure mode where a composed message cites a
    CATEGORY-level offer_catalog price (a generic per-vertical template
    shared across every merchant in that category, e.g. every restaurant
    sees the same "Match-night Combo @ \u20b9399") as if it were THIS
    merchant's own live offer. A number/name from offer_catalog passes
    plain fact-verification (it's real text in the context payload), but
    citing it as "your ₹399 offer" to a merchant who doesn't actually
    have that offer is a merchant-fit failure indistinguishable from
    fabrication from the merchant's point of view.

    Returns a list of category-catalog offer names/prices that were
    cited in the body AND are not present in the merchant's own offers
    list -- empty list means clean."""
    merchant_offers = normalize_merchant_offers(merchant_payload)
    merchant_prices = {str(o["price"]) for o in merchant_offers if o.get("price") is not None}
    merchant_names = {o["name"].lower() for o in merchant_offers if o.get("name")}

    catalog_offers = normalize_category_offer_catalog(category_payload)
    merchant_phrases = {
        _citable_phrase(o.get("raw_title") or o.get("name") or "").lower()
        for o in merchant_offers
    }
    merchant_phrases.discard("")
    leaked = []
    for co in catalog_offers:
        price = co.get("price")
        name = co.get("name", "")
        if price is None or not name:
            # PRICE-LESS CATALOG OFFERS WERE COMPLETELY EXEMPT before this
            # branch existed, and they are the ones that actually escaped in
            # graded runs. normalize_category_offer_catalog computes
            # `price if price else None`, so every free offer (value "0")
            # arrives here with price=None and used to be skipped outright --
            # "Free Consultation", "Free Smile Analysis + Digital Scan",
            # "Free Delivery > 500", "Free Starter on orders > 1,200",
            # "Birthday: Free Cake on parties of 6+". A live reply told a
            # dentist whose only offer is Dental Cleaning @ 299 to
            # "Book now for a free consultation!".
            #
            # These are matched on the full citable phrase rather than on
            # single words, and only when the sentence ASSERTS the offer is
            # the merchant's or is bookable. Proposing to create one is
            # correct behaviour -- for a merchant with no offers at all it is
            # the only correct behaviour -- so "should I draft a Free
            # Consultation offer?" must not trip this.
            phrase = _citable_phrase(co.get("raw_title") or name)
            if not phrase or len(_significant_words(phrase)) < 2:
                continue
            if phrase.lower() in merchant_phrases:
                continue
            sentence = _sentence_containing(body, phrase)
            if sentence is None:
                continue
            if _ASSERTS_OWNERSHIP_RE.search(sentence):
                leaked.append(phrase)
            continue
        price_cited = str(price) in body
        name_words = [w for w in name.split() if len(w) > 3]
        name_cited = any(w.lower() in body.lower() for w in name_words) if name_words else False
        if price_cited and name_cited:
            # Only a genuine leak if this exact offer isn't also in the
            # merchant's own catalog (a merchant could legitimately have
            # adopted a category-standard offer as their own).
            if str(price) not in merchant_prices and name.lower() not in merchant_names:
                leaked.append(f"{name} @ \u20b9{price}")
    return leaked


# ---------------------------------------------------------------------------
# Offers invented from nothing at all
# ---------------------------------------------------------------------------
#
# find_category_offer_leakage above catches a REAL offer attributed to the
# wrong owner. It cannot catch an offer that exists nowhere -- and that is a
# separate, observed failure mode. One roll produced, for a salon:
#
#     "... our 'Diwali Glow' package with 20% off ... for the next 30 days"
#
# No such package, no such discount, no such window exists in any pushed
# context. It slipped through every gate because verify_numeric_claims only
# asks whether the digits "20" and "30" appear SOMEWHERE in the context, and
# bare two-digit numbers almost always do (a day count, a review count, a
# days_remaining field). The offer name itself was never checked against
# anything at all. Reproduced in roughly 1 of 5 rolls.
#
# Two narrow checks, both deliberately scoped to avoid punishing good copy:
#
#   1. A NAMED offer/package that matches no title in either the merchant's
#      own catalog or the category catalog -- but only when the message also
#      quotes terms for it (a price or a discount), and only when the message
#      is asserting the offer exists rather than proposing to create one.
#      "Want me to draft a Diwali package?" is exactly the right move for a
#      merchant with no matching offer and must stay allowed; "our 'Diwali
#      Glow' package is 20% off" must not.
#   2. A DISCOUNT percentage that appears nowhere in context as a percentage.
#      Checked as a percentage rather than as a bare digit, which is the
#      specific loophole the observed failure walked through.

_OFFER_NOUN = r"(?:package|offer|combo|deal|bundle|special|scheme)"

# "'Diwali Glow' package", "\"Monsoon Reset\" offer"
_QUOTED_OFFER_NAME_RE = re.compile(
    rf"['\"\u2018\u201c]([^'\"\u2019\u201d]{{2,50}})['\"\u2019\u201d]\s*{_OFFER_NOUN}",
    re.IGNORECASE,
)
# "package called 'Diwali Glow'", "offer named \"Monsoon Reset\""
_NAMED_OFFER_RE = re.compile(
    rf"{_OFFER_NOUN}\s+(?:called|named)\s+['\"\u2018\u201c]?([^'\"\u2019\u201d.,!?]{{2,50}})",
    re.IGNORECASE,
)
# "Diwali Glow package" -- title-cased multiword name immediately before an
# offer noun. Case-sensitive on purpose: lowercase "a diwali package" is
# generic phrasing, not a product name being asserted.
_TITLECASE_OFFER_NAME_RE = re.compile(
    rf"\b((?:[A-Z][A-Za-z]{{2,}}\s+){{1,3}}){_OFFER_NOUN}\b"
)

# Proposal framings. When the offer noun sits inside one of these the message
# is offering to CREATE something, which is the correct behaviour for a
# merchant with no matching catalog entry -- never flag it.
# Kept to verbs that unambiguously mean "bring into existence", plus the
# interrogative framings Vera uses to propose work. Looser words were tried
# and rejected: "start"/"starting" exempted the exact invented-package case
# this gate exists for, because the message opened "Diwali prep is starting",
# and "plan"/"planning" collides with the festival composer's own legitimate
# "the planning window is open" phrasing. A missed proposal would only ever
# cost a false flag; a missed invention costs a fabricated commercial term.
_OFFER_PROPOSAL_RE = re.compile(
    r"\b(?:draft|drafting|create|creating|set\s+up|setting\s+up|build|building|"
    r"design|designing|put\s+together|launch|launching|prepare|preparing|"
    r"shall\s+i|should\s+i|want\s+me\s+to|would\s+you\s+like|do\s+you\s+want|"
    r"i\s+can\s+(?:draft|create|build|design|set\s+up|put\s+together)|"
    r"kar\s+doon|banau|bana\s+doon)\b",
    re.IGNORECASE,
)

_PRICE_OR_DISCOUNT_RE = re.compile(r"\u20b9\s*[\d,]+|\b\d{1,3}\s*%")

_DISCOUNT_CLAIM_RE = re.compile(
    r"(\d{1,3})\s*%\s*(?:off|discount(?:ed)?)\b"
    r"|discount\s+of\s+(\d{1,3})\s*%"
    r"|(\d{1,3})\s*%\s*(?:ki\s+)?(?:chhoot|chhut)\b",
    re.IGNORECASE,
)

_CONTEXT_PERCENT_RE = re.compile(r"(\d{1,3})(?:\.\d+)?\s*%")

_PROPOSAL_WINDOW = 60


def _known_offer_titles(merchant_payload: dict | None, category_payload: dict | None) -> list[str]:
    titles = []
    for offer in normalize_merchant_offers(merchant_payload) + normalize_category_offer_catalog(category_payload):
        for field in ("name", "raw_title"):
            value = offer.get(field)
            if value:
                titles.append(str(value))
    return titles


def _looks_like_a_proposal(body: str, match_start: int) -> bool:
    window = body[max(0, match_start - _PROPOSAL_WINDOW):match_start + 20]
    return bool(_OFFER_PROPOSAL_RE.search(window))


def find_invented_offer_names(
    body: str, merchant_payload: dict | None, category_payload: dict | None
) -> list[str]:
    """Returns named offers/packages asserted in the body that match no title
    in the merchant's own catalog or the category catalog. Empty list when the
    message names only real offers, names none at all, or is proposing to
    create one rather than claiming it already exists."""
    if not body:
        return []
    # Terms attached to the claim are what make an invented name actively
    # misleading rather than merely loose phrasing.
    if not _PRICE_OR_DISCOUNT_RE.search(body):
        return []

    known_token_sets = [_semantic_tokens(t) for t in _known_offer_titles(merchant_payload, category_payload)]
    flagged: list[str] = []
    seen: set[str] = set()

    for regex in (_QUOTED_OFFER_NAME_RE, _NAMED_OFFER_RE, _TITLECASE_OFFER_NAME_RE):
        for match in regex.finditer(body):
            candidate = (match.group(1) or "").strip(" '\"\u2018\u2019\u201c\u201d")
            if not candidate:
                continue
            candidate_tokens = _semantic_tokens(candidate)
            if not candidate_tokens:
                continue
            if any(candidate_tokens & known for known in known_token_sets):
                continue
            if _looks_like_a_proposal(body, match.start()):
                continue
            key = candidate.lower()
            if key not in seen:
                seen.add(key)
                flagged.append(candidate)
    return flagged


def find_unsupported_discount_claims(
    body: str, *context_sources: dict | list | str | None
) -> list[str]:
    """Returns discount percentages claimed in the body ("20% off", "20% ki
    chhoot") whose value appears nowhere in the context AS A PERCENTAGE.

    Matching on the percentage rather than on the bare digit is the whole
    point: a lone "20" turns up in almost any real payload, so digit-level
    verification cannot tell an invented discount from a grounded one."""
    if not body:
        return []
    claimed = []
    for match in _DISCOUNT_CLAIM_RE.finditer(body):
        value = next((g for g in match.groups() if g), None)
        if value is not None:
            claimed.append(value.lstrip("0") or value)
    if not claimed:
        return []

    blob_parts = []
    for src in context_sources:
        if src is None:
            continue
        blob_parts.append(src if isinstance(src, str) else json.dumps(src, default=str))
    blob = " ".join(blob_parts)
    supported = {m.group(1).lstrip("0") or m.group(1) for m in _CONTEXT_PERCENT_RE.finditer(blob)}

    out, seen = [], set()
    for value in claimed:
        if value in supported or value in seen:
            continue
        seen.add(value)
        out.append(f"{value}% off")
    return out


def format_offer(offer: dict) -> str:
    """'Dental Cleaning' + 299 -> 'Dental Cleaning (\u20b9299)'. For a
    no-price offer, just the name/raw_title."""
    if offer.get("price") is not None:
        return f"{offer['name']} (\u20b9{offer['price']:,})"
    return offer.get("raw_title") or offer.get("name") or ""
