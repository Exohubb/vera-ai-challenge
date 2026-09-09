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


_CLOCK_TIME_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM)\b|\b\d{1,2}:\d{2}\b")


def find_fabricated_clock_times(
    body: str,
    merchant_payload: dict | list | str | None = None,
    trigger_payload: dict | list | str | None = None,
    customer_payload: dict | list | str | None = None,
) -> list[str]:
    """Catches a specific fabrication mode that plain digit-token matching
    (verify_numeric_claims above) cannot: a composed message inventing a
    SPECIFIC clock time ("7am", "6:30pm") for THIS recipient's own
    appointment/slot/booking, when no such time was ever actually given
    for them -- even if the exact same time-shaped substring happens to
    appear elsewhere in context for a completely unrelated reason.

    Live-repro'd via direct testing, twice, against the real dataset:
      1. composing for a placeholder-shaped `appointment_tomorrow`
         trigger (genuinely zero time/date/service fields anywhere)
         produced "...appointment scheduled for tomorrow at 7am...". The
         bare digit "7" appeared elsewhere in the merchant payload for an
         unrelated reason (review_themes[].occurrences_30d: 7), so
         verify_numeric_claims's digit-only check passed it as grounded.
      2. after adding a first version of this check that searched ALL
         four context sources (including CategoryContext), the SAME
         fabricated "7am" passed anyway -- because gyms.json's real
         digest item legitimately mentions "Adding a 7am class typically
         doesn't cannibalize evenings" (a category-wide scheduling
         insight, not this specific customer's own appointment time).
         Category context is shared reference material across every
         merchant/customer in the vertical; it can never be evidence that
         THIS recipient's own appointment is really at that time.

    This is exactly the "hallucinated data" anti-pattern the brief
    explicitly penalizes (challenge-brief.md section 11) and
    case-studies.md's cross-case pattern #10 caps at 5/dimension
    regardless of quality otherwise -- and it matters here specifically
    because `appointment_tomorrow` is used in 2 of the official 30
    canonical test pairs (T03/T04), where the real dataset ships exactly
    the placeholder shape with no real time data.

    Deliberately excludes category_payload from the search sources: only
    merchant, trigger, and customer context can ever legitimately contain
    THIS recipient's own real appointment/slot/booking time (e.g. a real
    slot label in trigger.payload.available_slots, or a booking record on
    the merchant/customer). A genuinely grounded time is always cited
    from one of those three, so this narrower scope does not flag real
    slot citations -- only a time that was never given anywhere relevant
    to this specific recipient and was invented wholesale by the model.
    """
    times_in_body = {m.group(0) for m in _CLOCK_TIME_RE.finditer(body)}
    if not times_in_body:
        return []

    def _norm(s: str) -> str:
        return re.sub(r"\s+", "", s).lower()

    blob = " ".join(
        src if isinstance(src, str) else json.dumps(src, default=str)
        for src in (merchant_payload, trigger_payload, customer_payload) if src is not None
    )
    blob_norm = _norm(blob)
    return sorted(t for t in times_in_body if _norm(t) not in blob_norm)


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


# Offer copy that is explicitly restricted to first-time / new customers.
# Matched against the composed body, not against catalog metadata, because
# the failure mode is the WORDING promising a new-customer-only deal to
# somebody the context shows is already a returning customer.
_NEW_CUSTOMER_ONLY_RE = re.compile(
    r"\bfor\s+new\s+(?:patients?|customers?|users?|members?|clients?)\b"
    r"|\bnew[-\s]patient\s+(?:offer|special|discount|promo)\b"
    r"|\bnew[-\s](?:customer|user|member)\s+(?:offer|special|discount|promo)\b"
    r"|\bfirst[-\s]time\s+(?:patients?|customers?|users?|members?|clients?)\s+only\b",
    re.IGNORECASE,
)


def find_new_customer_offer_mismatch(body: str, customer_payload: dict | None) -> list[str]:
    """Detects a message promising a NEW-customer-only offer to a customer
    whose own context shows an existing relationship.

    Real repro (canonical pair T14, m_014_dr_asha + c_055_reyansh): the
    composed message told a patient with 5 recorded visits "We offer a Free
    Smile Analysis + Digital Scan for new patients", and the independent
    judge applied a penalty for it -- "Offering a new_user offer to a
    patient with 5 prior visits is a logical mismatch and poor
    personalization" (merchant_fit 3/10). The eligibility claim is simply
    false for this recipient, which is a correctness problem, not just a
    tone one.

    Returns a list with the offending phrase(s), or [] when there's no
    mismatch. Deliberately conservative: it only fires when the customer
    context positively establishes a prior relationship (a recorded visit
    count above zero, a last-visit date, or a non-new state), so a genuinely
    new customer can still be offered new-customer pricing.
    """
    if not isinstance(customer_payload, dict):
        return []

    relationship = customer_payload.get("relationship")
    relationship = relationship if isinstance(relationship, dict) else {}
    visits = relationship.get("visits_total")
    has_visits = isinstance(visits, (int, float)) and visits > 0
    has_last_visit = bool(relationship.get("last_visit"))
    state = str(customer_payload.get("state") or "").strip().lower()
    is_established_state = state in {
        "active", "lapsed_soft", "lapsed_hard", "churned", "loyal", "repeat",
    }

    if not (has_visits or has_last_visit or is_established_state):
        return []

    return sorted({m.group(0) for m in _NEW_CUSTOMER_ONLY_RE.finditer(body or "")})


# Detecting an asserted booking by enumerating phrasings proved to be a
# losing game -- an early version of this gate listed six templates and the
# model simply produced a seventh ("you have an appointment with us
# tomorrow", verified live). Inverted instead: treat ANY co-occurrence of a
# booking noun with a next-day reference as an assertion, then exempt the
# specific shapes that OFFER a slot rather than claim one.
_BOOKING_NOUN_RE = re.compile(
    # Nouns, plus the participle forms that assert a reservation without ever
    # using a booking noun ("we have you booked for tomorrow").
    r"\b(?:appointment|booking|slot|sitting|session|booked|scheduled|reserved)\b",
    re.IGNORECASE,
)
_NEXT_DAY_RE = re.compile(r"\btomorrow\b|\bkal\b", re.IGNORECASE)

# Offer/invitation framings. If any of these is present the message is
# proposing a time, not asserting an existing reservation, which is exactly
# the rewrite this gate is trying to steer toward -- so it must not trip.
_SLOT_OFFER_RE = re.compile(
    r"\bslot\s+(?:is\s+)?open\b"
    r"|\bopen\s+slot\b"
    r"|\bhave\s+a\s+slot\b"
    r"|\bslot\s+khaali\b"
    r"|\bkhaali\s+hai\b"
    r"|\bhold\s+it\b"
    r"|\bhold\s+(?:a|the|one)\s+(?:slot|spot|place)\b"
    r"|\brakh\s+doon\b"
    r"|\bwould\s+you\s+like\s+to\s+(?:book|come|schedule)\b"
    r"|\bshall\s+i\s+(?:book|hold|schedule)\b"
    r"|\bwant\s+me\s+to\s+(?:book|hold|schedule)\b"
    r"|\bcan\s+we\s+book\b"
    r"|\binterested\s+in\s+(?:booking|a\s+slot)\b",
    re.IGNORECASE,
)

_APPOINTMENT_DETAIL_FIELDS = (
    "appointment_time", "time", "slot_label", "appointment_date", "date",
    "service", "service_name", "slot", "booking_time", "booking_date",
)

_LAPSED_CUSTOMER_STATES = {"lapsed_hard", "churned", "lapsed"}


def find_unsupported_appointment_assertion(
    body: str,
    trigger_payload: dict | None,
    customer_payload: dict | None,
) -> list[str]:
    """Detects a message asserting a confirmed next-day appointment that
    nothing in the context actually supports.

    Real repro (canonical pair T03, trg_076_appointment_tomorrow +
    c_075_aditya): the trigger payload is the bare synthetic placeholder
    with no time, date or service, and the customer's own context says
    ``state: lapsed_hard`` with a single lifetime visit. Telling that person
    "your appointment is tomorrow" invents a booking, and the independent
    judge scored it 20/50 with a -2 penalty and trigger_relevance 2/10:
    "fabricates an appointment for tomorrow that is not supported by the
    customer state".

    Deliberately narrow, because the trigger KIND is normally sufficient
    authority that a booking exists -- this only fires when BOTH:
      * the payload carries no appointment detail whatsoever, AND
      * the customer context positively indicates a lapsed/churned
        relationship, i.e. somebody who almost certainly has no booking.
    An active customer with a bare payload is left alone; a lapsed customer
    with a real time/date/service is left alone too.
    """
    payload = trigger_payload if isinstance(trigger_payload, dict) else {}
    if any(payload.get(field) for field in _APPOINTMENT_DETAIL_FIELDS):
        return []

    if not isinstance(customer_payload, dict):
        return []
    state = str(customer_payload.get("state") or "").strip().lower()
    if state not in _LAPSED_CUSTOMER_STATES:
        return []

    text = body or ""
    booking = _BOOKING_NOUN_RE.search(text)
    if not booking or not _NEXT_DAY_RE.search(text):
        return []
    if _SLOT_OFFER_RE.search(text):
        return []
    return [booking.group(0).lower()]
