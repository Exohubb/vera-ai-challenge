"""Unit tests for the composer: specificity, no-hallucination, single CTA,
and the concrete worked examples A and B from section 8.2."""
from composer import compose
from fact_check import count_ctas, verify_numeric_claims


def test_example_a_research_digest_with_offer():
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
    assert "299" in result["body"]
    assert count_ctas(result["body"]) == 1
    assert result["cta"] == "binary_yes_no"

    valid, unverifiable = verify_numeric_claims(result["body"], merchant_payload, trigger)
    assert valid, f"unverifiable numbers found: {unverifiable}"


def test_example_b_missing_offer_no_invented_price():
    merchant_payload = {
        "identity": {"name": "Dr. Rao", "category": "dentists", "languages": ["en"]},
        "performance": {"ctr": 0.02, "peer_ctr_median": 0.03},
        "offers": [],
    }
    trigger = {
        "kind": "research_digest",
        "fact": "190 people in your area searched for 'Dental Check Up' this week",
        "trigger_id": "trg_research_digest_dentists",
    }
    result = compose(category="dentists", merchant_payload=merchant_payload, trigger=trigger)

    assert "\u20b9" not in result["body"], "must not invent a currency amount with no matching offer"
    assert "no matching offer" in result["rationale"].lower() or "offer" in result["rationale"].lower()


def test_composer_never_invents_numbers_across_random_cases():
    cases = [
        ({"identity": {"name": "Glow Salon", "category": "salons"}, "offers": [{"name": "Bridal Package", "price": 4999}]},
         {"kind": "milestone_reached", "fact": "500th booking milestone hit this month"}),
        ({"identity": {"name": "FitZone Gym", "category": "gyms"}, "performance": {"ctr": 0.01, "peer_ctr_median": 0.05}, "offers": []},
         {"kind": "perf_dip", "fact": None}),
        ({"identity": {"name": "City Pharmacy", "category": "pharmacies"}, "offers": [{"name": "BP Monitor", "price": 1200}]},
         {"kind": "recall_due", "fact": "refill due for chronic medication"}),
    ]
    for merchant_payload, trigger in cases:
        result = compose(category=merchant_payload["identity"]["category"], merchant_payload=merchant_payload, trigger=trigger)
        valid, unverifiable = verify_numeric_claims(result["body"], merchant_payload, trigger)
        assert valid, f"invented numbers {unverifiable} in body: {result['body']}"
        assert count_ctas(result["body"]) <= 1


def test_all_real_dataset_trigger_kinds_have_strategy_and_fallback():
    """Live-repro'd gap: 14 of the 24 real trigger kinds in
    dataset/triggers_seed.json (including ipl_match_today = case study 5,
    supply_alert = case study 9, active_planning_intent = case studies
    3/6) had no strategy mapping and silently fell to the weakest
    default lever. Every kind that appears in the real dataset must have
    both a strategy AND a dedicated fallback template."""
    import json
    from pathlib import Path

    from fallback import _TRIGGER_TEMPLATE_DISPATCH
    from trigger_router import DEFAULT_STRATEGY, TRIGGER_STRATEGY_TABLE, get_strategy

    dataset_dir = Path(__file__).resolve().parents[2] / "magicpin-ai-challenge" / "dataset"
    triggers = json.load(open(dataset_dir / "triggers_seed.json"))["triggers"]
    kinds = {t["kind"] for t in triggers}

    for kind in kinds:
        assert kind in TRIGGER_STRATEGY_TABLE, f"{kind} has no dedicated strategy, falls to DEFAULT_STRATEGY"
        assert get_strategy(kind) is not DEFAULT_STRATEGY
        assert kind in _TRIGGER_TEMPLATE_DISPATCH, f"{kind} has no dedicated fallback template"


def test_unknown_trigger_kind_gets_safe_default():
    merchant_payload = {"identity": {"name": "Test Biz", "category": "restaurants"}, "offers": []}
    trigger = {"kind": "unknown_future_trigger", "fact": None}
    result = compose(category="restaurants", merchant_payload=merchant_payload, trigger=trigger)
    assert result["body"]
    assert result["cta"] in ("binary_yes_no", "open_ended", "multi_choice")


def test_never_fabricates_a_url():
    """api-call-examples.md Example F.4: any URL not present verbatim in
    context is a hard fail. cde_opportunity triggers are especially prone
    to this since the model may 'helpfully' invent an event registration
    link that was never in the digest item."""
    from fact_check import find_urls

    merchant_payload = {
        "identity": {"name": "Dr. Meera", "category": "dentists", "languages": ["en"]},
        "offers": [],
    }
    trigger = {
        "kind": "cde_opportunity",
        "fact": "IDA Delhi: Digital impressions webinar (IDA Delhi chapter calendar) (2 CDE credits)",
    }
    result = compose(category="dentists", merchant_payload=merchant_payload, trigger=trigger)
    assert not find_urls(result["body"]), f"invented URL in body: {result['body']}"


def test_transient_bedrock_fallback_does_not_poison_cache(monkeypatch):
    import composer

    merchant = {"identity": {"name": "Transient Test", "category": "dentists"}, "offers": []}
    trigger = {"kind": "regulation_change", "fact": "a real rule changed", "trigger_id": "transient_cache_unique"}
    calls = {"count": 0}

    def fake_invoke(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return composer._BedrockOutcome(None, "transient")
        return composer._BedrockOutcome({
            "body": "Transient Test, a real rule changed. Want the checklist?",
            "cta": "binary_yes_no",
            "rationale": "grounded recovery",
        })

    monkeypatch.setattr(composer, "_invoke_bedrock", fake_invoke)
    first = composer.compose("dentists", merchant, trigger)
    second = composer.compose("dentists", merchant, trigger)

    assert calls["count"] == 2
    assert first["body"] != second["body"]
    assert second["rationale"].startswith("grounded recovery")
