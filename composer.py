"""LLM prompt construction + call + fact-verification post-check.

Product priority, in order:
  1. Get a real, on-strategy message out of Nova. This is always tried
     first for every request.
  2. If Nova's first draft fails any quality gate (invented facts, banned
     phrase, wrong CTA count, missing a citable offer), give it exactly
     one corrected attempt with the specific problems named -- Nova is
     good at fixing a named issue, so this recovers a lot of otherwise-
     wasted first drafts.
  3. Only if both the first draft and the correction attempt fail (or
     Bedrock is unreachable/unconfigured/times out entirely) do we fall
     back to the deterministic template library in fallback.py. The
     fallback library is deliberately large and covers every trigger
     kind, Hinglish, and customer-directed messaging so it is never a
     degraded experience, just a different (more conservative) source of
     wording.

Section 7 of the build spec: "a downstream API failure must never become
an unhandled exception or a timeout past 30s." Every external call in
this module is wrapped so failures degrade gracefully rather than
propagate.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

# Load .env here too, not just in main.py. main.py calls load_dotenv()
# before importing this module, which covers the real server process --
# but anything that imports composer.py directly (a standalone script, a
# test file that doesn't first import main.py, a future entry point)
# would otherwise silently miss AWS_BEARER_TOKEN_BEDROCK/AWS_REGION and
# fall back to the deterministic templates without any obvious error.
# load_dotenv() is idempotent and a no-op if called twice, so this is safe
# even when main.py already called it.
load_dotenv()

from category_styles import get_category_style, violates_banned_phrases
from fact_check import count_ctas, find_urls, verify_numeric_claims
from fallback import compose_fallback, compose_reply_grounded_fallback
from offers import find_category_offer_leakage, format_offer, normalize_merchant_offers, select_relevant_offer
from state_backend import StateBackend
from trigger_router import get_strategy
from vera_persona import (
    REPLY_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_correction_message,
    build_reply_correction_message,
    build_reply_user_message,
    build_user_message,
)

logger = logging.getLogger("vera_bot.composer")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")
BEDROCK_REGION = os.environ.get("AWS_REGION", "us-east-1")
LLM_TIMEOUT_SECONDS = 8  # generous headroom under the 30s endpoint contract
LLM_MAX_ATTEMPTS = 2  # first draft + one named-correction retry, then fallback

# Nova's valid temperature range is (0.00001, 1] -- exactly 0 is rejected by
# the API. This is the smallest allowed value, effectively deterministic
# alongside topK=1 (see _invoke_nova below).
NOVA_MIN_TEMPERATURE = 0.00001

# Trigger kinds where, if the merchant has a matching offer in their
# catalog, decision quality requires citing it (name + price) rather than
# leaving it to the LLM's discretion whether to mention it. Section 3.2:
# "a real lookup/strategy table in code, not left to the LLM to guess."
OFFER_CITATION_REQUIRED_KINDS = {
    "research_digest",
    "perf_spike",
    "perf_dip",
    "recall_due",
    "festival_upcoming",
    "competitor_opened",
    "milestone_reached",
    "winback_eligible",
    "wedding_package_followup",
    "customer_lapsed_hard",
    "trial_followup",
}

# Cross-call memoization so repeated identical (category, merchant, trigger,
# customer) inputs return byte-identical output even though the underlying
# LLM is not perfectly deterministic (Bedrock's Nova Invoke API forbids
# temperature=0 exactly and exposes no seed parameter). The challenge spec
# requires "same input -> same output"; this cache is what actually
# guarantees that property at the app level.
_compose_cache = StateBackend("compose_cache")

_bedrock_client = None
_bedrock_init_failed = False


def _cache_key(
    category: str,
    merchant_payload: dict,
    trigger: dict,
    customer_payload: dict | None,
    category_payload: dict | None,
) -> str:
    blob = json.dumps(
        {
            "cache_version": 2,
            "category": category,
            "merchant": merchant_payload,
            "trigger": trigger,
            "customer": customer_payload,
            "category_ctx": category_payload,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Bedrock client + raw invocation
# ---------------------------------------------------------------------------

def _get_bedrock_client():
    """Builds the bedrock-runtime client. Auth precedence (handled entirely
    by boto3/botocore, nothing custom here):
      1. AWS_BEARER_TOKEN_BEDROCK env var -- API-key/bearer-token auth
         (local dev convenience, short- or long-term key from the console).
      2. Standard AWS credential chain -- env keys, ~/.aws/credentials, or
         (in production on EC2) the attached instance IAM role.
    Returns None if boto3 isn't installed or client construction fails
    outright (e.g. bad region string). Actual auth failures surface later
    as an invoke_model exception, caught below, so a bad/missing key
    degrades to the fallback composer rather than crashing the request.
    """
    global _bedrock_client, _bedrock_init_failed
    if _bedrock_init_failed:
        return None
    if _bedrock_client is not None:
        return _bedrock_client
    try:
        import boto3
        from botocore.config import Config

        _bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=BEDROCK_REGION,
            config=Config(connect_timeout=3, read_timeout=LLM_TIMEOUT_SECONDS, retries={"max_attempts": 1}),
        )
        return _bedrock_client
    except Exception as exc:  # no creds / no boto3 / no network -- expected in local dev
        logger.info("bedrock client unavailable, will use deterministic fallback: %s", exc)
        _bedrock_init_failed = True
        return None


def _parse_nova_text(raw_text: str) -> dict | None:
    """Nova is instructed never to wrap output in markdown fences, but
    models don't always follow formatting instructions perfectly -- strip
    fences defensively before parsing, and never raise on malformed JSON."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Nova output was not valid JSON, discarding: %s | raw=%r", exc, text[:300])
        return None
    if not isinstance(parsed, dict) or "body" not in parsed or not isinstance(parsed.get("body"), str):
        logger.warning("Nova output missing required 'body' string field: %r", parsed)
        return None
    return parsed


def _is_transient_bedrock_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return any(token in name or token in message for token in (
        "throttl", "timeout", "temporar", "connection", "too many requests", "service unavailable",
    ))


@dataclass(frozen=True)
class _BedrockOutcome:
    value: dict | None
    failure: str | None = None  # transient | unavailable | malformed


def _invoke_bedrock(user_message: str, system_prompt: str = SYSTEM_PROMPT) -> _BedrockOutcome:
    """Typed Nova invocation used internally for cache decisions."""
    client = _get_bedrock_client()
    if client is None:
        return _BedrockOutcome(None, "unavailable")
    try:
        response = client.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=json.dumps(
                {
                    "system": [{"text": system_prompt}],
                    "messages": [{"role": "user", "content": [{"text": user_message}]}],
                    "inferenceConfig": {
                        "temperature": NOVA_MIN_TEMPERATURE,
                        "topP": 1,
                        "topK": 1,
                        "maxTokens": 500,
                    },
                }
            ),
        )
    except Exception as exc:
        logger.warning("bedrock invoke_model call failed (%s: %s)", type(exc).__name__, exc)
        return _BedrockOutcome(None, "transient" if _is_transient_bedrock_error(exc) else "unavailable")

    try:
        payload = json.loads(response["body"].read())
        content_blocks = payload["output"]["message"]["content"]
        text = next((b["text"] for b in content_blocks if "text" in b), None)
        if text is None:
            logger.warning("bedrock response had no text content block: %r", payload)
            return _BedrockOutcome(None, "malformed")
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("bedrock response shape was unexpected (%s): %s", type(exc).__name__, exc)
        return _BedrockOutcome(None, "malformed")

    parsed = _parse_nova_text(text)
    return _BedrockOutcome(parsed, None if parsed is not None else "malformed")


def _call_bedrock(user_message: str, system_prompt: str = SYSTEM_PROMPT) -> dict | None:
    """Backward-compatible raw-call helper used by the smoke test."""
    return _invoke_bedrock(user_message, system_prompt).value


# ---------------------------------------------------------------------------
# Quality gates run against every Nova draft before it's accepted
# ---------------------------------------------------------------------------

def _first_offer(
    merchant_payload: dict,
    trigger_kind: str | None = None,
    trigger_fact: str | None = None,
    trigger_payload: dict | None = None,
    customer_payload: dict | None = None,
) -> dict | None:
    """Backward-compatible offer helper.

    A trigger-aware call returns only a positively matched priced offer.
    The no-trigger form remains available for normalization smoke tests,
    but production composition always passes trigger context.
    """
    if not any((trigger_kind, trigger_fact, trigger_payload, customer_payload)):
        priced = [o for o in normalize_merchant_offers(merchant_payload) if o.get("price") is not None]
        return priced[0] if priced else None
    return select_relevant_offer(
        merchant_payload, trigger_kind, trigger_fact, trigger_payload, customer_payload,
    )


def _offer_is_cited(body: str, offer: dict) -> bool:
    name_words = [w.lower() for w in str(offer.get("name", "")).split() if len(w) > 2]
    name_hit = bool(name_words) and sum(w in body.lower() for w in name_words) >= max(1, len(name_words) // 2)
    price = offer.get("price")
    price_hit = price is not None and re.search(
        rf"(?<!\d){re.escape(str(price))}(?!\d)", body.replace(",", "")
    ) is not None
    return name_hit and price_hit


def _expected_cta(strategy, trigger_kind: str, offer_cited: bool, is_customer_facing: bool = False) -> str:
    """The strategy table's default CTA per trigger kind is right most of
    the time, but a citable offer changes the shape of the ask for
    MERCHANT-facing messages: once a concrete priced offer is on the
    table, the natural next question is a yes/no ("should I send it?"),
    not an open-ended one. This mirrors section 11's own worked example,
    which explicitly overrides research_digest's table default of
    open_ended to binary_yes_no once a matching offer exists.

    This override does NOT apply to customer-facing messages: case study
    2 (recall_due, customer-facing) cites a real offer+price AND uses
    multi_choice (pick-a-slot), which is the correct shape for a
    booking/recall ask even with a priced offer in the message -- forcing
    binary_yes_no there would be a decision-quality regression, not an
    improvement."""
    if trigger_kind in OFFER_CITATION_REQUIRED_KINDS and offer_cited and not is_customer_facing:
        return "binary_yes_no"
    return strategy.cta


def _evaluate_draft(
    body_text: str,
    merchant_payload: dict,
    trigger: dict,
    customer_payload: dict | None,
    style: dict,
    trigger_kind: str,
    offer: dict | None,
    raw_cta_hint: str | None = None,
    category_payload: dict | None = None,
) -> list[str]:
    is_customer_facing = customer_payload is not None
    """Runs every quality gate and returns a list of human-readable problem
    descriptions (empty list = draft passes everything)."""
    problems: list[str] = []

    valid, unverifiable = verify_numeric_claims(body_text, merchant_payload, trigger, customer_payload, category_payload)
    if not valid:
        problems.append(
            f"These numbers appear in your message but not verbatim anywhere in the given context: "
            f"{', '.join(unverifiable)}. Remove or replace them with only numbers present in the context."
        )

    banned_hits = violates_banned_phrases(body_text, style.get("banned_phrases", []))
    if banned_hits:
        problems.append(
            f"Your message uses these banned phrases for this category: {', '.join(banned_hits)}. "
            "Rewrite without them."
        )

    urls = find_urls(body_text)
    if urls:
        problems.append(
            f"Your message contains a URL ({', '.join(urls)}) that is not present verbatim in the "
            "given context. Never invent or include a URL that wasn't explicitly given to you -- "
            "remove it entirely."
        )

    leaked_offers = find_category_offer_leakage(body_text, merchant_payload, category_payload)
    if leaked_offers:
        problems.append(
            f"Your message cites {', '.join(leaked_offers)} as if it were this merchant's own live "
            "offer, but it only exists in the CATEGORY CONTEXT's generic offer_catalog, not in this "
            "merchant's own MERCHANT CONTEXT offers list. Never attribute a category-level template "
            "offer to this specific merchant -- either cite an offer that's actually in their own "
            "catalog, or don't cite a specific priced offer at all."
        )

    offer_cited = _offer_is_cited(body_text, offer) if offer is not None else False
    if trigger_kind in OFFER_CITATION_REQUIRED_KINDS and offer is not None and not offer_cited:
        problems.append(
            f"A relevant offer exists in the merchant's catalog ('{offer.get('name')}' at "
            f"{offer.get('price')}) but your message did not name it and its exact price. "
            "Cite the offer name and price explicitly."
        )

    raw_cta = raw_cta_hint
    expected_cta = _expected_cta(get_strategy(trigger_kind), trigger_kind, offer_cited, is_customer_facing)
    if raw_cta is not None and raw_cta != expected_cta:
        problems.append(
            f"You used cta='{raw_cta}' but this situation requires cta='{expected_cta}' "
            f"({'a citable offer exists, so the ask should be a direct yes/no' if expected_cta == 'binary_yes_no' else 'per the required CTA type for this trigger'})."
        )

    cta_count = count_ctas(body_text)
    if cta_count == 0:
        problems.append("Your message has no question/call-to-action. Add exactly one.")
    elif cta_count > 1:
        problems.append(
            f"Your message has {cta_count} question marks / asks. Rewrite with exactly one call-to-action."
        )

    return problems


def _finalize_llm_result(raw: dict, strategy, trigger_kind: str, offer: dict | None, is_customer_facing: bool = False) -> dict:
    rationale = raw.get("rationale") or f"{trigger_kind} trigger -> {strategy.lever} lever"
    if offer is None and "\u20b9" not in raw["body"] and "offer" not in rationale.lower():
        rationale += " (no matching offer in catalog, no price cited)"
    offer_cited = _offer_is_cited(raw["body"], offer) if offer is not None else False
    final_cta = _expected_cta(strategy, trigger_kind, offer_cited, is_customer_facing)
    return {
        "body": raw["body"],
        "cta": final_cta,
        "rationale": rationale,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compose(
    category: str,
    merchant_payload: dict,
    trigger: dict,
    customer_payload: dict | None = None,
    category_payload: dict | None = None,
) -> dict:
    """Returns {body, cta, rationale} for a single outbound action.

    trigger = {"kind": ..., "fact": ..., "trigger_id": ...}

    Deterministic by design: identical inputs return a cached, byte-identical
    result rather than re-invoking the LLM every time, since Bedrock's Nova
    Invoke API has no seed parameter and doesn't accept temperature=0
    exactly -- so two live calls with the same input can legitimately
    differ in wording.

    Never raises: every internal step is wrapped so a total failure (bad
    context shape, Bedrock down, malformed model output) still returns a
    valid dict from the fallback library rather than propagating an
    exception up to the FastAPI route handler.
    """
    try:
        cache_key = _cache_key(category, merchant_payload, trigger, customer_payload, category_payload)
        cached = _compose_cache.get(cache_key)
        if cached is not None:
            return cached
    except Exception as exc:
        logger.warning("compose cache lookup failed, proceeding without cache: %s", exc)
        cache_key = None

    trigger_kind = trigger.get("kind", "") if isinstance(trigger, dict) else ""
    strategy = get_strategy(trigger_kind)
    style = get_category_style(category, category_payload)
    offer = _first_offer(
        merchant_payload if isinstance(merchant_payload, dict) else {},
        trigger_kind,
        trigger.get("fact") if isinstance(trigger, dict) else None,
        trigger.get("payload") if isinstance(trigger, dict) and isinstance(trigger.get("payload"), dict) else None,
        customer_payload,
    )

    llm_result, fallback_reason = _try_llm_with_correction(
        category, merchant_payload, trigger, customer_payload, style, strategy, trigger_kind, offer, category_payload
    )

    result = llm_result if llm_result is not None else _build_fallback_result(
        trigger_kind, trigger, merchant_payload, customer_payload, category_payload
    )

    # A transient throttle/timeout must not permanently poison the normal
    # deterministic cache with lower-quality fallback text. Stable quality
    # failures and permanent unavailability remain cacheable.
    if cache_key is not None and fallback_reason != "transient":
        try:
            _compose_cache.set(cache_key, result)
        except Exception as exc:
            logger.warning("compose cache write failed (non-fatal): %s", exc)

    return result


def _try_llm_with_correction(category, merchant_payload, trigger, customer_payload, style, strategy, trigger_kind, offer, category_payload=None) -> tuple[dict | None, str | None]:
    """Return ``(accepted_result, fallback_reason)``. A transient first
    failure stops immediately to avoid multiplying a throttle burst."""
    try:
        user_message = build_user_message(category, merchant_payload, trigger, strategy, style, customer_payload, category_payload)
        effective_cta = _expected_cta(strategy, trigger_kind, offer is not None, customer_payload is not None)
        if effective_cta != strategy.cta:
            user_message += f"\n\nEFFECTIVE CTA OVERRIDE FOR THIS MESSAGE: {effective_cta} (a semantically relevant merchant offer was selected)."
    except Exception as exc:
        logger.error("failed to build Nova prompt from context (%s), skipping LLM path", exc)
        return None, "quality"

    outcome = _invoke_bedrock(user_message)
    raw = outcome.value
    attempt = 1
    problems: list[str] = []

    while attempt <= LLM_MAX_ATTEMPTS:
        if raw is None:
            if outcome.failure == "transient":
                logger.info("Nova attempt %d hit a transient failure; falling back without an immediate retry", attempt)
                return None, "transient"
            logger.info("Nova attempt %d produced no usable output, %s", attempt, "retrying" if attempt < LLM_MAX_ATTEMPTS else "falling back")
        else:
            problems = _evaluate_draft(
                raw["body"], merchant_payload, trigger, customer_payload, style, trigger_kind, offer, raw.get("cta"),
                category_payload,
            )
            if not problems:
                return _finalize_llm_result(raw, strategy, trigger_kind, offer, customer_payload is not None), None
            logger.info("Nova attempt %d failed quality gates: %s", attempt, problems)

        if attempt >= LLM_MAX_ATTEMPTS:
            break

        try:
            correction_prompt = build_correction_message(
                category,
                merchant_payload,
                trigger,
                strategy,
                style,
                previous_body=raw["body"] if raw else "(no output produced)",
                problems=problems or ["No usable response was produced. Try again with a valid JSON object."],
                customer_payload=customer_payload,
                category_payload=category_payload,
            )
        except Exception as exc:
            logger.error("failed to build Nova correction prompt (%s), giving up on LLM path", exc)
            return None, "quality"

        outcome = _invoke_bedrock(correction_prompt)
        raw = outcome.value
        attempt += 1

    logger.info("Nova exhausted %d attempt(s) without an accepted draft, using deterministic fallback", LLM_MAX_ATTEMPTS)
    return None, "transient" if outcome.failure == "transient" else "quality"


def _build_fallback_result(trigger_kind: str, trigger: dict, merchant_payload: dict, customer_payload: dict | None, category_payload: dict | None = None) -> dict:
    fallback_result = compose_fallback(
        trigger_kind,
        trigger.get("fact") if isinstance(trigger, dict) else None,
        merchant_payload,
        customer_payload,
        trigger.get("payload") if isinstance(trigger, dict) else None,
    )
    # Defensive re-check even on our own templates (cheap, catches future
    # template bugs before they reach a real merchant).
    try:
        valid, unverifiable = verify_numeric_claims(fallback_result["body"], merchant_payload, trigger, customer_payload, category_payload)
        if not valid:
            logger.error("fallback template produced unverifiable numbers: %s", unverifiable)
    except Exception as exc:
        logger.error("fallback verification step itself failed (%s)", exc)
    return fallback_result


# ---------------------------------------------------------------------------
# /v1/reply composition -- grounded, LLM-backed reply generation
# ---------------------------------------------------------------------------
#
# This mirrors the tick-composer's "try Nova, verify, one named correction,
# else deterministic fallback" pipeline, but for in-thread replies: the
# quality gates that matter here are zero-invented-facts (same fact_check
# module) and at-most-one-CTA -- offer-citation and CTA-type enforcement
# don't apply the same way mid-conversation, since the ask may legitimately
# be a plain confirmation with no question at all.

_reply_cache = StateBackend("compose_reply_cache")


def _reply_cache_key(intent: str, incoming_message: str, merchant_payload: dict | None, trigger_payload: dict | None, customer_payload: dict | None) -> str:
    blob = json.dumps(
        {
            "cache_version": 2,
            "intent": intent,
            "message": incoming_message,
            "merchant": merchant_payload,
            "trigger": trigger_payload,
            "customer": customer_payload,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _evaluate_reply_draft(body_text: str, merchant_payload: dict | None, trigger_payload: dict | None, customer_payload: dict | None, style: dict, category_payload: dict | None = None) -> list[str]:
    problems: list[str] = []
    valid, unverifiable = verify_numeric_claims(body_text, merchant_payload, trigger_payload, customer_payload, category_payload)
    if not valid:
        problems.append(
            f"These numbers appear in your reply but not verbatim anywhere in the given context: "
            f"{', '.join(unverifiable)}. Remove or replace them with only numbers present in the context."
        )
    banned_hits = violates_banned_phrases(body_text, style.get("banned_phrases", []))
    if banned_hits:
        problems.append(f"Your reply uses these banned phrases for this category: {', '.join(banned_hits)}. Rewrite without them.")
    urls = find_urls(body_text)
    if urls:
        problems.append(
            f"Your reply contains a URL ({', '.join(urls)}) not present verbatim in context. Never "
            "invent or include a URL -- remove it entirely."
        )
    leaked_offers = find_category_offer_leakage(body_text, merchant_payload, category_payload)
    if leaked_offers:
        problems.append(
            f"Your reply cites {', '.join(leaked_offers)} as if it were this merchant's own live offer, "
            "but it's only a generic category-level template, not something in this merchant's own "
            "offers list. Remove it or cite an offer actually in their catalog."
        )
    cta_count = count_ctas(body_text)
    if cta_count > 1:
        problems.append(f"Your reply has {cta_count} question marks / asks. Rewrite with at most one call-to-action.")
    return problems


def compose_reply(
    intent: str,
    incoming_message: str,
    merchant_payload: dict | None = None,
    trigger_payload: dict | None = None,
    customer_payload: dict | None = None,
    category: str | None = None,
    category_payload: dict | None = None,
) -> dict:
    """Returns {body, rationale} for the next /v1/reply message, grounded
    in the real merchant/trigger context for this thread rather than a
    static per-intent template. Same reliability guarantees as compose():
    cached for determinism, never raises, always falls back to a
    deterministic-but-still-grounded template if Nova is unavailable or
    fails verification twice.
    """
    try:
        cache_key = _reply_cache_key(intent, incoming_message, merchant_payload, trigger_payload, customer_payload)
        cached = _reply_cache.get(cache_key)
        if cached is not None:
            return cached
    except Exception as exc:
        logger.warning("reply compose cache lookup failed, proceeding without cache: %s", exc)
        cache_key = None

    style = get_category_style(category or "", category_payload)

    result, fallback_reason = _try_reply_llm_with_correction(
        intent, incoming_message, merchant_payload, trigger_payload, customer_payload, style, category_payload
    )
    if result is None:
        result = compose_reply_grounded_fallback(intent, incoming_message, merchant_payload, trigger_payload, customer_payload)

    if cache_key is not None and fallback_reason != "transient":
        try:
            _reply_cache.set(cache_key, result)
        except Exception as exc:
            logger.warning("reply compose cache write failed (non-fatal): %s", exc)

    return result


def _try_reply_llm_with_correction(intent, incoming_message, merchant_payload, trigger_payload, customer_payload, style, category_payload=None) -> tuple[dict | None, str | None]:
    try:
        user_message = build_reply_user_message(intent, incoming_message, merchant_payload, trigger_payload, style, customer_payload, category_payload)
    except Exception as exc:
        logger.error("failed to build Nova reply prompt (%s), skipping LLM path", exc)
        return None, "quality"

    outcome = _invoke_bedrock(user_message, system_prompt=REPLY_SYSTEM_PROMPT)
    raw = outcome.value
    attempt = 1
    problems: list[str] = []

    while attempt <= LLM_MAX_ATTEMPTS:
        if raw is None:
            if outcome.failure == "transient":
                logger.info("Nova reply attempt %d hit a transient failure; falling back without an immediate retry", attempt)
                return None, "transient"
            logger.info("Nova reply attempt %d produced no usable output, %s", attempt, "retrying" if attempt < LLM_MAX_ATTEMPTS else "falling back")
        else:
            problems = _evaluate_reply_draft(raw["body"], merchant_payload, trigger_payload, customer_payload, style, category_payload)
            if not problems:
                return {"body": raw["body"], "rationale": raw.get("rationale") or f"LLM-grounded reply for intent={intent}"}, None
            logger.info("Nova reply attempt %d failed quality gates: %s", attempt, problems)

        if attempt >= LLM_MAX_ATTEMPTS:
            break

        try:
            correction_prompt = build_reply_correction_message(
                intent,
                incoming_message,
                merchant_payload,
                trigger_payload,
                style,
                previous_body=raw["body"] if raw else "(no output produced)",
                problems=problems or ["No usable response was produced. Try again with a valid JSON object."],
                customer_payload=customer_payload,
                category_payload=category_payload,
            )
        except Exception as exc:
            logger.error("failed to build Nova reply correction prompt (%s), giving up on LLM path", exc)
            return None, "quality"

        outcome = _invoke_bedrock(correction_prompt, system_prompt=REPLY_SYSTEM_PROMPT)
        raw = outcome.value
        attempt += 1

    logger.info("Nova reply path exhausted %d attempt(s) without an accepted draft, using deterministic fallback", LLM_MAX_ATTEMPTS)
    return None, "transient" if outcome.failure == "transient" else "quality"
