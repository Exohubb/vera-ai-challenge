"""Gates covering the two remaining one-sided grounding checks.

Both of these are failure modes where a message passed every existing gate
and was still materially worse than the context it was built from:

  1. Numbers that WERE resolved and then paraphrased away. Every gate asked
     "is this number real?"; none asked "did you use the real number?".
  2. An offer invented from nothing. find_category_offer_leakage catches a
     real offer attributed to the wrong owner; nothing caught a product that
     exists in no catalog at all.
"""
import json
import os
from pathlib import Path

import pytest

from fact_check import extract_hard_figures, find_dropped_grounded_figures
from offers import (
    find_category_offer_leakage,
    find_invented_offer_names,
    find_unsupported_discount_claims,
)

DATASET = Path(
    os.environ.get(
        "MAGICPIN_DATASET_DIR",
        Path(__file__).resolve().parents[2] / "magicpin-ai-challenge" / "dataset",
    )
)


# ---------------------------------------------------------------------------
# Hard-figure preservation
# ---------------------------------------------------------------------------

def test_hard_figures_are_percentages_and_large_counts_only():
    fact = (
        "3-month fluoride varnish recall outperforms 6-month for high-risk adult caries "
        "(JIDA Oct 2026, p.14) -- Multi-center Indian trial shows 38% lower caries recurrence "
        "with 3-month vs 6-month recall in adults with active decay history; 2100-patient trial"
    )
    figures = extract_hard_figures(fact)
    assert "38" in figures, "the headline percentage is the whole point"
    assert "2100" in figures, "the trial size is the credibility anchor"
    # Prose numbers and the citation's own year/page must NOT become
    # requirements -- forcing them in produces stilted copy for no gain.
    assert "3" not in figures and "6" not in figures
    assert "2026" not in figures
    assert "14" not in figures


def test_iso_dates_do_not_become_required_figures():
    """A customer-facing recall message should read "due 12th Nov", not "due
    2026". Requiring the year out of an ISO date would have degraded one of
    the highest-scoring messages in the graded set."""
    fact = "6 month cleaning recall is due (2026-11-12), last service 2026-05-12"
    assert extract_hard_figures(fact) == []


def test_paraphrasing_the_headline_number_away_is_caught():
    fact = (
        "3-month fluoride varnish recall outperforms 6-month (JIDA Oct 2026, p.14) -- "
        "Multi-center Indian trial shows 38% lower caries recurrence; 2100-patient trial"
    )
    vague = (
        "Dr. Meera, a recent JIDA study (Oct 2026, p.14) found 3-month fluoride recall "
        "outperforms 6-month for high-risk adults. Shall I send you the abstract?"
    )
    dropped = find_dropped_grounded_figures(vague, fact)
    assert sorted(dropped) == ["2100", "38"]

    # challenge-brief.md Appendix A's own gold-standard message keeps both.
    gold = (
        "Dr. Meera, JIDA's Oct issue landed. One item relevant to your high-risk adult "
        "patients -- 2,100-patient trial showed 3-month fluoride recall cuts caries "
        "recurrence 38% better than 6-month. Want me to pull it? -- JIDA Oct 2026 p.14"
    )
    assert find_dropped_grounded_figures(gold, fact) == []


def test_no_fact_or_no_figures_makes_the_gate_a_no_op():
    assert find_dropped_grounded_figures("anything at all", None) == []
    assert find_dropped_grounded_figures("anything at all", "") == []
    assert find_dropped_grounded_figures("", "no numbers in this fact") == []


@pytest.mark.skipif(not DATASET.exists(), reason="real dataset not available")
def test_gate_requirements_stay_narrow_across_the_real_dataset():
    """Guards against the gate becoming a blunt instrument: it must stay
    silent on the trigger kinds whose facts are date- or quote-shaped, and
    must never demand more than a handful of figures from any one fact."""
    from trigger_facts import resolve_fact

    categories = {}
    for path in (DATASET / "categories").glob("*.json"):
        payload = json.load(open(path))
        categories[payload.get("slug", path.stem)] = payload

    def _load(name, container, key):
        data = json.load(open(DATASET / name))
        return {i[key]: i for i in data.get(container, []) if key in i}

    merchants = _load("merchants_seed.json", "merchants", "merchant_id")
    customers = _load("customers_seed.json", "customers", "customer_id")
    triggers = _load("triggers_seed.json", "triggers", "id")

    for tid, envelope in triggers.items():
        merchant = merchants.get(envelope.get("merchant_id"))
        slug = (merchant or {}).get("category_slug") or (envelope.get("payload") or {}).get("category")
        fact = resolve_fact(
            envelope.get("kind", ""),
            envelope.get("payload") or {},
            category_payload=categories.get(slug),
            merchant_payload=merchant,
            customer_payload=customers.get(envelope.get("customer_id")) if envelope.get("customer_id") else None,
        )
        figures = extract_hard_figures(fact if isinstance(fact, str) else None)
        assert len(figures) <= 3, f"{tid} would demand too many figures: {figures}"

    # The two graded triggers whose facts are pure dates/quotes must stay
    # unconstrained, and the research digest must demand its two anchors.
    def _figures_for(tid):
        envelope = triggers[tid]
        merchant = merchants.get(envelope.get("merchant_id"))
        slug = (merchant or {}).get("category_slug") or (envelope.get("payload") or {}).get("category")
        fact = resolve_fact(
            envelope.get("kind", ""),
            envelope.get("payload") or {},
            category_payload=categories.get(slug),
            merchant_payload=merchant,
            customer_payload=customers.get(envelope.get("customer_id")) if envelope.get("customer_id") else None,
        )
        return extract_hard_figures(fact if isinstance(fact, str) else None)

    assert _figures_for("trg_002_compliance_dci_radiograph") == []
    assert _figures_for("trg_003_recall_due_priya") == []
    assert _figures_for("trg_011_review_theme_late_delivery") == []
    assert sorted(_figures_for("trg_001_research_digest_dentists")) == ["2100", "38"]


@pytest.mark.skipif(not DATASET.exists(), reason="real dataset not available")
def test_research_digest_fact_carries_the_quantified_effect():
    """The 38% lived in the digest item's `summary`, which the fact resolver
    discarded outright -- so the single most persuasive number in the brief's
    own gold-standard scenario could not reach the message even in principle."""
    from trigger_facts import resolve_fact

    dentists = json.load(open(DATASET / "categories" / "dentists.json"))
    fact = resolve_fact(
        "research_digest",
        {"category": "dentists", "top_item_id": "d_2026W17_jida_fluoride"},
        category_payload=dentists,
        merchant_payload=None,
        customer_payload=None,
    )
    assert "38%" in fact
    assert "2100" in fact.replace(",", "")
    assert "JIDA Oct 2026, p.14" in fact


# ---------------------------------------------------------------------------
# Offers invented from nothing
# ---------------------------------------------------------------------------

SALON = {
    "merchant_id": "m_003",
    "category_slug": "salons",
    "identity": {"name": "Studio11 Family Salon", "owner_first_name": "Lakshmi"},
    "offers": [
        {"id": "o1", "title": "Hair Spa @ \u20b9499", "status": "active"},
        {"id": "o2", "title": "Bridal Makeup @ \u20b95,999", "status": "active"},
    ],
}
SALON_CATEGORY = {
    "slug": "salons",
    "offer_catalog": [{"id": "c1", "title": "Haircut @ \u20b999", "value": "99"}],
}


def test_wholly_invented_package_with_terms_is_caught():
    """Observed in roughly 1 of 5 rolls: a package, a discount and a validity
    window, none of which exist anywhere in the pushed context. It passed
    numeric verification because the bare digits 20 and 30 turn up somewhere
    in almost any real payload."""
    body = (
        "Lakshmi, Diwali prep is starting. Our 'Diwali Glow' package with 20% off is "
        "available for the next 30 days. Want me to promote it?"
    )
    assert find_invented_offer_names(body, SALON, SALON_CATEGORY) == ["Diwali Glow"]
    assert find_unsupported_discount_claims(body, SALON, SALON_CATEGORY) == ["20% off"]


def test_citing_a_real_catalog_offer_is_not_flagged():
    body = (
        "Lakshmi, Diwali is 188 days out. Your Hair Spa @ \u20b9499 package is a good fit for "
        "festive prep. Want me to start promoting it?"
    )
    assert find_invented_offer_names(body, SALON, SALON_CATEGORY) == []


def test_offering_to_create_a_package_is_the_correct_move_and_stays_allowed():
    """The prescribed behaviour when nothing in the catalog fits. Flagging
    this would push the model back toward inventing offers instead."""
    for body in [
        "Lakshmi, Diwali is 188 days out. Want me to draft a Diwali package around your "
        "Hair Spa @ \u20b9499?",
        "Shall I put together a festive package for you? Your Bridal Makeup @ \u20b95,999 could anchor it.",
        "I can create a Diwali offer if you like \u2014 Hair Spa @ \u20b9499 as the base?",
    ]:
        assert find_invented_offer_names(body, SALON, SALON_CATEGORY) == [], body


def test_offer_name_with_no_terms_attached_is_not_flagged():
    """Loose phrasing without a price or discount is not the failure mode
    this gate exists for, and flagging it would cost more than it saves."""
    body = "Lakshmi, worth thinking about a Diwali Glow package this season. Interested?"
    assert find_invented_offer_names(body, SALON, SALON_CATEGORY) == []


def test_discount_supported_as_a_percentage_in_context_is_allowed():
    trigger = {"kind": "festival_upcoming", "fact": "peers ran 20% off festive bundles"}
    body = "Lakshmi, a 20% off festive bundle is what peers are running. Want the same?"
    assert find_unsupported_discount_claims(body, SALON, SALON_CATEGORY, trigger) == []


def test_bare_digit_in_context_does_not_legitimise_a_discount():
    """The exact loophole: "30" is present (days_remaining), but as a count,
    not as a percentage."""
    merchant = dict(SALON, subscription={"status": "active", "days_remaining": 30})
    body = "Get 30% off this week!"
    assert find_unsupported_discount_claims(body, merchant, SALON_CATEGORY) == ["30% off"]


def test_no_discount_claim_means_no_findings():
    body = "Your Hair Spa @ \u20b9499 is live and your calls are down 50% this week."
    assert find_unsupported_discount_claims(body, SALON, SALON_CATEGORY) == []


# ---------------------------------------------------------------------------
# A CTA shape the context cannot support must not be mandated
# ---------------------------------------------------------------------------
#
# The strategy table's CTA for recall_due is multi_choice ("pick Wed 6pm or
# Thu 5pm"), which is correct when the trigger carries real slots. Measured on
# T29 (Zen Yoga / Diya) the trigger payload was the bare placeholder shape
# with no slots at all: Nova wrote a sensible open-ended ask, the gate rejected
# it for not being multi_choice, the retry was rejected identically, and the
# message fell through to a template that scored 10/50 -- "ignores the
# specific customer's history (9 past visits, last visit April 1) and could be
# copy-pasted to any business". The only way to satisfy the demand was to
# invent slot times, which a different gate exists to prevent.

def test_multi_choice_is_not_demanded_without_a_choice_set():
    from composer import _expected_cta, _has_choice_set
    from trigger_router import get_strategy

    strategy = get_strategy("recall_due")
    assert strategy.cta == "multi_choice", "precondition for this test"

    with_slots = {"available_slots": [{"label": "Wed 5 Nov, 6pm"}, {"label": "Thu 6 Nov, 5pm"}]}
    without = {"placeholder": True, "metric_or_topic": "recall_due"}

    assert _has_choice_set(with_slots) is True
    assert _has_choice_set(without) is False

    # Real slots -> the table's multi_choice still stands.
    assert _expected_cta(strategy, "recall_due", False, True, _has_choice_set(with_slots)) == "multi_choice"
    # No slots -> degrade rather than demand the impossible.
    assert _expected_cta(strategy, "recall_due", False, True, _has_choice_set(without)) == "open_ended"
    # No slots but a real offer to point at -> a binary ask is available.
    assert _expected_cta(strategy, "recall_due", True, True, _has_choice_set(without)) == "binary_yes_no"


def test_choice_set_detection_ignores_empty_and_unlabelled_lists():
    from composer import _has_choice_set

    assert _has_choice_set({"available_slots": []}) is False
    assert _has_choice_set({"available_slots": [{}]}) is False
    assert _has_choice_set({"available_slots": [{"label": ""}]}) is False
    assert _has_choice_set(None) is False
    assert _has_choice_set({"next_session_options": ["Sat 3 May, 8am"]}) is True


def test_two_asks_are_counted_even_when_one_is_a_question():
    """The imperative-CTA check used to run only when there were no question
    marks, so a question plus a "Reply 1 for..." list read as a single ask."""
    from fact_check import count_ctas

    double = (
        "Hi Priya, your recall is due on 12th Nov. Which slot works for you? "
        "Reply 1 for Wed 5th Nov, 6pm, 2 for Thu 6th Nov, 5pm, or tell us a time that works."
    )
    assert count_ctas(double) == 2

    # challenge-brief.md Appendix B's gold customer message: an imperative
    # multi_choice CTA with no question mark is still exactly one ask.
    gold = (
        "Hi Priya, Dr. Meera's clinic here. Your 6-month cleaning recall is due. "
        "Reply 1 for Wed, 2 for Thu, or tell us a time that works."
    )
    assert count_ctas(gold) == 1

    assert count_ctas("Want me to draft it?") == 1
    assert count_ctas("No CTA here at all.") == 0


# ---------------------------------------------------------------------------
# Placeholder recall_due grounds in the relationship
# ---------------------------------------------------------------------------

def test_placeholder_recall_due_grounds_in_customer_history():
    """A recall message to a customer whose trigger carries no recall detail
    is exactly the case where the relationship IS the fact. _f_recall_due
    returned None here, which is what left T29 with nothing to say."""
    from trigger_facts import resolve_fact

    customer = {
        "customer_id": "c_034_diya",
        "identity": {"name": "Diya", "language_pref": "en"},
        "relationship": {"first_visit": "2025-09-01", "last_visit": "2026-04-01", "visits_total": 9},
        "state": "lapsed_soft",
    }
    fact = resolve_fact(
        "recall_due",
        {"placeholder": True, "metric_or_topic": "recall_due"},
        category_payload=None,
        merchant_payload={"merchant_id": "m_008", "category_slug": "gyms"},
        customer_payload=customer,
    )
    assert fact, "a placeholder recall_due with real visit history must still yield a fact"
    assert "9 times" in fact
    assert "2026-04-01" in fact

    # A real recall payload must still win over the derived fallback.
    real = resolve_fact(
        "recall_due",
        {"service_due": "6_month_cleaning", "due_date": "2026-11-12"},
        category_payload=None, merchant_payload=None, customer_payload=customer,
    )
    assert "6 month cleaning recall is due" in real
    assert "2026-11-12" in real


# ---------------------------------------------------------------------------
# Free category-catalog offers cited as the merchant's own
# ---------------------------------------------------------------------------
#
# Read straight out of Redis after a real graded run. The category catalogs
# carry free offers with value "0", normalize_category_offer_catalog turns
# that into price=None via `price if price else None`, and the leakage gate
# skipped every price=None entry -- so "Free Consultation", "Free Smile
# Analysis + Digital Scan", "Free Delivery > 500", "Free Starter on orders
# > 1,200" and "Birthday: Free Cake on parties of 6+" were all exempt.
#
# A graded reply to Dr. Meera, whose only active offer is Dental Cleaning @
# 299, ended: "Book now for a free consultation!"

DENTIST_CATEGORY = {
    "slug": "dentists",
    "offer_catalog": [
        {"id": "c1", "title": "Dental Cleaning @ \u20b9299", "value": "299"},
        {"id": "c2", "title": "Free Consultation", "value": "0"},
        {"id": "c3", "title": "Free Smile Analysis + Digital Scan", "value": "0"},
        {"id": "c4", "title": "Teeth Whitening @ \u20b91,499", "value": "1499"},
    ],
}
MEERA = {
    "merchant_id": "m_001_drmeera_dentist_delhi",
    "category_slug": "dentists",
    "identity": {"name": "Dr. Meera's Dental Clinic", "owner_first_name": "Meera"},
    "offers": [
        {"id": "o1", "title": "Dental Cleaning @ \u20b9299", "status": "active"},
        {"id": "o2", "title": "Deep Cleaning @ \u20b9499", "status": "expired"},
    ],
}
RESTAURANT_CATEGORY = {
    "slug": "restaurants",
    "offer_catalog": [
        {"id": "r1", "title": "Buy 1 Pizza Get 1 Free (Tue-Thu)", "value": "BOGO"},
        {"id": "r2", "title": "Free Delivery > \u20b9500", "value": "free_delivery"},
        {"id": "r3", "title": "Weekday Lunch Thali @ \u20b9149", "value": "149"},
    ],
}
PIZZA = {
    "merchant_id": "m_005_pizzajunction_restaurant_delhi",
    "category_slug": "restaurants",
    "identity": {"name": "SK Pizza Junction", "owner_first_name": "Suresh"},
    "offers": [{"id": "p1", "title": "Buy 1 Pizza Get 1 Free (Tue-Thu)", "status": "active"}],
}


def test_free_catalog_offer_asserted_as_bookable_is_caught():
    """The exact body that shipped in a graded run."""
    body = (
        "Great! Let's start with the first draft for your Google post. "
        "Book now for a free consultation!"
    )
    leaked = find_category_offer_leakage(body, MEERA, DENTIST_CATEGORY)
    assert any("Consultation" in x for x in leaked), leaked


def test_free_catalog_offer_claimed_as_theirs_is_caught():
    body = "Suresh, your Free Delivery is live this week \u2014 want me to promote it?"
    leaked = find_category_offer_leakage(body, PIZZA, RESTAURANT_CATEGORY)
    assert any("Free Delivery" in x for x in leaked), leaked


def test_proposing_to_create_a_free_offer_is_allowed():
    """For a merchant with no matching offer this is the CORRECT move, and it
    is what the graded perf_dip message did for a dentist whose offers list is
    literally empty. Flagging it would push the model back toward claiming."""
    for body in [
        "Bharat, your calls are down 50% this week. Should I draft a Free Consultation offer to boost inquiries?",
        "How about a Free Delivery promo to reset expectations?",
        "Want me to set up a Free Smile Analysis + Digital Scan for new patients?",
    ]:
        assert find_category_offer_leakage(body, PIZZA, RESTAURANT_CATEGORY) == [], body
        assert find_category_offer_leakage(body, MEERA, DENTIST_CATEGORY) == [], body


def test_merchant_own_offer_is_never_leakage_even_when_also_in_catalog():
    """SK Pizza Junction really does run the category-standard BOGO."""
    body = "Suresh, your Buy 1 Pizza Get 1 Free (Tue-Thu) is available all week \u2014 push it?"
    assert find_category_offer_leakage(body, PIZZA, RESTAURANT_CATEGORY) == []
    body2 = "Dr. Meera, your Dental Cleaning @ \u20b9299 is available \u2014 shall I promote it?"
    assert find_category_offer_leakage(body2, MEERA, DENTIST_CATEGORY) == []


def test_single_generic_word_does_not_trip_the_free_offer_check():
    """Matching must need the whole citable phrase, not one word of it."""
    body = "Dr. Meera, your consultation slots are filling up \u2014 want me to post about them?"
    assert find_category_offer_leakage(body, MEERA, DENTIST_CATEGORY) == []


# ---------------------------------------------------------------------------
# Clinical honorific and recipient language
# ---------------------------------------------------------------------------

def test_dentist_owner_gets_the_doctor_honorific_from_the_category():
    """The judge's own scoring prompt states the rule for this category ("use
    'Dr.' prefix"). It used to be derived only from the business name, so
    "Dr. Meera's Dental Clinic" produced "Dr. Meera" while "Bharat Dental
    Care" produced a bare "Bharat" -- for a dentist, in a graded message."""
    from fallback import _merchant_greeting_name

    bharat = {
        "merchant_id": "m_002_bharat_dentist_mumbai",
        "category_slug": "dentists",
        "identity": {"name": "Bharat Dental Care", "owner_first_name": "Bharat"},
    }
    assert _merchant_greeting_name(bharat) == "Dr. Bharat"
    assert _merchant_greeting_name(MEERA) == "Dr. Meera"

    # Non-clinical categories must NOT acquire an honorific.
    assert _merchant_greeting_name(PIZZA) == "Suresh"
    salon = {
        "category_slug": "salons",
        "identity": {"name": "Studio11 Family Salon", "owner_first_name": "Lakshmi"},
    }
    assert _merchant_greeting_name(salon) == "Lakshmi"

    # An owner name that already carries the honorific is not doubled.
    already = {"category_slug": "dentists", "identity": {"name": "X", "owner_first_name": "Dr. Asha"}}
    assert _merchant_greeting_name(already) == "Dr. Asha"


def test_customer_language_preference_beats_the_merchants():
    """Priya's own pref is "hi-en mix" and the graded recall message went out
    in pure English, because this read the merchant's language list for every
    message including customer-facing ones."""
    from fallback import _is_hinglish

    english_only_merchant = {"identity": {"languages": ["en"]}}
    hindi_merchant = {"identity": {"languages": ["en", "hi"]}}
    priya = {"identity": {"name": "Priya", "language_pref": "hi-en mix"}}
    english_customer = {"identity": {"name": "Alex", "language_pref": "en"}}

    # Customer's preference wins in both directions.
    assert _is_hinglish(english_only_merchant, priya) is True
    assert _is_hinglish(hindi_merchant, english_customer) is False
    # Merchant-facing is unchanged.
    assert _is_hinglish(hindi_merchant) is True
    assert _is_hinglish(english_only_merchant) is False
    # No stated customer preference falls back to the merchant's languages.
    assert _is_hinglish(hindi_merchant, {"identity": {"name": "Nobody"}}) is True


# ---------------------------------------------------------------------------
# The prompt must separate the merchant's offers from the vertical's templates
# ---------------------------------------------------------------------------

def test_prompt_separates_own_offers_from_category_templates():
    """Root cause of the leak: offer_catalog was dumped inside a JSON blob
    labelled "peer stats, digest, seasonal/trend data", so the model read
    eight ready-made offer titles as things this merchant sells."""
    from category_styles import get_category_style
    from trigger_router import get_strategy
    from vera_persona import build_user_message

    msg = build_user_message(
        "dentists", MEERA,
        {"kind": "research_digest", "fact": "some fact", "payload": {}},
        get_strategy("research_digest"),
        get_category_style("dentists", DENTIST_CATEGORY),
        None, DENTIST_CATEGORY,
    )
    assert "THIS MERCHANT'S OWN LIVE OFFERS" in msg
    assert "Dental Cleaning @ \u20b9299" in msg
    assert "CATEGORY OFFER TEMPLATES" in msg
    assert "NOT this merchant's offers" in msg
    # The catalog must no longer appear inside the category JSON blob.
    assert '"offer_catalog"' not in msg


def test_prompt_states_plainly_when_a_merchant_has_no_offers():
    """m_002 Bharat: offers == [] and a signal that says so. This is where the
    model reached into the category catalog to fill the gap."""
    from category_styles import get_category_style
    from trigger_router import get_strategy
    from vera_persona import build_user_message

    bharat = {
        "merchant_id": "m_002_bharat_dentist_mumbai",
        "category_slug": "dentists",
        "identity": {"name": "Bharat Dental Care", "owner_first_name": "Bharat"},
        "offers": [],
        "signals": ["no_active_offers"],
    }
    msg = build_user_message(
        "dentists", bharat,
        {"kind": "perf_dip", "fact": "your calls are down 50% this week", "payload": {}},
        get_strategy("perf_dip"),
        get_category_style("dentists", DENTIST_CATEGORY),
        None, DENTIST_CATEGORY,
    )
    assert "NO ACTIVE OFFERS AT ALL" in msg
    assert "propose CREATING one" in msg


# ---------------------------------------------------------------------------
# Buried CTA
# ---------------------------------------------------------------------------
#
# A graded recall message ended "... Which works for you? Also, don't forget
# our Dental Cleaning offer at 299." -- one of challenge-brief.md section 11's
# named anti-patterns. Stating the rule in the prompt was not enough: the same
# trailing sentence came back on a later roll, which is why it is a gate.

def test_content_appended_after_the_ask_is_caught():
    from fact_check import find_content_after_cta

    shipped = (
        "Hi Priya, Dr. Meera's clinic here. Your 6-month cleaning recall is due. "
        "We have slots on Wed 5 Nov at 6pm or Thu 6 Nov at 5pm. Which works for you? "
        "Also, don't forget our Dental Cleaning offer at \u20b9299."
    )
    offenders = find_content_after_cta(shipped)
    assert offenders and "don't forget" in offenders[0].lower()


def test_trailing_source_citation_after_the_ask_is_allowed():
    """challenge-brief.md Appendix A's own gold-standard message puts its
    citation after the CTA. A gate that flagged this would be fighting the
    brief's stated benchmark."""
    from fact_check import find_content_after_cta

    gold = (
        "Dr. Meera, JIDA's Oct issue landed. 2,100-patient trial showed 3-month fluoride "
        "recall cuts caries recurrence 38% better than 6-month. Want me to pull it + draft a "
        "patient-ed WhatsApp you can share?  \u2014 JIDA Oct 2026 p.14"
    )
    assert find_content_after_cta(gold) == []


def test_ask_as_final_sentence_is_clean():
    from fact_check import find_content_after_cta

    for body in [
        "Bharat, your calls are down 50% this week. Want me to set up a new offer?",
        "Reply 1 for Wed, 2 for Thu, or tell us a time that works.",
        "Suresh, 4 reviews mention late delivery. What's driving it?",
        "",
        "No ask at all in this message.",
    ]:
        assert find_content_after_cta(body) == [], body


def test_mid_message_question_does_not_trip_the_gate():
    """Only text after the LAST ask counts, otherwise a rhetorical question
    early in the body would flag everything that follows it."""
    from fact_check import find_content_after_cta

    body = (
        "Dr. Meera, remember the fluoride recall question? The JIDA Oct issue answers it with a "
        "2100-patient trial. Should I send you the abstract?"
    )
    assert find_content_after_cta(body) == []


# ---------------------------------------------------------------------------
# Language preference: mechanical, not prompt-only
# ---------------------------------------------------------------------------
#
# The general "match the recipient's language" instruction was stated in the
# prompt and ignored on a real roll: Priya's language_pref is "hi-en mix" and
# a graded recall message went out in pure English. Every other rule that
# turned out to be prompt-only ended up as a gate instead (dropped figures,
# buried CTA, invented offers); this closes the same gap for language.

def test_is_hinglish_text_detects_real_hindi_in_latin_script():
    from fact_check import is_hinglish_text

    assert is_hinglish_text("Kya main aapke offer ko promote karoon?") is True
    assert is_hinglish_text("Aapka 6-month recall due hai") is True
    assert is_hinglish_text("Want me to draft an offer for you?") is False
    assert is_hinglish_text("") is False
    assert is_hinglish_text(None) is False


def test_english_borrowings_do_not_falsely_read_as_hindi():
    """"offer", "book", "confirm", "reply" are English words used in Hinglish
    copy but must not, on their own, make a pure-English sentence look mixed."""
    from fact_check import is_hinglish_text

    assert is_hinglish_text("Reply YES to confirm your offer booking.") is False


def test_find_missing_hinglish_flags_pure_english_for_a_mix_preference():
    from fact_check import find_missing_hinglish

    assert find_missing_hinglish(
        "Hi Priya, Dr. Meera's clinic here. Your 6-month cleaning recall is due. Which slot works for you?",
        "hi-en mix",
    ) is True
    assert find_missing_hinglish(
        "Hi Priya, Dr. Meera's clinic yahan. Aapka 6-month recall due hai. Kaunsa slot theek rahega?",
        "hi-en mix",
    ) is False


def test_find_missing_hinglish_is_a_noop_without_a_mix_preference():
    from fact_check import find_missing_hinglish

    assert find_missing_hinglish("Pure English message.", "en") is False
    assert find_missing_hinglish("Pure English message.", None) is False
    assert find_missing_hinglish("", "hi-en mix") is False


def test_recipient_language_pref_prefers_the_customer_over_the_merchant():
    """A customer-facing message must be judged against the CUSTOMER's own
    preference, not the merchant's language list -- the exact bug that let
    Priya's message ship in English (her merchant, Dr. Meera's clinic, also
    happens to list "hi", so this must not accidentally pass for the wrong
    reason)."""
    from composer import _recipient_language_pref

    merchant_en_only = {"identity": {"languages": ["en"]}}
    merchant_hi = {"identity": {"languages": ["en", "hi"]}}
    priya = {"identity": {"name": "Priya", "language_pref": "hi-en mix"}}
    english_customer = {"identity": {"name": "Alex", "language_pref": "en"}}

    assert _recipient_language_pref(merchant_en_only, priya) == "hi-en mix"
    assert _recipient_language_pref(merchant_hi, english_customer) == "en"
    assert _recipient_language_pref(merchant_hi, None) == "hi-en mix"
    assert _recipient_language_pref(merchant_en_only, None) is None


def test_composer_gate_catches_the_actual_graded_english_only_recall_message():
    """The literal body pattern that shipped for trg_003_recall_due_priya."""
    from composer import _evaluate_draft
    from category_styles import get_category_style

    priya = {"identity": {"name": "Priya", "language_pref": "hi-en mix"}}
    body = (
        "Hi Priya, Dr. Meera's clinic here. Your 6-month cleaning recall is due. "
        "We have slots available on Wed 5 Nov at 6pm or Thu 6 Nov at 5pm. Which works for you?"
    )
    style = get_category_style("dentists", None)
    trigger = {"kind": "recall_due", "fact": "6 month recall due", "payload": {}}
    problems = _evaluate_draft(
        body, MEERA, trigger, priya, style, "recall_due", None, "multi_choice", None,
    )
    assert any("Hindi-English mix" in p for p in problems), problems
