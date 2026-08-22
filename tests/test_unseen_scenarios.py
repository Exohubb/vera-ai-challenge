"""Unseen-scenario test (section 8.1 item 9 -- 'the single best predictor
of your real score'). These context/trigger combinations are hand-written
and do not exist in any sample/example data. They test that the bot
generalizes rather than pattern-matching memorized cases.
"""
from composer import compose
from fact_check import count_ctas, verify_numeric_claims


def test_unseen_salon_competitor_opened():
    merchant_payload = {
        "identity": {"name": "Style Studio", "category": "salons", "languages": ["en", "hi"]},
        "performance": {"footfall_last_week": 42, "footfall_peer_median": 60},
        "offers": [{"name": "Keratin Spa", "price": 1499}],
    }
    trigger = {"kind": "competitor_opened", "fact": "A new salon opened 400m away last week"}
    result = compose(category="salons", merchant_payload=merchant_payload, trigger=trigger)
    valid, unverifiable = verify_numeric_claims(result["body"], merchant_payload, trigger)
    assert valid, f"invented facts: {unverifiable}"
    assert count_ctas(result["body"]) <= 1
    assert result["body"]


def test_unseen_restaurant_festival_new_offer_price():
    merchant_payload = {
        "identity": {"name": "Spice Route", "category": "restaurants", "languages": ["en"]},
        "offers": [{"name": "Diwali Thali", "price": 899}],
    }
    trigger = {"kind": "festival_upcoming", "fact": "Diwali is in 5 days"}
    result = compose(category="restaurants", merchant_payload=merchant_payload, trigger=trigger)
    valid, unverifiable = verify_numeric_claims(result["body"], merchant_payload, trigger)
    assert valid, f"invented facts: {unverifiable}"
    assert count_ctas(result["body"]) <= 1


def test_unseen_pharmacy_regulation_change_no_category_pairing_before():
    """regulation_change paired with pharmacies -- a combination not shown
    anywhere in the build-prompt examples."""
    merchant_payload = {
        "identity": {"name": "Wellness Pharmacy", "category": "pharmacies", "languages": ["en", "hi"]},
        "offers": [],
    }
    trigger = {"kind": "regulation_change", "fact": "new prescription labeling rule takes effect next month"}
    result = compose(category="pharmacies", merchant_payload=merchant_payload, trigger=trigger)
    assert result["cta"] == "binary_yes_no"
    valid, unverifiable = verify_numeric_claims(result["body"], merchant_payload, trigger)
    assert valid, f"invented facts: {unverifiable}"


def test_unseen_gym_milestone_with_invented_metric_not_present():
    """Merchant has a metric field the composer must NOT reference since
    it's not part of the trigger or offer -- guards against dumping every
    available fact (decision quality: pick ONE driving signal)."""
    merchant_payload = {
        "identity": {"name": "PowerHouse Gym", "category": "gyms"},
        "performance": {"members_active": 340, "churn_rate": 0.08},
        "offers": [{"name": "Annual Membership", "price": 12000}],
    }
    trigger = {"kind": "milestone_reached", "fact": "crossed 1000 total sign-ups this year"}
    result = compose(category="gyms", merchant_payload=merchant_payload, trigger=trigger)
    valid, unverifiable = verify_numeric_claims(result["body"], merchant_payload, trigger)
    assert valid, f"invented facts: {unverifiable}"
    assert count_ctas(result["body"]) <= 1


def test_unseen_dormant_merchant_low_pressure():
    merchant_payload = {"identity": {"name": "Quiet Dental", "category": "dentists"}, "offers": []}
    trigger = {"kind": "dormant_with_vera", "fact": "no activity with Vera in 45 days"}
    result = compose(category="dentists", merchant_payload=merchant_payload, trigger=trigger)
    assert result["body"]
    assert count_ctas(result["body"]) <= 1


def test_unseen_new_high_price_offer_not_hallucinated_down():
    """A brand-new, unusually high offer price the model has never seen in
    any example -- ensure it's reproduced verbatim, not rounded/altered."""
    merchant_payload = {
        "identity": {"name": "Elite Dental Care", "category": "dentists"},
        "offers": [{"name": "Full Mouth Implant Consultation", "price": 24999}],
    }
    trigger = {"kind": "research_digest", "fact": "72 people searched for 'Dental Implants' this week"}
    result = compose(category="dentists", merchant_payload=merchant_payload, trigger=trigger)
    assert "24999" in result["body"] or "24,999" in result["body"]
    assert "72" in result["body"]
    valid, unverifiable = verify_numeric_claims(result["body"], merchant_payload, trigger)
    assert valid, f"invented facts: {unverifiable}"
