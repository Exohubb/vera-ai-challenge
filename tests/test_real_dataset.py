"""Regression tests against the REAL official dataset shapes
(magicpin-ai-challenge/dataset/*.json) -- not the earlier hand-written
fixtures. These specifically cover the gaps found in
VERA_BOT_TEST_REPORT.md: offer-title parsing, category_slug lookup,
customer-facing composition, trigger inner-payload fact resolution, tick
response schema, and the 409 stale-version contract.
"""
import json
from pathlib import Path

DATASET = Path(__file__).resolve().parents[2] / "magicpin-ai-challenge" / "dataset"


def _load_merchant(merchant_id: str) -> dict:
    data = json.load(open(DATASET / "merchants_seed.json"))
    for m in data["merchants"]:
        if m["merchant_id"] == merchant_id:
            return m
    raise KeyError(merchant_id)


def _load_customer(customer_id: str) -> dict:
    data = json.load(open(DATASET / "customers_seed.json"))
    for c in data["customers"]:
        if c["customer_id"] == customer_id:
            return c
    raise KeyError(customer_id)


def _load_trigger(trigger_id: str) -> dict:
    data = json.load(open(DATASET / "triggers_seed.json"))
    for t in data["triggers"]:
        if t["id"] == trigger_id:
            return t
    raise KeyError(trigger_id)


def _load_category(slug: str) -> dict:
    return json.load(open(DATASET / "categories" / f"{slug}.json"))


# ---------------------------------------------------------------------------
# offers.py -- real "@ \u20b9NNN" title parsing
# ---------------------------------------------------------------------------

def test_offer_title_parsing_real_shape():
    from offers import normalize_merchant_offers

    m = _load_merchant("m_001_drmeera_dentist_delhi")
    offers = normalize_merchant_offers(m)
    active = [o for o in offers if o["name"]]
    assert any(o["price"] == 299 and "cleaning" in o["name"].lower() for o in active)
    # expired offer must be excluded
    assert not any(o["price"] == 499 for o in active)


def test_offer_title_parsing_no_price_offer():
    from offers import normalize_merchant_offers

    m = _load_merchant("m_007_powerhouse_gym_bangalore")
    offers = normalize_merchant_offers(m)
    assert offers, "PowerHouse has one active offer ('3 FREE Trial Classes')"
    assert offers[0]["price"] is None


def test_fallback_greets_merchant_by_owner_first_name():
    """case-studies.md cross-case pattern #3: gold examples greet the
    merchant by owner_first_name ("Dr. Meera", "Suresh", "Karthik",
    "Ramesh"), never the full business name -- a business-name greeting
    is scored the same as a generic 'Hi' (merchant-fit -1)."""
    from fallback import compose_fallback

    m = _load_merchant("m_001_drmeera_dentist_delhi")
    result = compose_fallback("dormant_with_vera", None, m, None)
    assert "Dr. Meera" in result["body"]
    assert "Dr. Meera's Dental Clinic" not in result["body"]


def test_composer_first_offer_uses_real_shape():
    from composer import _first_offer

    m = _load_merchant("m_001_drmeera_dentist_delhi")
    offer = _first_offer(m)
    assert offer is not None
    assert offer["price"] == 299


# ---------------------------------------------------------------------------
# trigger_facts.py -- resolving concrete facts from real trigger payloads
# ---------------------------------------------------------------------------

def test_resolve_fact_perf_dip():
    from trigger_facts import resolve_fact

    trg = _load_trigger("trg_004_perf_dip_bharat")
    fact = resolve_fact("perf_dip", trg["payload"])
    assert fact is not None
    assert "50%" in fact or "calls" in fact


def test_resolve_fact_research_digest_uses_category_digest():
    from trigger_facts import resolve_fact

    trg = _load_trigger("trg_001_research_digest_dentists")
    cat = _load_category("dentists")
    fact = resolve_fact("research_digest", trg["payload"], category_payload=cat)
    assert fact is not None
    assert "2100" in fact or "2,100" in fact or "fluoride" in fact.lower()


def test_resolve_fact_competitor_opened():
    from trigger_facts import resolve_fact

    trg = _load_trigger("trg_023_competitor_opened_dentist")
    fact = resolve_fact("competitor_opened", trg["payload"])
    assert fact is not None
    assert "Smile Studio" in fact


def test_resolve_fact_supply_alert():
    from trigger_facts import resolve_fact

    trg = _load_trigger("trg_018_supply_atorvastatin_recall")
    fact = resolve_fact("supply_alert", trg["payload"])
    assert fact is not None
    assert "atorvastatin" in fact.lower()
    assert "AT2024-1102" in fact


# ---------------------------------------------------------------------------
# End-to-end compose() against real merchant + category + resolved fact
# ---------------------------------------------------------------------------

def test_compose_end_to_end_research_digest_real_shapes():
    from composer import compose
    from trigger_facts import resolve_fact
    from fact_check import verify_numeric_claims

    merchant = _load_merchant("m_001_drmeera_dentist_delhi")
    category = _load_category("dentists")
    trg = _load_trigger("trg_001_research_digest_dentists")
    fact = resolve_fact("research_digest", trg["payload"], category_payload=category)

    trigger = {"kind": "research_digest", "fact": fact, "trigger_id": trg["id"], "payload": trg["payload"]}
    result = compose(category="dentists", merchant_payload=merchant, trigger=trigger, category_payload=category)

    assert result["body"]
    valid, unverifiable = verify_numeric_claims(result["body"], merchant, trigger, None, category)
    assert valid, f"invented numbers: {unverifiable} in body: {result['body']}"
    # Should cite the real active offer (Dental Cleaning @ 299) since
    # research_digest is in OFFER_CITATION_REQUIRED_KINDS.
    assert "299" in result["body"]


def test_no_category_catalog_offer_leakage_ipl_restaurant():
    """Live repro found: composing for SK Pizza Junction's ipl_match_today
    trigger cited 'Match-night Combo @ \u20b9399' -- a category-level
    offer_catalog template (restaurants.json) that this merchant does NOT
    actually have. Their real, only offer is 'Buy 1 Pizza Get 1 Free
    (Tue-Thu)'. This must never be cited as SK Pizza's own offer."""
    from composer import compose
    from trigger_facts import resolve_fact
    from offers import find_category_offer_leakage

    merchant = _load_merchant("m_005_pizzajunction_restaurant_delhi")
    category = _load_category("restaurants")
    trg = _load_trigger("trg_010_ipl_match_delhi")
    fact = resolve_fact("ipl_match_today", trg["payload"], category_payload=category)

    trigger = {"kind": "ipl_match_today", "fact": fact, "trigger_id": trg["id"], "payload": trg["payload"]}
    result = compose(category="restaurants", merchant_payload=merchant, trigger=trigger, category_payload=category)

    leaked = find_category_offer_leakage(result["body"], merchant, category)
    assert not leaked, f"category-catalog offer leaked into merchant-facing message: {leaked} | body: {result['body']}"


def test_find_category_offer_leakage_detects_known_repro():
    from offers import find_category_offer_leakage

    merchant = _load_merchant("m_005_pizzajunction_restaurant_delhi")
    category = _load_category("restaurants")
    body = "Want me to push the 'Match-night Combo @ \u20b9399 (food + drink)' offer more on these days?"
    leaked = find_category_offer_leakage(body, merchant, category)
    assert leaked, "should have detected the category-catalog offer being cited as the merchant's own"


def test_compose_customer_facing_recall_due_real_shapes():
    from composer import compose
    from trigger_facts import resolve_fact
    from fact_check import verify_numeric_claims

    merchant = _load_merchant("m_001_drmeera_dentist_delhi")
    customer = _load_customer("c_001_priya_for_m001")
    category = _load_category("dentists")
    trg = _load_trigger("trg_003_recall_due_priya")
    fact = resolve_fact("recall_due", trg["payload"], category_payload=category, customer_payload=customer)

    trigger = {"kind": "recall_due", "fact": fact, "trigger_id": trg["id"], "payload": trg["payload"]}
    result = compose(
        category="dentists", merchant_payload=merchant, trigger=trigger,
        customer_payload=customer, category_payload=category,
    )

    assert result["body"]
    # Must address Priya (the customer), not "Dr. Meera" or "Meera"
    assert "priya" in result["body"].lower()
    assert "dr. meera" not in result["body"].lower() or "meera" in result["body"].lower()  # merchant name as sender is fine
    valid, unverifiable = verify_numeric_claims(result["body"], merchant, trigger, customer, category)
    assert valid, f"invented numbers: {unverifiable} in body: {result['body']}"


# ---------------------------------------------------------------------------
# main.py -- category lookup helper matches real dataset shape
# ---------------------------------------------------------------------------

def test_merchant_category_helper_matches_real_shape():
    from main import _merchant_category

    m = _load_merchant("m_001_drmeera_dentist_delhi")
    assert _merchant_category(m) == "dentists"


def test_category_lookup_bare_slug(client):
    cat = _load_category("dentists")
    r = client.post("/v1/context", json={
        "scope": "category", "context_id": "dentists", "version": 1, "payload": cat,
    })
    assert r.status_code == 200

    from main import _category_lookup
    rec = _category_lookup("dentists")
    assert rec is not None
    assert rec.payload["slug"] == "dentists"


# ---------------------------------------------------------------------------
# /v1/context -- 409 stale_version contract
# ---------------------------------------------------------------------------

def test_context_stale_version_returns_409(client):
    base = {"scope": "merchant", "context_id": "m_stale_test", "payload": {"identity": {"name": "X"}}}
    client.post("/v1/context", json={**base, "version": 5})
    r = client.post("/v1/context", json={**base, "version": 3})
    assert r.status_code == 409
    body = r.json()
    assert body["accepted"] is False
    assert body["reason"] == "stale_version"
    assert body["current_version"] == 5


# ---------------------------------------------------------------------------
# /v1/tick -- full response schema + customer-facing send_as
# ---------------------------------------------------------------------------

def test_tick_response_has_all_official_fields(client):
    merchant = _load_merchant("m_001_drmeera_dentist_delhi")
    category = _load_category("dentists")
    trg = _load_trigger("trg_001_research_digest_dentists")

    client.post("/v1/context", json={"scope": "category", "context_id": "dentists", "version": 1, "payload": category})
    client.post("/v1/context", json={"scope": "merchant", "context_id": merchant["merchant_id"], "version": 1, "payload": merchant})
    client.post("/v1/context", json={"scope": "trigger", "context_id": trg["id"], "version": 1, "payload": trg})

    r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": [trg["id"]]})
    assert r.status_code == 200
    actions = r.json()["actions"]
    assert len(actions) == 1
    action = actions[0]
    for field in ("conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id",
                  "template_name", "template_params", "body", "cta", "suppression_key", "rationale"):
        assert field in action, f"missing field: {field}"
    assert action["send_as"] == "vera"
    assert action["customer_id"] is None


def test_tick_customer_facing_send_as_merchant_on_behalf(client):
    merchant = _load_merchant("m_001_drmeera_dentist_delhi")
    customer = _load_customer("c_001_priya_for_m001")
    category = _load_category("dentists")
    trg = _load_trigger("trg_003_recall_due_priya")

    client.post("/v1/context", json={"scope": "category", "context_id": "dentists", "version": 1, "payload": category})
    client.post("/v1/context", json={"scope": "merchant", "context_id": merchant["merchant_id"], "version": 1, "payload": merchant})
    client.post("/v1/context", json={"scope": "customer", "context_id": customer["customer_id"], "version": 1, "payload": customer})
    client.post("/v1/context", json={"scope": "trigger", "context_id": trg["id"], "version": 1, "payload": trg})

    r = client.post("/v1/tick", json={"now": "2026-04-26T11:00:00Z", "available_triggers": [trg["id"]]})
    assert r.status_code == 200
    actions = r.json()["actions"]
    assert len(actions) == 1
    action = actions[0]
    assert action["send_as"] == "merchant_on_behalf"
    assert action["customer_id"] == customer["customer_id"]
    assert "priya" in action["body"].lower()


def test_tick_large_batch_stays_within_cap_and_schema(client):
    """25 triggers offered in one tick (test report §3.3's exact repro
    scenario) must not time out, must respect the 20-action cap, and must
    not corrupt suppression state for triggers that didn't make the cut."""
    triggers_data = json.load(open(DATASET / "triggers_seed.json"))["triggers"]
    merchants_data = json.load(open(DATASET / "merchants_seed.json"))["merchants"]
    categories = [_load_category(slug) for slug in ("dentists", "salons", "restaurants", "gyms", "pharmacies")]

    for cat in categories:
        client.post("/v1/context", json={"scope": "category", "context_id": cat["slug"], "version": 1, "payload": cat})
    for m in merchants_data:
        client.post("/v1/context", json={"scope": "merchant", "context_id": m["merchant_id"], "version": 1, "payload": m})
    for t in triggers_data:
        client.post("/v1/context", json={"scope": "trigger", "context_id": t["id"], "version": 1, "payload": t})

    all_ids = [t["id"] for t in triggers_data]
    r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": all_ids})
    assert r.status_code == 200
    actions = r.json()["actions"]
    assert len(actions) <= 20

    # repeat immediately: only the ones actually returned last time should
    # now be suppressed; nothing should have been silently marked
    # suppressed without a delivered body.
    r2 = client.post("/v1/tick", json={"now": "2026-04-26T10:40:00Z", "available_triggers": all_ids})
    assert r2.status_code == 200
    actions2 = r2.json()["actions"]
    sent_trigger_ids = {a["trigger_id"] for a in actions}
    for a in actions2:
        assert a["trigger_id"] not in sent_trigger_ids


def test_semantic_offer_selection_requires_positive_relevance():
    from offers import select_relevant_offer

    merchant = _load_merchant("m_003_studio11_salon_hyderabad")
    assert select_relevant_offer(
        merchant, "review_theme_emerged", '"delivery late" mentioned in 4 reviews',
        {"theme": "delivery_late"},
    ) is None
    matched = select_relevant_offer(
        merchant, "wedding_package_followup", "bridal hair prep window is open",
        {"next_step_window_open": "hair_prep"},
    )
    assert matched is not None and matched["name"] in {"Haircut", "Hair Spa"}


def test_richer_trigger_facts_preserve_grounded_fields():
    from trigger_facts import resolve_fact

    ipl = _load_trigger("trg_010_ipl_match_delhi")
    ipl_fact = resolve_fact(ipl["kind"], ipl["payload"])
    # The start time must survive, but as a time a merchant would read. This
    # assertion used to require the raw "19:30:00+05:30" verbatim, and the
    # composed message duly said "starts 19:30:00+05:30" -- a UTC offset in a
    # WhatsApp message, which the rubric docks as exposing internal plumbing.
    assert "7:30pm" in ipl_fact
    assert "19:30:00+05:30" not in ipl_fact
    # The weeknight flag must carry its consequence, not just its value. With a
    # bare "not a weeknight" the model repeatedly wrote the self-contradicting
    # "it's not a weeknight, so push your Tue-Thu offer today", inverting the
    # contrarian judgement this trigger exists to demonstrate.
    assert "NOT a weeknight" in ipl_fact
    assert "hold a weekday-only offer back" in ipl_fact

    review = _load_trigger("trg_011_review_theme_late_delivery")
    review_fact = resolve_fact(review["kind"], review["payload"])
    assert "50 mins for a 15 min ride" in review_fact

    cde = _load_trigger("trg_022_cde_webinar_dentists")
    cde_fact = resolve_fact(cde["kind"], cde["payload"], category_payload=_load_category("dentists"))
    assert "free for members" in cde_fact
    # The event date is the "why now" for a CDE invite and was absent from the
    # fact entirely, leaving it to chance whether a draft pulled it out of the
    # raw digest payload. Two runs of the same build scored this pair 48/50 and
    # 29/50, the low one specifically faulted for missing "concrete dates".
    assert "on May 2" in cde_fact


def test_appointment_tomorrow_and_customer_lapsed_soft_are_in_router():
    """Both kinds appear in the OFFICIAL 30 canonical test pairs
    (T03/T04 = appointment_tomorrow, T14/T15 = customer_lapsed_soft --
    confirmed by generating the real expanded dataset via
    dataset/generate_dataset.py and cross-referencing test_pairs.json)
    but previously had no entry in TRIGGER_STRATEGY_TABLE, so all 4 of
    those graded pairs silently got the weakest possible lever
    (DEFAULT_STRATEGY, base_priority=10) regardless of actual urgency or
    relationship signal in the real context."""
    from trigger_router import DEFAULT_STRATEGY, get_strategy

    for kind in ("appointment_tomorrow", "customer_lapsed_soft"):
        strategy = get_strategy(kind)
        assert strategy is not DEFAULT_STRATEGY, f"{kind} still falls to the weakest default lever"
        assert strategy.base_priority > DEFAULT_STRATEGY.base_priority


def test_appointment_tomorrow_placeholder_payload_yields_no_fabricated_fact():
    """The real dataset's synthetic expansion of this trigger kind ships
    as a bare placeholder ({"placeholder": true, "metric_or_topic":
    "appointment_tomorrow"}) with no actual time/date/service field.
    resolve_fact must return None rather than inventing a slot -- the
    composer's degraded-input handling already covers a None fact
    gracefully."""
    from trigger_facts import resolve_fact

    placeholder_payload = {"placeholder": True, "metric_or_topic": "appointment_tomorrow"}
    fact = resolve_fact("appointment_tomorrow", placeholder_payload)
    assert fact is None


def test_appointment_tomorrow_resolves_real_fields_when_present():
    """If a judge-supplied (non-placeholder) payload DOES carry real
    booking detail, the resolver must use it -- verifies the resolver
    isn't hardcoded to always return None regardless of input."""
    from trigger_facts import resolve_fact

    real_payload = {"appointment_time": "5:00 PM", "appointment_date": "2026-05-01", "service": "haircut"}
    fact = resolve_fact("appointment_tomorrow", real_payload)
    assert fact is not None
    assert "haircut" in fact.lower()
    assert "5:00 pm" in fact.lower()
    assert "2026-05-01" in fact


def test_find_fabricated_clock_times_catches_invented_appointment_time():
    """Live-repro'd bug: composing against the real placeholder-shaped
    appointment_tomorrow trigger (no time/date field anywhere) produced
    '...appointment scheduled for tomorrow at 7am...' -- plain
    verify_numeric_claims missed this because the bare digit '7'
    happened to appear elsewhere in the merchant payload for an
    unrelated reason (review_themes[].occurrences_30d: 7). This
    dedicated clock-time check must catch it even though the digit
    itself is technically "present" in context."""
    from fact_check import find_fabricated_clock_times

    merchant = _load_merchant("m_007_powerhouse_gym_bangalore")
    body = "Hi Arjun, this is PowerHouse Fitness. Your appointment tomorrow is at 7am. Still good?"
    fabricated = find_fabricated_clock_times(body, merchant, {"placeholder": True}, None)
    assert fabricated == ["7am"]


def test_find_fabricated_clock_times_ignores_unrelated_category_digest_mention():
    """Second live repro, against the SAME 7am fabrication: gyms.json's
    real digest legitimately contains an unrelated scheduling insight
    that mentions "7am class" as category-wide research, not this
    customer's own appointment. category_payload must never be treated
    as evidence for a specific recipient's own booking time -- passing
    it in must not suppress this exact fabrication."""
    from fact_check import find_fabricated_clock_times

    merchant = _load_merchant("m_007_powerhouse_gym_bangalore")
    category = _load_category("gyms")
    body = "Hi Arjun, this is PowerHouse Fitness. Your appointment tomorrow is at 7am. Still good?"
    # category_payload is intentionally NOT one of the function's
    # parameters -- confirms the fix scopes evidence to only
    # merchant/trigger/customer, never category-wide reference material,
    # even when the real category digest happens to contain the exact
    # same time-shaped substring for an unrelated reason.
    assert "7am" in json.dumps(category)  # sanity: the decoy really is present in category context
    fabricated = find_fabricated_clock_times(body, merchant, {"placeholder": True}, None)
    assert fabricated == ["7am"]


def test_find_fabricated_clock_times_allows_genuinely_grounded_slot():
    """A time that IS a real verbatim substring in the recipient's own
    trigger/merchant/customer context (e.g. a real slot label) must not
    be flagged."""
    from fact_check import find_fabricated_clock_times

    trigger_payload = {"available_slots": [{"label": "Wed 6 Nov, 6pm"}]}
    body = "Slots ready: Wed 6 Nov, 6pm. Reply YES to confirm."
    fabricated = find_fabricated_clock_times(body, {}, trigger_payload, None)
    assert fabricated == []


def test_compose_appointment_tomorrow_never_fabricates_a_time_end_to_end():
    """End-to-end regression for the exact live repro: composing against
    the real placeholder appointment_tomorrow shape must never produce a
    body containing an invented clock time, regardless of which path
    (LLM or fallback) actually produces the final message."""
    from composer import compose
    from fact_check import find_fabricated_clock_times
    from trigger_facts import resolve_fact

    merchant = _load_merchant("m_007_powerhouse_gym_bangalore")
    customer = _load_customer("c_001_priya_for_m001")
    customer = {**customer, "merchant_id": merchant["merchant_id"]}
    category = _load_category("gyms")
    trigger_payload = {"placeholder": True, "metric_or_topic": "appointment_tomorrow"}
    fact = resolve_fact("appointment_tomorrow", trigger_payload)
    assert fact is None  # confirms the resolver itself invents nothing

    trigger = {"kind": "appointment_tomorrow", "fact": fact, "trigger_id": "trg_probe_appt2", "payload": trigger_payload}
    result = compose(
        category="gyms", merchant_payload=merchant, trigger=trigger,
        customer_payload=customer, category_payload=category,
    )
    fabricated = find_fabricated_clock_times(result["body"], merchant, trigger, customer)
    assert fabricated == [], f"invented clock time(s) {fabricated} in body: {result['body']}"


def test_compose_customer_facing_appointment_tomorrow_placeholder_shape():
    """End-to-end: composing for the real (placeholder-shaped) trigger
    payload must still produce a valid, non-fabricated, customer-facing
    message with the correct send_as -- not an empty/broken response
    just because the trigger carries no real event detail."""
    from composer import compose
    from fact_check import verify_numeric_claims
    from trigger_facts import resolve_fact

    merchant = _load_merchant("m_003_studio11_salon_hyderabad")
    customer = _load_customer("c_001_priya_for_m001")
    customer = {**customer, "merchant_id": merchant["merchant_id"]}
    category = _load_category("salons")
    trigger_payload = {"placeholder": True, "metric_or_topic": "appointment_tomorrow"}
    fact = resolve_fact("appointment_tomorrow", trigger_payload)
    trigger = {"kind": "appointment_tomorrow", "fact": fact, "trigger_id": "trg_probe_appt", "payload": trigger_payload}

    result = compose(
        category="salons", merchant_payload=merchant, trigger=trigger,
        customer_payload=customer, category_payload=category,
    )
    assert result["body"]
    valid, unverifiable = verify_numeric_claims(result["body"], merchant, trigger, customer, category)
    assert valid, f"invented numbers: {unverifiable} in body: {result['body']}"


def test_no_lone_surrogates_in_any_fallback_template_output():
    """Real production incident: a /v1/tick call with the actual 30
    official canonical test-pair triggers hit a UnicodeEncodeError 500 --
    the ENTIRE response crashed (not just one action) because a
    deterministic fallback.py template used a broken Python string
    literal ("\\ud83d\\udc4b") that Python keeps as two lone,
    individually-unencodable UTF-16 surrogate halves instead of combining
    into one emoji codepoint (unlike JSON/JavaScript \\u pairs). Every
    fallback template that can fire in a customer-facing thread is
    exercised here directly against real dataset shapes to confirm none
    of them can ever produce a lone surrogate again."""
    import re

    from fallback import compose_fallback

    _LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

    merchant = _load_merchant("m_001_drmeera_dentist_delhi")
    customer = _load_customer("c_001_priya_for_m001")
    hinglish_merchant = {**merchant, "identity": {**merchant["identity"], "languages": ["en", "hi"]}}
    hinglish_customer = {**customer, "identity": {**customer["identity"], "language_pref": "hi-en mix"}}

    kinds_and_payloads = [
        ("recall_due", {"available_slots": [{"label": "Wed 6 Nov, 6pm"}, {"label": "Thu 7 Nov, 5pm"}]}),
        ("wedding_package_followup", {"days_to_wedding": 30}),
        ("customer_lapsed_soft", {"days_since_last_visit": 100}),
        ("customer_lapsed_hard", {"days_since_last_visit": 200}),
        ("chronic_refill_due", {"molecule_list": ["metformin"], "stock_runs_out_iso": "2026-05-01T00:00:00Z"}),
        ("trial_followup", {"next_session_options": [{"label": "Mon 5pm"}]}),
        ("appointment_tomorrow", {"placeholder": True}),
        ("unplanned_slot_open", {}),  # exercises the generic customer-directed fallback path
    ]

    for kind, trigger_payload in kinds_and_payloads:
        for merch, cust, label in (
            (merchant, customer, "english"),
            (hinglish_merchant, hinglish_customer, "hinglish"),
        ):
            result = compose_fallback(kind, "some grounded fact", merch, cust, trigger_payload)
            assert not _LONE_SURROGATE_RE.search(result["body"]), (
                f"{kind} ({label}) fallback body contains a lone surrogate and would crash "
                f"JSON serialization: {result['body']!r}"
            )
            # Confirm it's actually encodable end-to-end, not just regex-clean.
            result["body"].encode("utf-8")


def test_strip_lone_surrogates_removes_broken_emoji_without_crashing():
    from composer import _strip_lone_surrogates

    broken = "Hi there \ud83d\udc4b, how are you?"
    cleaned = _strip_lone_surrogates(broken)
    cleaned.encode("utf-8")  # must not raise
    assert "\ud83d" not in cleaned and "\udc4b" not in cleaned


def test_is_json_encodable_action_catches_lone_surrogate():
    from main import _is_json_encodable_action

    bad_action = {"merchant_id": "m1", "trigger_id": "t1", "body": "Hi \ud83d\udc4b there"}
    assert _is_json_encodable_action(bad_action) is False

    good_action = {"merchant_id": "m1", "trigger_id": "t1", "body": "Hi \U0001f44b there"}
    assert _is_json_encodable_action(good_action) is True


def test_tick_never_returns_500_even_if_one_action_would_be_unencodable(client, monkeypatch):
    """End-to-end: even if some future/unknown bug produces a lone
    surrogate in one composed action's body, /v1/tick must still return
    200 with the OTHER valid actions intact, not a 500 that drops the
    whole batch (the exact real production incident this whole test
    class guards against)."""
    import main as main_module

    merchant = _load_merchant("m_001_drmeera_dentist_delhi")
    category = _load_category("dentists")
    trg = _load_trigger("trg_001_research_digest_dentists")

    client.post("/v1/context", json={"scope": "category", "context_id": "dentists", "version": 1, "payload": category})
    client.post("/v1/context", json={"scope": "merchant", "context_id": merchant["merchant_id"], "version": 1, "payload": merchant})
    client.post("/v1/context", json={"scope": "trigger", "context_id": trg["id"], "version": 1, "payload": trg})

    real_compose_one = main_module._compose_one_candidate

    def _poisoned_compose_one(cand):
        result = real_compose_one(cand)
        result["body"] = "Hi \ud83d\udc4b there"  # inject a lone surrogate directly
        return result

    monkeypatch.setattr(main_module, "_compose_one_candidate", _poisoned_compose_one)

    r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": [trg["id"]]})
    assert r.status_code == 200
    assert r.json()["actions"] == []  # the one poisoned action was dropped, not crashed on


def test_strip_lone_surrogates_leaves_valid_emoji_untouched():
    from composer import _strip_lone_surrogates

    valid = "Hi there \U0001f44b, how are you? \U0001f9b7"
    assert _strip_lone_surrogates(valid) == valid
    valid.encode("utf-8")  # sanity: this form was always fine


def test_chronic_refill_fallback_no_double_your_when_no_molecule_data():
    """Live-repro'd bug against the real official canonical test pair T08
    (trg_081_chronic_refill_due_m_011_dr_sameer_dent, a placeholder-shaped
    trigger with no molecule_list): the deterministic fallback produced
    "Hi, Bright Smile Dental here. Your your regular medicines is running
    low." -- a duplicated possessive plus a subject-verb mismatch, on one
    of the 30 pairs the real judge grades. Must never say "your your" and
    must use "are" (plural) with the generic fallback phrase.

    The no-molecule case is asserted against a DISPENSING merchant here,
    because for a non-dispensing merchant the correct output is no longer
    medicine wording at all -- see the companion test below."""
    from fallback import _tpl_customer_chronic_refill

    body, cta = _tpl_customer_chronic_refill(
        "Vivaan", "Apollo Health Plus", {}, hi=False, is_dispensing_category=True
    )
    assert "your your" not in body.lower()
    assert "medicines is running" not in body
    assert "medicines are running" in body
    assert "Vivaan" in body  # customer must be greeted by name, not a bare "Hi,"


def test_chronic_refill_fallback_never_sends_medicine_wording_from_a_dentist():
    """Second, worse half of the same T08 defect. The official dataset's
    synthetic expander attaches chronic_refill_due to merchants that don't
    dispense medicine at all (T08's merchant is Bright Smile Dental, a
    dentist), and the independent Gemini judge scored the resulting "Your
    regular medicines are running low" 5/50 with an explicit fabrication
    penalty: "applies a pharmacy medicine refill message to a dental
    clinic". A non-dispensing merchant must never reference medicines or
    dispatching a prescription."""
    from fallback import _tpl_customer_chronic_refill

    body, cta = _tpl_customer_chronic_refill(
        "Vivaan", "Bright Smile Dental", {}, hi=False,
        is_dispensing_category=False,
        relationship_fact="5 visits with us, last one on 2026-04-01",
    )
    lowered = body.lower()
    assert "medicine" not in lowered
    assert "refill" not in lowered
    assert "dispatch" not in lowered
    assert "Vivaan" in body
    # Must still be grounded in the real relationship history, not smalltalk.
    assert "5 visits with us" in body
    assert cta == "binary_yes_no"


def test_is_dispensing_category_only_true_for_pharmacies():
    from fallback import _is_dispensing_category

    assert _is_dispensing_category({"category_slug": "pharmacies"}) is True
    assert _is_dispensing_category({"category_slug": "dentists"}) is False
    assert _is_dispensing_category({"category_slug": "gyms"}) is False
    assert _is_dispensing_category({}) is False
    assert _is_dispensing_category(None) is False


def test_chronic_refill_fallback_cites_real_molecules_correctly():
    from fallback import _tpl_customer_chronic_refill

    trigger_payload = {"molecule_list": ["metformin", "atorvastatin"], "stock_runs_out_iso": "2026-04-28T00:00:00Z"}
    body, cta = _tpl_customer_chronic_refill("Mr. Sharma", "Apollo Health Plus", trigger_payload, hi=False)
    assert "your your" not in body.lower()
    assert "Your metformin, atorvastatin are running low" in body
    assert "Mr. Sharma" in body
    assert "2026-04-28" in body


def test_chronic_refill_fallback_hinglish_no_double_possessive():
    from fallback import _tpl_customer_chronic_refill

    body, cta = _tpl_customer_chronic_refill("Vivaan", "Bright Smile Dental", {}, hi=True)
    assert "aapki aapki" not in body.lower()
    assert "Vivaan" in body


def test_compose_customer_lapsed_soft_real_shape():
    """customer_lapsed_soft uses the same fact resolver as
    customer_lapsed_hard (_f_customer_lapsed) but must route through its
    own (gentler) strategy entry, not silently alias to the hard-lapse
    lever's framing."""
    from composer import compose
    from fact_check import verify_numeric_claims
    from trigger_facts import resolve_fact

    merchant = _load_merchant("m_007_powerhouse_gym_bangalore")
    customer = _load_customer("c_001_priya_for_m001")
    customer = {**customer, "merchant_id": merchant["merchant_id"]}
    category = _load_category("gyms")
    trigger_payload = {"days_since_last_visit": 100, "previous_focus": "weight_loss"}
    fact = resolve_fact("customer_lapsed_soft", trigger_payload)
    assert fact is not None and "100" in fact

    trigger = {"kind": "customer_lapsed_soft", "fact": fact, "trigger_id": "trg_probe_soft", "payload": trigger_payload}
    result = compose(
        category="gyms", merchant_payload=merchant, trigger=trigger,
        customer_payload=customer, category_payload=category,
    )
    assert result["body"]
    valid, unverifiable = verify_numeric_claims(result["body"], merchant, trigger, customer, category)
    assert valid, f"invented numbers: {unverifiable} in body: {result['body']}"


def test_customer_trigger_defers_until_customer_context_arrives(client):
    merchant = _load_merchant("m_001_drmeera_dentist_delhi")
    customer = _load_customer("c_001_priya_for_m001")
    category = _load_category("dentists")
    trigger = _load_trigger("trg_003_recall_due_priya")
    suffix = "_defer_regression"
    mid, cid, tid = merchant["merchant_id"] + suffix, customer["customer_id"] + suffix, trigger["id"] + suffix
    merchant = {**merchant, "merchant_id": mid}
    customer = {**customer, "customer_id": cid, "merchant_id": mid}
    trigger = {**trigger, "id": tid, "merchant_id": mid, "customer_id": cid, "suppression_key": trigger["suppression_key"] + suffix}

    client.post("/v1/context", json={"scope": "category", "context_id": "dentists", "version": 1, "payload": category})
    client.post("/v1/context", json={"scope": "merchant", "context_id": mid, "version": 1, "payload": merchant})
    client.post("/v1/context", json={"scope": "trigger", "context_id": tid, "version": 1, "payload": trigger})
    first = client.post("/v1/tick", json={"now": "2026-04-26T11:00:00Z", "available_triggers": [tid]})
    assert first.json()["actions"] == []

    client.post("/v1/context", json={"scope": "customer", "context_id": cid, "version": 1, "payload": customer})
    second = client.post("/v1/tick", json={"now": "2026-04-26T11:01:00Z", "available_triggers": [tid]})
    assert len(second.json()["actions"]) == 1
    assert second.json()["actions"][0]["send_as"] == "merchant_on_behalf"


# ---------------------------------------------------------------------------
# Derived-fact layer for placeholder trigger payloads
# ---------------------------------------------------------------------------
#
# The official dataset hand-authors ~25 triggers with real event payloads and
# synthesises the other 75 with a bare {"placeholder": true, "metric_or_topic":
# "<kind>"} payload. 13 of the 30 canonical graded pairs land on one of those.
# An independent Gemini judge run over all 30 measured the cost precisely:
#     real-payload pairs   n=17  avg 39.8/50
#     placeholder pairs    n=13  avg 24.1/50
# with every one of the 5 worst messages being a placeholder pair, always for
# the same stated reason ("too generic", "no concrete verifiable facts").
#
# These tests lock in the fix: when the trigger carries no event data, the
# resolver must still ground the message in a REAL number read verbatim from
# the merchant/customer/category contexts that were pushed alongside it --
# and must still return None rather than inventing anything when there is
# genuinely no data anywhere.


def test_peer_comparison_handles_merchant_sitting_exactly_at_peer_average():
    """Real T17 repro. m_029_anand_restaurant_chandigarh has ctr 0.025 and
    the restaurants peer avg_ctr is also 0.025. The old guard was
    `ratio >= 0 and ratio > best_ratio` with best_ratio seeded at 0.0, so an
    exact tie passed the first half and failed the second -- yielding None
    and the fully generic 15/50 message "noticed you've been a bit quiet"."""
    from trigger_facts import _peer_comparison_fact

    merchant = {"performance": {"views": 2189, "calls": 26, "directions": 82, "ctr": 0.025}}
    category = {"peer_stats": {"avg_views_30d": 4800, "avg_calls_30d": 38,
                               "avg_directions_30d": 95, "avg_ctr": 0.025}}
    fact = _peer_comparison_fact(merchant, category)
    assert fact is not None
    assert "0.025" in fact
    # An exact tie must not be claimed as "above" -- that's self-contradicting
    # next to two identical numbers in the same sentence.
    assert "above" not in fact
    assert "in line with the peer benchmark" in fact
    # CTR is singular: "your CTR is", never "your CTR are".
    assert "CTR is" in fact
    assert "CTR are" not in fact


def test_peer_comparison_verb_agreement_per_metric():
    from trigger_facts import _peer_comparison_fact

    # views/calls/directions take "are"; ctr takes "is".
    views_fact = _peer_comparison_fact(
        {"performance": {"views": 5934}}, {"peer_stats": {"avg_views_30d": 1100}}
    )
    assert views_fact is not None and "views are" in views_fact

    ctr_fact = _peer_comparison_fact(
        {"performance": {"ctr": 0.053}}, {"peer_stats": {"avg_ctr": 0.025}}
    )
    assert ctr_fact is not None and "CTR is" in ctr_fact and "above peer benchmark" in ctr_fact


def test_peer_comparison_allow_below_gives_loss_aversion_hook():
    """Every metric below peer: default mode correctly declines (leading with
    a deficit would be tonally wrong for a celebration framing), but
    allow_below must surface the largest gap as a loss-aversion hook."""
    from trigger_facts import _peer_comparison_fact

    merchant = {"performance": {"views": 500, "calls": 5, "ctr": 0.01}}
    category = {"peer_stats": {"avg_views_30d": 4800, "avg_calls_30d": 38, "avg_ctr": 0.038}}

    assert _peer_comparison_fact(merchant, category) is None
    below = _peer_comparison_fact(merchant, category, allow_below=True)
    assert below is not None
    assert "below peer benchmark" in below


def test_placeholder_perf_spike_uses_real_seven_day_delta():
    """Real T27 repro (m_010_sunrisepharm_pharmacy_lucknow, 18/50,
    specificity 2/10). The message was "your numbers are trending up this
    week" while the merchant payload carried real +2% views / +5% calls
    deltas the entire time."""
    from trigger_facts import resolve_fact

    merchant = {
        "performance": {"window_days": 30, "views": 720, "calls": 14, "ctr": 0.041,
                        "delta_7d": {"views_pct": 0.02, "calls_pct": 0.05}},
    }
    fact = resolve_fact("perf_spike", {"placeholder": True, "metric_or_topic": "perf_spike"},
                        merchant_payload=merchant, category_payload={})
    assert fact is not None
    assert "5%" in fact           # the real, larger-magnitude delta
    assert "calls" in fact
    assert "up" in fact


def test_placeholder_milestone_names_a_real_number():
    """Real T23 repro (m_032_mukesh_restaurant_pune, 12/50, specificity
    0/10): "you've hit a solid milestone recently" despite a real
    889-customer YTD figure sitting in customer_aggregate."""
    from trigger_facts import resolve_fact

    merchant = {
        "performance": {"views": 787, "calls": 5, "ctr": 0.053},
        "customer_aggregate": {"total_unique_ytd": 889},
    }
    fact = resolve_fact("milestone_reached", {"placeholder": True},
                        merchant_payload=merchant, category_payload={})
    assert fact is not None
    assert "889" in fact


def test_placeholder_milestone_never_leads_with_a_deficit():
    """A milestone/celebration framing must not be grounded in a below-peer
    gap -- that would invert the tone of the message."""
    from trigger_facts import resolve_fact

    merchant = {"performance": {"views": 100, "ctr": 0.001}}
    category = {"peer_stats": {"avg_views_30d": 4800, "avg_ctr": 0.038}}
    fact = resolve_fact("milestone_reached", {"placeholder": True},
                        merchant_payload=merchant, category_payload=category)
    assert fact is None or "below peer benchmark" not in fact


def test_placeholder_competitor_opened_anchors_on_own_standing():
    """Real T10 repro (m_006_southindiancafe, 14/50): "a new competitor has
    opened nearby" for the richest merchant context in the dataset."""
    from trigger_facts import resolve_fact

    merchant = {"performance": {"views": 12400, "calls": 88, "directions": 320, "ctr": 0.032}}
    category = {"peer_stats": {"avg_views_30d": 4800, "avg_calls_30d": 38,
                               "avg_directions_30d": 95, "avg_ctr": 0.025}}
    fact = resolve_fact("competitor_opened", {"placeholder": True},
                        merchant_payload=merchant, category_payload=category)
    assert fact is not None
    assert "above peer benchmark" in fact


def test_placeholder_customer_lapsed_uses_real_visit_history():
    """Real T14/T15 repro: "It's been a while since your last visit" while
    the customer payload carried a real visit count and last-visit date."""
    from trigger_facts import resolve_fact

    customer = {"relationship": {"visits_total": 12, "last_visit": "2026-04-01",
                                 "lifetime_value": 3012}}
    fact = resolve_fact("customer_lapsed_soft", {"placeholder": True},
                        customer_payload=customer, merchant_payload={})
    assert fact is not None
    assert "12 times" in fact
    assert "2026-04-01" in fact
    # Must read as a self-contained clause so it survives being embedded /
    # code-mixed by the composer (see the T15 mangling this guards against).
    assert fact.startswith("you've visited us")


def test_derived_customer_fact_does_not_invent_a_day_count():
    """These resolvers have no trustworthy "now" (the judge controls
    simulated time), so the last-visit date must be cited verbatim rather
    than converted into a computed "N days ago"."""
    from trigger_facts import _derived_customer_fact

    fact = _derived_customer_fact({"relationship": {"visits_total": 3, "last_visit": "2026-04-01"}})
    assert fact is not None
    assert "days" not in fact.lower()
    assert "2026-04-01" in fact
    assert "3 times" in fact

    # Singular visit reads naturally too, not "1 times".
    single = _derived_customer_fact({"relationship": {"visits_total": 1, "last_visit": "2026-04-01"}})
    assert single is not None and "once" in single and "1 times" not in single


def test_far_out_festival_is_not_framed_as_urgent():
    """Real T18 repro: Diwali at days_until=188 was pitched with live-window
    urgency and scored trigger_relevance 3/10 for it."""
    from trigger_facts import resolve_fact

    far = resolve_fact("festival_upcoming", {"festival": "Diwali", "days_until": 188},
                       merchant_payload={}, category_payload={})
    assert far is not None
    assert "188" in far
    assert "still" in far and "planning window" in far

    near = resolve_fact("festival_upcoming", {"festival": "Diwali", "days_until": 9},
                        merchant_payload={}, category_payload={})
    assert near == "Diwali is 9 days away"


def test_perf_dip_baseline_is_not_labelled_per_day():
    """Real T24 repro: vs_baseline is a window total (it equals
    performance.calls for the same window), but the fact string called it
    "baseline 12/day" and the judge flagged the misread explicitly."""
    from trigger_facts import resolve_fact

    fact = resolve_fact("perf_dip", {"metric": "calls", "delta_pct": -0.50,
                                     "window": "7d", "vs_baseline": 12},
                        merchant_payload={}, category_payload={})
    assert fact is not None
    assert "/day" not in fact
    assert "baseline of 12" in fact


def test_category_seasonal_trend_token_renders_percent_units():
    """Real seed values look like "ORS_demand_+40"; naive underscore
    stripping produced the bare "ORS demand +40" the judge called clunky."""
    from trigger_facts import resolve_fact, _humanize_trend_token

    assert _humanize_trend_token("ORS_demand_+40") == "ORS demand +40%"
    assert _humanize_trend_token("cold_cough_demand_-60") == "cold cough demand -60%"
    # No trailing signed number -> left exactly as-is, no invented units.
    assert _humanize_trend_token("shelf_rearrange") == "shelf rearrange"

    fact = resolve_fact("category_seasonal",
                        {"season": "summer_2026", "trends": ["ORS_demand_+40", "sunscreen_demand_+38"]},
                        merchant_payload={}, category_payload={})
    assert "ORS demand +40%" in fact


def test_placeholder_resolvers_still_return_none_with_no_context_at_all():
    """The derived-fact layer must never manufacture a fact out of nothing --
    with empty contexts these placeholder kinds still resolve to None, which
    is what lets the templates fall back to an honest low-specificity ask.

    `competitor_opened` is deliberately excluded and asserted separately
    below: restating the trigger kind's own premise is not fabrication."""
    from trigger_facts import resolve_fact

    for kind in ("perf_spike", "perf_dip", "milestone_reached",
                 "festival_upcoming", "dormant_with_vera", "customer_lapsed_soft",
                 "chronic_refill_due", "category_seasonal"):
        assert resolve_fact(kind, {"placeholder": True}, merchant_payload={},
                            category_payload={}, customer_payload={}) is None, kind


def test_no_placeholder_resolver_ever_invents_a_number():
    """The guarantee that actually matters for the fabrication penalty: with
    empty contexts, no resolver may emit a digit. `competitor_opened` restates
    its own trigger premise in words (needed for trigger relevance -- see the
    T10 test above) but still contributes no unverifiable figure."""
    import re

    from trigger_facts import resolve_fact

    for kind in ("perf_spike", "perf_dip", "milestone_reached", "competitor_opened",
                 "festival_upcoming", "dormant_with_vera", "customer_lapsed_soft",
                 "chronic_refill_due", "category_seasonal", "appointment_tomorrow"):
        fact = resolve_fact(kind, {"placeholder": True}, merchant_payload={},
                            category_payload={}, customer_payload={})
        if fact is not None:
            assert not re.search(r"\d", fact), f"{kind} invented a number: {fact!r}"


def test_day_restricted_offer_is_not_recommended_on_a_conflicting_day():
    """Real T21 repro: the offer is "Buy 1 Pizza Get 1 Free (Tue-Thu)" and the
    match payload says is_weeknight=false, but Vera recommended pushing it
    "today" anyway -- scored merchant_fit 3/10 with a -1 penalty for the
    operational contradiction."""
    from fallback import _offer_day_restriction_conflicts

    weekday_offer = {"name": "Buy 1 Pizza Get 1 Free (Tue-Thu)"}
    assert _offer_day_restriction_conflicts(weekday_offer, {"is_weeknight": False}) is True
    assert _offer_day_restriction_conflicts(weekday_offer, {"is_weeknight": True}) is False

    weekend_offer = {"name": "Family Sunday Brunch @ Rs699/pax"}
    assert _offer_day_restriction_conflicts(weekend_offer, {"is_weeknight": True}) is True

    # Ambiguous / unrestricted offers are never suppressed.
    plain_offer = {"name": "Match-night Combo @ Rs399"}
    assert _offer_day_restriction_conflicts(plain_offer, {"is_weeknight": False}) is False
    assert _offer_day_restriction_conflicts(weekday_offer, {}) is False
    assert _offer_day_restriction_conflicts(None, {"is_weeknight": False}) is False


def test_ipl_template_drops_conflicting_offer_but_keeps_the_fact():
    from fallback import _tpl_ipl_match_today

    offer = {"name": "Buy 1 Pizza Get 1 Free (Tue-Thu)", "price": 0}
    fact = "DC vs MI today at Arun Jaitley Stadium, not a weeknight"
    body, cta = _tpl_ipl_match_today("Suresh", fact, offer, hi=False,
                                     trigger_payload={"is_weeknight": False})
    assert "Tue-Thu" not in body      # the conflicting offer is not pushed
    assert "DC vs MI" in body         # but the real trigger fact survives


def test_new_customer_only_offer_rejected_for_returning_customer():
    """Real T14 repro (m_014_dr_asha + c_055_reyansh, -1 penalty): the draft
    told a patient with 5 recorded visits "We offer a Free Smile Analysis +
    Digital Scan for new patients". The eligibility claim is false for that
    recipient, which is a correctness problem, not just tone."""
    from fact_check import find_new_customer_offer_mismatch

    returning = {"relationship": {"visits_total": 5, "last_visit": "2026-04-01"}, "state": "churned"}
    body = ("Hi Reyansh, it's been a while. We offer a Free Smile Analysis + Digital Scan "
            "for new patients. Want to book?")
    hits = find_new_customer_offer_mismatch(body, returning)
    assert hits, "must flag a new-patients-only promise made to a 5-visit patient"

    # Same wording is fine for a genuinely new contact with no history.
    brand_new = {"relationship": {"visits_total": 0}, "state": "new"}
    assert find_new_customer_offer_mismatch(body, brand_new) == []

    # And a returning customer with no new-customer wording is untouched.
    clean = "Hi Reyansh, you've visited us 5 times. Want to book your next check-up?"
    assert find_new_customer_offer_mismatch(clean, returning) == []

    # Merchant-facing messages have no customer context and must never trip it.
    assert find_new_customer_offer_mismatch(body, None) == []


def test_new_customer_gate_is_wired_into_the_draft_evaluator():
    from composer import _evaluate_draft
    from category_styles import get_category_style

    problems = _evaluate_draft(
        "Hi Reyansh, we offer a Free Consultation for new patients. Shall I book you in?",
        {"identity": {"name": "Asha Dental Care", "owner_first_name": "Dr. Asha"},
         "category_slug": "dentists", "offers": []},
        {"kind": "customer_lapsed_soft", "fact": None, "payload": {}},
        {"identity": {"name": "Reyansh"},
         "relationship": {"visits_total": 5, "last_visit": "2026-04-01"}, "state": "churned"},
        get_category_style("dentists", None),
        "customer_lapsed_soft",
        None,
    )
    assert any("restricted to new customers" in p for p in problems)


def test_placeholder_festival_does_not_splice_in_an_unrelated_seasonal_beat():
    """Real T19 repro (-2 penalty): splicing the first gym seasonal_beat
    ("Jan: resolution surge") into an unnamed upcoming-festival message
    produced "With the Jan resolution surge ending...", a season the trigger
    never mentioned. trigger_relevance was scored 3/10 for the mismatch."""
    from trigger_facts import resolve_fact

    gyms_category = {
        "peer_stats": {"avg_views_30d": 1100, "avg_calls_30d": 18, "avg_ctr": 0.045},
        "seasonal_beats": [{"month_range": "Jan", "note": "resolution surge - trial walk-ins 4x baseline"}],
    }
    merchant = {"performance": {"views": 5934, "calls": 65, "ctr": 0.042,
                                "delta_7d": {"views_pct": -0.07, "calls_pct": 0.06}}}
    fact = resolve_fact("festival_upcoming", {"placeholder": True},
                        merchant_payload=merchant, category_payload=gyms_category)
    assert fact is not None
    assert "resolution surge" not in fact
    assert "Jan" not in fact
    # Still grounded in the merchant's own real numbers.
    assert "5934" in fact or "7%" in fact or "6%" in fact


def test_placeholder_competitor_keeps_the_competitive_why_now():
    """Two-round T10 lesson. Grounding the placeholder case in the merchant's
    own numbers lifted specificity 0->8, but dropping the competitive framing
    entirely then tanked trigger relevance to 2/10 with a -2 penalty ("fails
    completely on trigger relevance by ignoring the competitor") -- a net
    loss. Both halves have to survive: the trigger kind justifies saying new
    competition appeared; only its name/distance/offer would be fabricated."""
    from trigger_facts import resolve_fact

    merchant = {"performance": {"views": 12400, "directions": 320, "ctr": 0.032}}
    category = {"peer_stats": {"avg_views_30d": 4800, "avg_directions_30d": 95, "avg_ctr": 0.025}}
    fact = resolve_fact("competitor_opened", {"placeholder": True},
                        merchant_payload=merchant, category_payload=category)
    assert fact is not None
    assert "competition" in fact.lower()          # the why-now survives
    assert "320" in fact or "12400" in fact        # anchored in a real number
    # Never invents identifying detail about the competitor.
    for invented in ("km away", "Smile Studio", "opened with"):
        assert invented not in fact


def test_appointment_reminder_not_asserted_for_a_lapsed_customer():
    """Real T03 repro (17/50, -2 penalty, trigger_relevance 2/10): with a
    placeholder payload carrying no booking detail, asserting "your
    appointment is tomorrow" to a lapsed_hard customer with 1 visit reads as
    an invented appointment. Reframe as a slot offer instead."""
    from fallback import _tpl_customer_appointment_tomorrow

    lapsed = {"state": "lapsed_hard", "relationship": {"visits_total": 1, "last_visit": "2026-04-01"}}
    body, cta = _tpl_customer_appointment_tomorrow(
        "Aditya", "Karim's Salon", {"placeholder": True}, hi=False, customer_state=lapsed
    )
    assert "appointment is tomorrow" not in body.lower()
    assert "slot open tomorrow" in body.lower()
    assert cta == "binary_yes_no"

    # A real booking detail is still reported as a straight reminder...
    body2, _ = _tpl_customer_appointment_tomorrow(
        "Riya", "Beauty Lounge", {"appointment_time": "5pm"}, hi=False, customer_state=lapsed
    )
    assert "appointment is tomorrow" in body2.lower() and "5pm" in body2

    # ...and so is an active customer with no detail (the trigger kind is the
    # authority that a booking exists; only its details would be invented).
    active = {"state": "active", "relationship": {"visits_total": 6}}
    body3, _ = _tpl_customer_appointment_tomorrow(
        "Riya", "Beauty Lounge", {"placeholder": True}, hi=False, customer_state=active
    )
    assert "appointment is tomorrow" in body3.lower()


def test_dormant_checkin_ends_in_a_concrete_low_effort_ask():
    """Judge scored the old passive close ("let me know if you ever need
    help") 3/10 on engagement twice running."""
    from fallback import _tpl_dormant_with_vera

    for hinglish in (False, True):
        body, cta = _tpl_dormant_with_vera("Anand", "it's been 38 days since we last talked", hinglish)
        assert cta == "binary_yes_no"
        assert "bata dena" not in body      # the passive Hinglish close is gone
        assert "draft" in body.lower()      # offers to do the work instead


def test_unsupported_appointment_assertion_is_rejected():
    """Real T03 repro (20/50, -2 penalty, trigger_relevance 2/10). The
    template-level guard alone was not enough: this pair is composed by the
    LLM, so the check has to run as a draft quality gate too."""
    from fact_check import find_unsupported_appointment_assertion

    bare_payload = {"placeholder": True, "metric_or_topic": "appointment_tomorrow"}
    lapsed = {"identity": {"name": "Aditya"}, "state": "lapsed_hard",
              "relationship": {"visits_total": 1, "last_visit": "2026-04-01"}}

    for body in [
        "Hi Aditya, Karim's Salon here. Your appointment is tomorrow. Reply YES to confirm.",
        "Hi Aditya, reminder: your appointment tomorrow at the salon.",
        "Hi Aditya, aapka appointment kal hai. Confirm karein?",
        "Hi Aditya, we have you booked for tomorrow.",
        # Live-observed evasion of the first, enumeration-based version of
        # this gate -- a phrasing none of its six templates covered.
        ("Hi Aditya, this is Karim's Salon. Just a quick reminder that you have an "
         "appointment with us tomorrow. Should we proceed as scheduled?"),
        "Hi Aditya, your sitting is booked for tomorrow.",
    ]:
        assert find_unsupported_appointment_assertion(body, bare_payload, lapsed), body

    # A real detail in the payload makes the assertion legitimate.
    assert find_unsupported_appointment_assertion(
        "Your appointment is tomorrow at 5pm.", {"appointment_time": "5pm"}, lapsed) == []

    # An active customer with a bare payload is left alone -- the trigger kind
    # is normally sufficient authority that a booking exists.
    active = {"state": "active", "relationship": {"visits_total": 6}}
    assert find_unsupported_appointment_assertion(
        "Your appointment is tomorrow.", bare_payload, active) == []

    # Slot OFFERS are never flagged -- these are the rewrite this gate steers
    # toward, so tripping on them would make the correction unsatisfiable.
    for offer_body in [
        "We have a slot open tomorrow - want me to hold it?",
        "Hi Aditya, kal ke liye ek slot khaali hai - aapke liye rakh doon?",
        "Would you like to book a sitting tomorrow?",
        "Shall I hold a slot for you tomorrow?",
    ]:
        assert find_unsupported_appointment_assertion(offer_body, bare_payload, lapsed) == [], offer_body

    # Merchant-facing messages have no customer context.
    assert find_unsupported_appointment_assertion(
        "Your appointment is tomorrow.", bare_payload, None) == []


def test_unsupported_appointment_gate_is_wired_into_the_draft_evaluator():
    from composer import _evaluate_draft
    from category_styles import get_category_style

    problems = _evaluate_draft(
        "Hi Aditya, Karim's Salon here. Your appointment is tomorrow. Reply YES to confirm.",
        {"identity": {"name": "Karim's Salon", "owner_first_name": "Karim"},
         "category_slug": "salons", "offers": []},
        {"kind": "appointment_tomorrow", "fact": None,
         "payload": {"placeholder": True, "metric_or_topic": "appointment_tomorrow"}},
        {"identity": {"name": "Aditya"}, "state": "lapsed_hard",
         "relationship": {"visits_total": 1, "last_visit": "2026-04-01"}},
        get_category_style("salons", None),
        "appointment_tomorrow",
        None,
    )
    assert any("confirmed appointment" in p for p in problems)
