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
    leaked = []
    for co in catalog_offers:
        price = co.get("price")
        name = co.get("name", "")
        if price is None or not name:
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


def format_offer(offer: dict) -> str:
    """'Dental Cleaning' + 299 -> 'Dental Cleaning (\u20b9299)'. For a
    no-price offer, just the name/raw_title."""
    if offer.get("price") is not None:
        return f"{offer['name']} (\u20b9{offer['price']:,})"
    return offer.get("raw_title") or offer.get("name") or ""
