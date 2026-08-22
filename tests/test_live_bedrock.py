"""Live Bedrock integration check -- only meaningful when AWS_BEARER_TOKEN_BEDROCK
(or another valid credential) is present in the environment. Skips cleanly
otherwise so the rest of the suite doesn't depend on network/AWS access.
"""
import os

import pytest
from dotenv import load_dotenv

load_dotenv()

from composer import compose
from fact_check import count_ctas, verify_numeric_claims

requires_bedrock = pytest.mark.skipif(
    "AWS_BEARER_TOKEN_BEDROCK" not in os.environ,
    reason="no Bedrock credentials in environment",
)


@requires_bedrock
def test_live_bedrock_research_digest_matches_worked_example():
    merchant_payload = {
        "identity": {"name": "Dr. Meera", "category": "dentists", "languages": ["en"]},
        "performance": {"ctr": 0.021, "peer_ctr_median": 0.030},
        "offers": [{"name": "Dental Check Up", "price": 299}],
    }
    trigger = {
        "kind": "research_digest",
        "fact": "190 people in your area searched for 'Dental Check Up' this week",
        "trigger_id": "trg_research_digest_dentists",
    }
    result = compose(category="dentists", merchant_payload=merchant_payload, trigger=trigger)

    assert "190" in result["body"]
    assert count_ctas(result["body"]) <= 1
    valid, unverifiable = verify_numeric_claims(result["body"], merchant_payload, trigger)
    assert valid, f"live Nova output invented facts: {unverifiable} in body: {result['body']}"


@requires_bedrock
def test_live_bedrock_no_offer_does_not_invent_price():
    merchant_payload = {
        "identity": {"name": "Dr. Rao", "category": "dentists", "languages": ["en"]},
        "offers": [],
    }
    trigger = {
        "kind": "research_digest",
        "fact": "190 people in your area searched for 'Dental Check Up' this week",
        "trigger_id": "trg_research_digest_dentists",
    }
    result = compose(category="dentists", merchant_payload=merchant_payload, trigger=trigger)
    assert "\u20b9" not in result["body"], f"live Nova invented a price with no offer present: {result['body']}"


@requires_bedrock
def test_live_bedrock_hinglish_merchant():
    merchant_payload = {
        "identity": {"name": "Sharma Ji Gym", "category": "gyms", "languages": ["hi", "en"]},
        "offers": [{"name": "Monthly Pass", "price": 999}],
    }
    trigger = {"kind": "perf_dip", "fact": "footfall dropped this week"}
    result = compose(category="gyms", merchant_payload=merchant_payload, trigger=trigger)
    valid, unverifiable = verify_numeric_claims(result["body"], merchant_payload, trigger)
    assert valid, f"live Nova invented facts: {unverifiable}"
    assert count_ctas(result["body"]) <= 1
