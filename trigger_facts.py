"""Resolves a concrete, groundable `fact` string from a real TriggerContext
payload (dataset/triggers_seed.json shapes), plus category digest lookups
for research_digest/regulation_change/cde_opportunity kinds.

Why this exists: the official dataset's triggers do NOT carry a plain-
English `fact` field (unlike the earlier hand-written test fixtures in
this repo, which did). Real trigger payloads are kind-specific structured
data -- e.g. `{"metric": "calls", "delta_pct": -0.50, "vs_baseline": 12}`
for perf_dip, or `{"category": "dentists", "top_item_id": "d_..."}` for
research_digest, which needs a category-context digest lookup to become
readable at all. Without this resolver, every trigger kind not already
covered by a hand-written test degrades to the composer's generic
"no fact available" fallback -- a real decision-quality and specificity
loss against the actual judge dataset.

Every resolver here only derives text from fields verbatim present in the
trigger payload (or, for digest lookups, the category context's digest
list) -- never invents a number or claim. The derived string is attached
to trigger["fact"] and becomes part of the trigger context object itself,
so downstream fact-verification (fact_check.py) treats it as grounded --
same pattern already used for hand-authored fact strings elsewhere in
this codebase.
"""
from __future__ import annotations


def _pct(value) -> str | None:
    """0.18 -> '18%', -0.30 -> '-30%'. Returns None on non-numeric input."""
    try:
        return f"{round(float(value) * 100):g}%"
    except (TypeError, ValueError):
        return None


def _fmt_num(value) -> str:
    try:
        if isinstance(value, float) and value == int(value):
            return str(int(value))
        return str(value)
    except (TypeError, ValueError):
        return str(value)


def _digest_item(category_payload: dict | None, item_id: str | None) -> dict | None:
    if not category_payload or not item_id:
        return None
    for item in category_payload.get("digest", []) or []:
        if isinstance(item, dict) and item.get("id") == item_id:
            return item
    return None


def _digest_fact(item: dict) -> str:
    title = item.get("title", "")
    source = item.get("source")
    parts = [title] if title else []
    if source:
        parts.append(f"({source})")
    return " ".join(parts).strip()


# Merchant performance field -> matching category peer_stats field, in
# priority order. Used as a social-proof/specificity fallback for trigger
# kinds that carry no event-specific fact of their own (dormant_with_vera,
# curious_ask_due) -- section 10 of the brief calls out "social proof" and
# "asking the merchant" as Vera's two most underused levers today. Rather
# than falling back to pure smalltalk ("noticed you haven't been in touch"),
# ground the check-in in a real, verifiable comparison against category
# peers whenever both numbers are actually present in pushed context.
_PEER_COMPARISON_FIELDS = [
    ("views", "avg_views_30d", "views"),
    ("calls", "avg_calls_30d", "calls"),
    ("directions", "avg_directions_30d", "direction requests"),
    ("ctr", "avg_ctr", "CTR"),
]


def _peer_comparison_fact(merchant_payload: dict | None, category_payload: dict | None) -> str | None:
    """Compares this merchant's real 30d performance numbers against the
    category's real peer_stats benchmark and returns whichever comparison
    is most favorable to lead with (biggest merchant-vs-peer ratio), so
    the check-in has a concrete, verifiable hook instead of none at all.
    Returns None if either side lacks the data -- never invents a number
    on either side of the comparison."""
    if not isinstance(merchant_payload, dict) or not isinstance(category_payload, dict):
        return None
    perf = merchant_payload.get("performance")
    peer_stats = category_payload.get("peer_stats")
    if not isinstance(perf, dict) or not isinstance(peer_stats, dict):
        return None

    best = None
    best_ratio = 0.0
    for merchant_field, peer_field, label in _PEER_COMPARISON_FIELDS:
        mv = perf.get(merchant_field)
        pv = peer_stats.get(peer_field)
        if not isinstance(mv, (int, float)) or not isinstance(pv, (int, float)) or pv == 0:
            continue
        ratio = (mv - pv) / pv
        # Curiosity/dormancy messages should lead with credible strength,
        # not select the largest negative gap while claiming "favorable".
        if ratio >= 0 and ratio > best_ratio:
            best_ratio = ratio
            best = (merchant_field, peer_field, label, mv, pv)

    if best is None:
        return None
    merchant_field, peer_field, label, mv, pv = best
    mv_disp = f"{mv:.3f}".rstrip("0").rstrip(".") if merchant_field == "ctr" else _fmt_num(mv)
    pv_disp = f"{pv:.3f}".rstrip("0").rstrip(".") if peer_field == "avg_ctr" else _fmt_num(pv)
    direction = "above" if mv >= pv else "below"
    return f"your {label} are {mv_disp} vs the area average of {pv_disp} ({direction} peer benchmark)"


# ---------------------------------------------------------------------------
# Per-kind resolvers. Each takes the trigger's inner `payload` dict (i.e.
# trigger_context["payload"], not the whole trigger envelope) plus
# category/merchant/customer context, and returns a fact string or None.
# ---------------------------------------------------------------------------

def _f_research_digest(p: dict, category_payload, merchant_payload, customer_payload) -> str | None:
    item = _digest_item(category_payload, p.get("top_item_id"))
    if item:
        summary = item.get("summary")
        base = _digest_fact(item)
        if item.get("trial_n") and item.get("patient_segment"):
            return f"{base} -- {_fmt_num(item['trial_n'])}-patient trial, {item['patient_segment'].replace('_', ' ')} segment"
        return base or summary
    return None


def _f_regulation_change(p: dict, category_payload, merchant_payload, customer_payload) -> str | None:
    item = _digest_item(category_payload, p.get("top_item_id"))
    base = _digest_fact(item) if item else None
    deadline = p.get("deadline_iso")
    if base and deadline:
        return f"{base}, effective {deadline.split('T')[0]}"
    return base


def _f_cde_opportunity(p: dict, category_payload, merchant_payload, customer_payload) -> str | None:
    item = _digest_item(category_payload, p.get("digest_item_id"))
    base = _digest_fact(item) if item else None
    details = []
    credits = p.get("credits")
    if credits is not None:
        details.append(f"{_fmt_num(credits)} CDE credits")
    fee = p.get("fee")
    if fee:
        details.append(str(fee).replace("_", " "))
    if base and details:
        return f"{base} ({', '.join(details)})"
    return base


def _f_perf_dip(p: dict, *_) -> str | None:
    metric = p.get("metric")
    delta = _pct(p.get("delta_pct"))
    window = p.get("window")
    baseline = p.get("vs_baseline")
    if metric and delta:
        base = f"your {metric} are down {delta.lstrip('-')}"
        if window:
            base += f" this {window}".replace("this 7d", "this week")
        if baseline is not None:
            base += f" (baseline {_fmt_num(baseline)}/day)"
        return base
    return None


def _f_perf_spike(p: dict, *_) -> str | None:
    metric = p.get("metric")
    delta = _pct(p.get("delta_pct"))
    baseline = p.get("vs_baseline")
    driver = p.get("likely_driver")
    if metric and delta:
        base = f"your {metric} are up {delta} this week"
        if baseline is not None:
            base += f" (vs {_fmt_num(baseline)}/day baseline)"
        if driver:
            base += f", likely from {driver.replace('_', ' ')}"
        return base
    return None


def _f_seasonal_perf_dip(p: dict, *_) -> str | None:
    metric = p.get("metric")
    delta = _pct(p.get("delta_pct"))
    note = p.get("season_note")
    if metric and delta:
        base = f"your {metric} are down {delta.lstrip('-')} this week"
        if p.get("is_expected_seasonal") and note:
            base += f" -- expected seasonal pattern ({note.replace('_', ' ')})"
        return base
    return None


def _f_recall_due(p: dict, category_payload, merchant_payload, customer_payload) -> str | None:
    service = p.get("service_due")
    due = p.get("due_date")
    last_service = p.get("last_service_date")
    slots = p.get("available_slots") or []
    if service:
        base = f"{service.replace('_', ' ')} recall is due"
        if due:
            base += f" ({due})"
        if last_service:
            base += f", last service {last_service}"
        labels = [s.get("label") for s in slots if isinstance(s, dict) and s.get("label")]
        if labels:
            base += f", slots: {' or '.join(labels)}"
        return base
    return None


def _f_renewal_due(p: dict, *_) -> str | None:
    days = p.get("days_remaining")
    plan = p.get("plan")
    amount = p.get("renewal_amount")
    if days is not None:
        base = f"your {plan or 'subscription'} plan renews in {_fmt_num(days)} days"
        if amount:
            base += f" (\u20b9{_fmt_num(amount)})"
        return base
    return None


def _f_festival_upcoming(p: dict, *_) -> str | None:
    festival = p.get("festival")
    days = p.get("days_until")
    if festival and days is not None:
        return f"{festival} is {_fmt_num(days)} days away"
    return None


def _f_wedding_package_followup(p: dict, *_) -> str | None:
    days = p.get("days_to_wedding")
    step = p.get("next_step_window_open")
    if days is not None:
        base = f"{_fmt_num(days)} days to the wedding"
        if step:
            base += f" -- {step.replace('_', ' ')} window is open"
        return base
    return None


def _f_winback_eligible(p: dict, *_) -> str | None:
    days = p.get("days_since_expiry")
    dip = _pct(p.get("perf_dip_pct"))
    lapsed = p.get("lapsed_customers_added_since_expiry")
    if days is not None:
        base = f"subscription expired {_fmt_num(days)} days ago"
        if dip:
            base += f", performance down {dip.lstrip('-')} since"
        if lapsed:
            base += f" ({_fmt_num(lapsed)} customers lapsed since)"
        return base
    return None


def _f_ipl_match_today(p: dict, *_) -> str | None:
    match = p.get("match")
    venue = p.get("venue")
    match_time = p.get("match_time_iso")
    if match:
        base = f"{match} today"
        if venue:
            base += f" at {venue}"
        if match_time:
            base += f", starts {str(match_time).split('T')[-1]}"
        if p.get("is_weeknight") is False:
            base += ", not a weeknight"
        elif p.get("is_weeknight") is True:
            base += ", on a weeknight"
        return base
    return None


def _f_review_theme_emerged(p: dict, *_) -> str | None:
    theme = p.get("theme")
    occ = p.get("occurrences_30d")
    trend = p.get("trend")
    quote = p.get("common_quote")
    if theme and occ is not None:
        base = f"\"{theme.replace('_', ' ')}\" mentioned in {_fmt_num(occ)} reviews this month"
        if trend:
            base += f" ({trend})"
        if quote:
            base += f"; one customer wrote: \"{quote}\""
        return base
    return None


def _f_milestone_reached(p: dict, *_) -> str | None:
    metric = p.get("metric")
    now = p.get("value_now")
    target = p.get("milestone_value")
    if metric and now is not None and target is not None:
        return f"{_fmt_num(now)} {metric.replace('_', ' ')}, {_fmt_num(target)} milestone is close"
    return None


def _f_active_planning_intent(p: dict, *_) -> str | None:
    topic = p.get("intent_topic")
    last_msg = p.get("merchant_last_message")
    if topic:
        base = topic.replace("_", " ")
        if last_msg:
            return f"{base} -- you said: \"{last_msg}\""
        return base
    return None


def _f_customer_lapsed(p: dict, *_) -> str | None:
    days = p.get("days_since_last_visit")
    focus = p.get("previous_focus")
    months = p.get("previous_membership_months")
    if days is not None:
        base = f"it's been {_fmt_num(days)} days since your last visit"
        if focus:
            base += f", previously focused on {focus.replace('_', ' ')}"
        if months:
            base += f" ({_fmt_num(months)}-month member before)"
        return base
    return None


def _f_trial_followup(p: dict, *_) -> str | None:
    trial_date = p.get("trial_date")
    options = p.get("next_session_options") or []
    if trial_date:
        base = f"trial session on {trial_date}"
        if options and isinstance(options[0], dict) and options[0].get("label"):
            base += f", next slot {options[0]['label']}"
        return base
    return None


def _f_supply_alert(p: dict, *_) -> str | None:
    molecule = p.get("molecule")
    batches = p.get("affected_batches") or []
    manufacturer = p.get("manufacturer")
    if molecule:
        base = f"recall on {molecule}"
        if batches:
            base += f" (batches {', '.join(batches)})"
        if manufacturer:
            base += f" by {manufacturer}"
        return base
    return None


def _f_chronic_refill_due(p: dict, category_payload, merchant_payload, customer_payload) -> str | None:
    molecules = p.get("molecule_list") or []
    runs_out = p.get("stock_runs_out_iso")
    if molecules:
        base = f"refill due for {', '.join(molecules)}"
        if runs_out:
            base += f" (stock runs out {runs_out.split('T')[0]})"
        if p.get("delivery_address_saved"):
            base += ", delivery address saved"
        preferences = customer_payload.get("preferences", {}) if isinstance(customer_payload, dict) else {}
        channel = preferences.get("channel")
        slot = preferences.get("preferred_slots")
        if channel:
            base += f", contact via {str(channel).replace('_', ' ')}"
        if slot:
            base += f", prefers {str(slot).replace('_', ' ')}"
        return base
    return None


def _f_category_seasonal(p: dict, *_) -> str | None:
    season = p.get("season")
    trends = p.get("trends") or []
    if trends:
        top = trends[0].replace("_", " ")
        base = f"{top}"
        if season:
            base += f" this {season.replace('_', ' ')}"
        return base
    return None


def _f_gbp_unverified(p: dict, *_) -> str | None:
    uplift = _pct(p.get("estimated_uplift_pct"))
    if uplift:
        return f"verifying your profile could lift visibility by ~{uplift}"
    return "your Google profile isn't verified yet"


def _f_competitor_opened(p: dict, *_) -> str | None:
    name = p.get("competitor_name")
    dist = p.get("distance_km")
    offer = p.get("their_offer")
    if name:
        base = f"{name} opened"
        if dist is not None:
            base += f" {_fmt_num(dist)}km away"
        if offer:
            base += f" with {offer}"
        return base
    return None


def _f_dormant_with_vera(p: dict, category_payload, merchant_payload, customer_payload) -> str | None:
    days = p.get("days_since_last_merchant_message")
    peer_fact = _peer_comparison_fact(merchant_payload, category_payload)
    if days is not None and peer_fact:
        return f"it's been {_fmt_num(days)} days since we last talked -- {peer_fact}"
    if days is not None:
        return f"it's been {_fmt_num(days)} days since we last talked"
    return peer_fact


def _f_curious_ask_due(p: dict, category_payload, merchant_payload, customer_payload) -> str | None:
    # This trigger kind is a scheduled cadence prompt, not an event, so
    # the trigger's own payload never carries an event-specific fact.
    # Ground it in a real category-peer comparison instead of a fully
    # generic question whenever the numbers are actually available --
    # this is the "social proof" lever (brief section 10, #3), currently
    # Vera's most underused per the brief's own framing.
    return _peer_comparison_fact(merchant_payload, category_payload)


_RESOLVERS = {
    "research_digest": _f_research_digest,
    "regulation_change": _f_regulation_change,
    "cde_opportunity": _f_cde_opportunity,
    "perf_dip": _f_perf_dip,
    "perf_spike": _f_perf_spike,
    "seasonal_perf_dip": _f_seasonal_perf_dip,
    "recall_due": _f_recall_due,
    "renewal_due": _f_renewal_due,
    "festival_upcoming": _f_festival_upcoming,
    "wedding_package_followup": _f_wedding_package_followup,
    "winback_eligible": _f_winback_eligible,
    "ipl_match_today": _f_ipl_match_today,
    "review_theme_emerged": _f_review_theme_emerged,
    "milestone_reached": _f_milestone_reached,
    "active_planning_intent": _f_active_planning_intent,
    "customer_lapsed_hard": _f_customer_lapsed,
    "customer_lapsed_soft": _f_customer_lapsed,
    "trial_followup": _f_trial_followup,
    "supply_alert": _f_supply_alert,
    "chronic_refill_due": _f_chronic_refill_due,
    "category_seasonal": _f_category_seasonal,
    "gbp_unverified": _f_gbp_unverified,
    "competitor_opened": _f_competitor_opened,
    "dormant_with_vera": _f_dormant_with_vera,
    "curious_ask_due": _f_curious_ask_due,
}


def resolve_fact(
    trigger_kind: str,
    trigger_payload: dict | None,
    category_payload: dict | None = None,
    merchant_payload: dict | None = None,
    customer_payload: dict | None = None,
) -> str | None:
    """trigger_payload is the trigger's INNER `payload` dict (i.e.
    trigger_context["payload"] from the dataset shape), not the whole
    trigger envelope. Returns None (never raises) if the kind is unknown
    or required fields are missing -- callers already handle fact=None
    via the composer's degraded-input rules."""
    if not isinstance(trigger_payload, dict):
        return None
    resolver = _RESOLVERS.get(trigger_kind)
    if resolver is None:
        return None
    try:
        return resolver(trigger_payload, category_payload, merchant_payload, customer_payload)
    except Exception:
        return None
