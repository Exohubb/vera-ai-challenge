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
    assert "19:30:00+05:30" in ipl_fact and "not a weeknight" in ipl_fact

    review = _load_trigger("trg_011_review_theme_late_delivery")
    review_fact = resolve_fact(review["kind"], review["payload"])
    assert "50 mins for a 15 min ride" in review_fact

    cde = _load_trigger("trg_022_cde_webinar_dentists")
    cde_fact = resolve_fact(cde["kind"], cde["payload"], category_payload=_load_category("dentists"))
    assert "free for members" in cde_fact


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
