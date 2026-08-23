"""Trigger-kind -> strategy/lever/CTA mapping + eligibility ranking.

Section 3.2 of the build spec: this is an explicit lookup table in code,
not something left to the LLM to guess per-call. Decision logic lives here
so it's testable independently of LLM output variance.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Strategy:
    kind: str
    lever: str
    cta: str
    # relative priority when multiple triggers are eligible for the same
    # merchant in the same tick -- higher wins. Loss aversion / urgency
    # signals outrank curiosity-only ones because they're more likely to
    # drive an immediate reply.
    base_priority: int


TRIGGER_STRATEGY_TABLE: dict[str, Strategy] = {
    "research_digest": Strategy("research_digest", "curiosity + reciprocity", "open_ended", 50),
    "perf_spike": Strategy("perf_spike", "curiosity + opportunity", "open_ended", 55),
    "perf_dip": Strategy("perf_dip", "loss aversion", "binary_yes_no", 70),
    "recall_due": Strategy("recall_due", "specificity + relationship", "multi_choice", 65),
    "festival_upcoming": Strategy("festival_upcoming", "urgency + local relevance", "binary_yes_no", 60),
    "regulation_change": Strategy("regulation_change", "urgency + authority", "binary_yes_no", 80),
    "competitor_opened": Strategy("competitor_opened", "loss aversion + action", "binary_yes_no", 75),
    "milestone_reached": Strategy("milestone_reached", "reciprocity + momentum", "open_ended", 45),
    "curious_ask_due": Strategy("curious_ask_due", "direct question to merchant", "open_ended", 40),
    "dormant_with_vera": Strategy("dormant_with_vera", "light curiosity, low pressure", "open_ended", 30),
    # The 14 additional kinds below all appear in the real official
    # dataset (dataset/triggers_seed.json) and/or the official case
    # studies, but had no strategy mapping -- every one of them was
    # silently falling to DEFAULT_STRATEGY's weak "light curiosity, low
    # pressure" lever regardless of how urgent or specific the real
    # situation actually was (e.g. a drug recall defaulting to the same
    # low-pressure lever as a dormant check-in).
    "supply_alert": Strategy("supply_alert", "urgency + specificity (case study 9)", "open_ended", 85),
    "regulation_change_deadline": Strategy("regulation_change_deadline", "urgency + authority", "binary_yes_no", 80),
    "renewal_due": Strategy("renewal_due", "loss aversion + urgency", "binary_yes_no", 72),
    "winback_eligible": Strategy("winback_eligible", "loss aversion + reciprocity", "binary_yes_no", 62),
    "seasonal_perf_dip": Strategy("seasonal_perf_dip", "anxiety pre-emption + reframe (case study 7)", "open_ended", 42),
    "review_theme_emerged": Strategy("review_theme_emerged", "social proof + specificity", "open_ended", 58),
    "ipl_match_today": Strategy("ipl_match_today", "specificity + contrarian judgment (case study 5)", "open_ended", 56),
    "category_seasonal": Strategy("category_seasonal", "trend awareness + opportunity", "open_ended", 44),
    "gbp_unverified": Strategy("gbp_unverified", "loss aversion (missed visibility)", "binary_yes_no", 48),
    "cde_opportunity": Strategy("cde_opportunity", "reciprocity + low-effort value-add", "open_ended", 35),
    "active_planning_intent": Strategy("active_planning_intent", "effort externalization (case studies 3 & 6)", "binary_yes_no", 90),
    "customer_lapsed_hard": Strategy("customer_lapsed_hard", "no-shame winback (case study 8)", "binary_yes_no", 63),
    "trial_followup": Strategy("trial_followup", "relationship continuity + low-friction booking", "binary_yes_no", 47),
    "wedding_package_followup": Strategy("wedding_package_followup", "relationship continuity + urgency window (case study 3)", "binary_yes_no", 61),
    "chronic_refill_due": Strategy("chronic_refill_due", "specificity + trust (case study 10)", "binary_yes_no", 68),
}

# Safe default for any trigger kind not in the table above (section 7:
# "Unknown trigger kind: default to a safe, generic-but-still-grounded
# strategy rather than erroring the whole tick").
DEFAULT_STRATEGY = Strategy("unknown", "light curiosity, low pressure", "open_ended", 10)


def get_strategy(trigger_kind: str) -> Strategy:
    return TRIGGER_STRATEGY_TABLE.get(trigger_kind, DEFAULT_STRATEGY)


def rank_candidates(candidates: list[dict]) -> list[dict]:
    """candidates: list of dicts each with at least a 'trigger_kind' key
    and optionally a 'value_score' override. Higher-priority strategy
    wins; ties broken by any supplied value_score, then input order
    (stable sort) for determinism."""

    def sort_key(c: dict):
        strategy = get_strategy(c["trigger_kind"])
        value_score = c.get("value_score", 0)
        return (-(strategy.base_priority + value_score),)

    return sorted(candidates, key=sort_key)
