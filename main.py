"""FastAPI app: route wiring + request validation for the Vera bot.

Implements exactly the 5 endpoints required by the challenge contract:
GET /v1/healthz, GET /v1/metadata, POST /v1/context, POST /v1/tick,
POST /v1/reply.
"""
from __future__ import annotations

import asyncio
import logging
import os as _os
import time
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

load_dotenv()

from composer import compose, compose_reply
from context_store import ContextStore
from conversation import ConversationManager
from fallback import compose_reply_fallback
from suppression import SuppressionStore
from trigger_facts import resolve_fact
from trigger_router import get_strategy, rank_candidates
from validators import (
    ValidationError,
    validate_context_request,
    validate_reply_request,
    validate_tick_request,
)

# Intents where a fresh LLM-grounded reply materially improves quality
# (the merchant asked/said something specific that deserves a specific
# answer). Terminal/low-content intents (opt_out, turn_cap, auto_reply_*)
# stay on the static template library in fallback.py -- there's no thread
# fact worth grounding a goodbye or an auto-reply nudge in, and keeping
# them fully deterministic/static is the safer choice for compliance-
# adjacent closing messages.
_LLM_GROUNDED_REPLY_INTENTS = {"intent_transition", "off_topic", "engaged"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("vera_bot.main")

APP_VERSION = "1.3.0"
TEAM_NAME = "Airo"
TEAM_MEMBERS = ["Kapil Meena"]
MODEL_NAME = "amazon.nova-pro-v1:0 (Bedrock) with deterministic template fallback"
APPROACH = (
    "trigger-router (24 dataset trigger kinds mapped to explicit levers/CTAs) + "
    "fact-verified composer (numeric-claim, URL, and category-vs-merchant-offer "
    "verification) + conversation state machine (auto-reply/opt-out/intent-transition) "
    "+ per-trigger and per-merchant suppression"
)
CONTACT_EMAIL = _os.environ.get("TEAM_CONTACT_EMAIL", "kapilmeena30062003@gmail.com")
SUBMITTED_AT = _os.environ.get("SUBMITTED_AT", "2026-08-30T00:00:00Z")

_start_time = time.monotonic()

store = ContextStore()
conv_manager = ConversationManager()
suppression = SuppressionStore()

app = FastAPI(title="Vera Bot")

MAX_ACTIONS_PER_TICK = 20

# Section 5 of the testing brief: 30s is the judge's own timeout on
# /v1/tick. §3.3 of the test report documented a real data-loss bug: a
# 20-25-candidate batch processed compose() sequentially and got SIGKILLed
# by Gunicorn's --timeout 30 mid-batch, with suppression writes already
# committed for messages whose response never made it back. Fix has two
# parts: (1) run compose() calls concurrently (bounded) instead of one at
# a time, and (2) stop accepting new candidates once TICK_DEADLINE_SECONDS
# of wall-clock time has elapsed and return whatever's ready, well before
# the judge's own 30s budget or Gunicorn's worker timeout would fire.
TICK_DEADLINE_SECONDS = 22.0
TICK_COMPOSE_CONCURRENCY = 8

# /v1/reply's "wait" action should carry `wait_seconds` per the official
# examples (challenge-testing-brief.md 2.3, api-call-examples.md 2.5/4.1) --
# main.py's response builder was previously omitting this field entirely.
# Magnitudes follow the Phase-4 replay scenario (the one actually scored
# for top-10 bots): 2nd auto-reply occurrence backs off 24h; any other
# generic/no-signal wait backs off 30 min so the judge's tick loop can
# reasonably re-poll within the 60-minute test window.
_WAIT_SECONDS_BY_INTENT = {
    "auto_reply_repeat": 86400,
    "generic": 1800,
}
DEFAULT_WAIT_SECONDS = 1800

# Section 6.1: "in-process memory across workers will silently corrupt
# state and is the single most common failure point in naive submissions."
# The systemd unit in the build spec runs `-w 2` (2 Gunicorn workers), each
# a separate process with its own in-memory fallback dict if Redis isn't
# reachable -- suppression/conversation/context state would silently
# diverge between workers with no error surfaced anywhere. Refuse to boot
# on the in-memory backend when VERA_ENV=production, so a misconfigured
# deploy fails loudly at startup instead of corrupting state under load.
if _os.environ.get("VERA_ENV") == "production" and store.backend_name != "redis":
    raise RuntimeError(
        "VERA_ENV=production but Redis is not reachable (falling back to "
        "in-memory backend). Refusing to start: running multiple Gunicorn "
        "workers on the in-memory fallback silently corrupts suppression, "
        "conversation, and context state. Set REDIS_URL to a reachable "
        "Redis instance, or unset VERA_ENV for local single-process dev."
    )


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.message})


@app.get("/v1/healthz")
async def healthz():
    healthy = store.is_healthy()
    counts = store.counts()
    return {
        "status": "ok" if healthy else "degraded",
        "uptime_seconds": int(time.monotonic() - _start_time),
        "contexts_loaded": counts,
        "backend": store.backend_name,
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": TEAM_NAME,
        "team_members": TEAM_MEMBERS,
        "model": MODEL_NAME,
        "approach": APPROACH,
        "contact_email": CONTACT_EMAIL,
        "version": APP_VERSION,
        "submitted_at": SUBMITTED_AT,
    }


@app.post("/v1/context")
async def post_context(request: Request):
    raw_body = await request.body()
    if len(raw_body) > 500 * 1024:
        return JSONResponse(status_code=413, content={"error": "payload exceeds 500KB cap"})
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})

    try:
        validate_context_request(body)
    except ValidationError as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

    status, record = store.push_context(
        scope=body["scope"],
        context_id=body["context_id"],
        version=body["version"],
        payload=body["payload"],
        delivered_at=body.get("delivered_at"),
    )
    logger.info(
        "context push scope=%s id=%s version=%s status=%s",
        body["scope"], body["context_id"], body["version"], status,
    )
    if status == "stale":
        # Official contract (challenge-testing-brief.md 2.1 / api-call-
        # examples.md 1.5): a lower version than what's already stored
        # must be rejected with 409, not silently accepted.
        return JSONResponse(
            status_code=409,
            content={"accepted": False, "reason": "stale_version", "current_version": record.version},
        )
    # "written" (new/higher version) and "same_version" (idempotent
    # no-op re-post) both return 200 accepted:true per spec -- the ack_id
    # for a same_version repost is the ack_id from the original write.
    return {"accepted": True, "ack_id": record.ack_id, "stored_at": record.stored_at}


def _merchant_category(merchant_payload: dict | None) -> str | None:
    """The real official dataset puts the category slug at the merchant's
    top level as `category_slug` (see dataset/merchants_seed.json). Some
    hand-written test payloads (and the earlier paraphrased spec this bot
    was first built against) instead nest it as `identity.category`.
    Check both so category-context lookup doesn't silently miss on the
    real dataset."""
    if not isinstance(merchant_payload, dict):
        return None
    return merchant_payload.get("category_slug") or merchant_payload.get("identity", {}).get("category")


def _category_lookup(slug: str | None):
    """Category contexts are pushed with context_id == the bare slug
    (e.g. "dentists") per challenge-testing-brief.md 3.1 and
    api-call-examples.md 1.3 -- NOT a "cat_"-prefixed id. Try the bare
    slug first (the spec-correct form); fall back to the cat_-prefixed
    form for backward compatibility with any earlier test data pushed
    that way."""
    if not slug:
        return None
    rec = store.get("category", slug)
    if rec is None:
        rec = store.get("category", f"cat_{slug}")
    return rec


def _select_candidate_triggers(available_triggers: list[str]) -> list[dict]:
    """Look up each offered trigger_id against stored trigger context,
    match to eligible merchants, and build ranked candidate actions.

    Real TriggerContext envelopes (dataset/triggers_seed.json shape) carry
    an OUTER envelope {id, scope, kind, source, merchant_id, customer_id,
    payload, urgency, suppression_key, expires_at} where `payload` is the
    kind-specific INNER data (e.g. {"metric": "calls", "delta_pct": -0.5}
    for perf_dip). Both the envelope's own top-level `merchant_id`/
    `customer_id`/`suppression_key` AND the inner `payload` dict are kept
    on the candidate so downstream composition has everything it needs."""
    candidates = []
    for trigger_id in available_triggers:
        trigger_ctx = store.get("trigger", trigger_id)
        if trigger_ctx is None:
            logger.info("tick: trigger_id %s not found in stored context, skipping", trigger_id)
            continue
        envelope = trigger_ctx.payload
        inner_payload = envelope.get("payload", {}) if isinstance(envelope.get("payload"), dict) else {}
        merchant_id = envelope.get("merchant_id")
        customer_id = envelope.get("customer_id")
        trigger_kind = envelope.get("kind", "unknown")
        trigger_scope = envelope.get("scope", "merchant")

        if merchant_id is None:
            # digest-style triggers may target a category rather than one
            # merchant -- fan out to all merchants in that category. The
            # category slug for this fan-out case lives in the inner
            # payload (see dataset research_digest triggers:
            # payload={"category": "dentists", ...}), not the envelope.
            category = inner_payload.get("category")
            merchant_records = [
                m for m in store.all("merchant")
                if _merchant_category(m.payload) == category
            ] if category else []
        else:
            rec = store.get("merchant", merchant_id)
            merchant_records = [rec] if rec else []

        customer_rec = store.get("customer", customer_id) if customer_id else None
        customer_payload = customer_rec.payload if customer_rec else None
        customer_scoped = trigger_scope == "customer" or customer_id is not None
        if customer_scoped and (not customer_id or customer_payload is None):
            logger.info(
                "tick: deferring customer-scoped trigger %s until CustomerContext %s is available",
                trigger_id, customer_id or "<missing customer_id>",
            )
            continue
        if customer_payload is not None and merchant_id is not None:
            customer_merchant_id = customer_payload.get("merchant_id")
            if customer_merchant_id and customer_merchant_id != merchant_id:
                logger.warning(
                    "tick: deferring trigger %s because customer %s belongs to merchant %s, not %s",
                    trigger_id, customer_id, customer_merchant_id, merchant_id,
                )
                continue

        for merchant_rec in merchant_records:
            mid = merchant_rec.context_id
            suppression_key = envelope.get("suppression_key", f"{trigger_kind}:{trigger_id}")
            if suppression.is_merchant_suppressed(mid):
                continue
            if suppression.is_trigger_suppressed(mid, suppression_key):
                continue
            if suppression.has_open_conversation(mid, trigger_id):
                continue
            candidates.append(
                {
                    "merchant_id": mid,
                    "merchant_payload": merchant_rec.payload,
                    "customer_id": customer_id,
                    "customer_payload": customer_payload,
                    "trigger_id": trigger_id,
                    "trigger_kind": trigger_kind,
                    "trigger_scope": trigger_scope,
                    "trigger_inner_payload": inner_payload,
                    "suppression_key": suppression_key,
                    # rank_candidates' base_priority comes from the static
                    # per-kind strategy table alone, so it can't tell a
                    # genuinely urgent instance of a normally-low-priority
                    # trigger kind apart from a routine one. The real
                    # TriggerContext envelope carries its own urgency
                    # (1-5, per challenge-brief.md 4.3); feed it in as an
                    # additive value_score (already supported by
                    # rank_candidates but previously always defaulted to
                    # 0) so a high-urgency instance can outrank a
                    # higher-table-priority but routine one, without
                    # replacing the table-driven ordering entirely.
                    "value_score": _safe_urgency_score(envelope.get("urgency")),
                }
            )
    return rank_candidates(candidates)


def _safe_urgency_score(urgency) -> int:
    """Maps the trigger envelope's urgency (1-5 per spec) to a small
    additive nudge for ranking. Never raises on missing/malformed input --
    unrecognized values contribute 0, same as before this field existed."""
    try:
        u = int(urgency)
    except (TypeError, ValueError):
        return 0
    return max(0, min(5, u)) * 2


def _compose_one_candidate(cand: dict) -> dict:
    """Runs compose() for a single tick candidate (sync, CPU/IO work
    offloaded to a thread by the caller) and returns a fully-shaped
    action dict per the official /v1/tick contract (challenge-testing-
    brief.md 2.2): conversation_id, merchant_id, customer_id, send_as,
    trigger_id, template_name, template_params, body, cta,
    suppression_key, rationale. Does NOT perform any suppression/state
    side effects -- those are applied by the caller only after the full
    action list has been assembled, so a mid-batch failure/timeout can
    never leave "marked as sent but never delivered" orphaned state
    (test report §3.3)."""
    merchant_category = _merchant_category(cand["merchant_payload"])
    cat_rec = _category_lookup(merchant_category)
    category_ctx = cat_rec.payload if cat_rec else None

    fact = resolve_fact(
        cand["trigger_kind"],
        cand["trigger_inner_payload"],
        category_payload=category_ctx,
        merchant_payload=cand["merchant_payload"],
        customer_payload=cand.get("customer_payload"),
    )
    trigger = {
        "kind": cand["trigger_kind"],
        "fact": fact,
        "trigger_id": cand["trigger_id"],
        "payload": cand["trigger_inner_payload"],
    }

    is_customer_facing = cand.get("customer_payload") is not None and (
        cand.get("trigger_scope") == "customer" or cand.get("customer_id") is not None
    )
    result = compose(
        category=merchant_category or "unknown",
        merchant_payload=cand["merchant_payload"],
        trigger=trigger,
        customer_payload=cand.get("customer_payload"),
        category_payload=category_ctx,
    )
    strategy = get_strategy(cand["trigger_kind"])
    logger.info(
        "tick decision: merchant=%s customer=%s trigger=%s kind=%s lever=%s send_as=%s",
        cand["merchant_id"], cand.get("customer_id"), cand["trigger_id"], cand["trigger_kind"],
        strategy.lever, "merchant_on_behalf" if is_customer_facing else "vera",
    )
    conversation_id = f"conv_{cand['merchant_id']}_{cand['trigger_id']}"
    template_name = f"vera_{cand['trigger_kind']}_v1" if not is_customer_facing else f"merchant_{cand['trigger_kind']}_v1"
    return {
        "conversation_id": conversation_id,
        "merchant_id": cand["merchant_id"],
        "customer_id": cand.get("customer_id"),
        "send_as": "merchant_on_behalf" if is_customer_facing else "vera",
        "trigger_id": cand["trigger_id"],
        "template_name": template_name,
        "template_params": [cand["merchant_id"], fact or "", result["cta"]],
        "body": result["body"],
        "cta": result["cta"],
        "suppression_key": cand["suppression_key"],
        "rationale": result.get("rationale", ""),
        # internal-only, stripped before returning to the judge -- kept
        # here so the caller can apply suppression side effects without
        # re-deriving merchant_id/trigger_id/suppression_key from the dict.
        "_merchant_id": cand["merchant_id"],
        "_trigger_id": cand["trigger_id"],
        "_suppression_key": cand["suppression_key"],
    }


@app.post("/v1/tick")
async def post_tick(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})

    try:
        validate_tick_request(body)
    except ValidationError as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

    tick_started = time.monotonic()
    candidates = _select_candidate_triggers(body["available_triggers"])[:MAX_ACTIONS_PER_TICK]

    # Run compose() for all candidates concurrently (bounded) instead of
    # sequentially -- each compose() call may make a blocking Bedrock
    # round trip (~1-2s), so 20 sequential calls reliably blows the 30s
    # judge timeout (test report §3.3: 502 after 28.4s, worker killed
    # mid-batch). Bounded via a semaphore so we don't fire 20 simultaneous
    # Bedrock requests either. Each candidate also races against a
    # deadline check so, once TICK_DEADLINE_SECONDS has elapsed, we stop
    # waiting on further candidates and return whatever's ready -- never
    # try to "catch up" past the judge's own budget.
    semaphore = asyncio.Semaphore(TICK_COMPOSE_CONCURRENCY)

    async def _compose_with_limit(cand: dict) -> dict | None:
        remaining = TICK_DEADLINE_SECONDS - (time.monotonic() - tick_started)
        if remaining <= 0:
            return None
        async with semaphore:
            remaining = TICK_DEADLINE_SECONDS - (time.monotonic() - tick_started)
            if remaining <= 0:
                return None
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(_compose_one_candidate, cand), timeout=max(remaining, 0.1)
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "tick: compose() for merchant=%s trigger=%s exceeded remaining tick budget, dropping "
                    "from this response (will be offered again next tick, not marked suppressed)",
                    cand["merchant_id"], cand["trigger_id"],
                )
                return None
            except Exception as exc:
                logger.error(
                    "tick: compose() raised for merchant=%s trigger=%s (%s), dropping from this response",
                    cand["merchant_id"], cand["trigger_id"], exc,
                )
                return None

    raw_results = await asyncio.gather(*(_compose_with_limit(c) for c in candidates))
    composed_actions = [r for r in raw_results if r is not None]

    # Suppression/open-conversation side effects are applied ONLY after
    # every compose() call has returned and the full action list is
    # assembled -- this is the fix for test report §3.3's data-loss bug,
    # where per-candidate suppression writes happened inside the same
    # loop iteration that built the response, so a mid-batch kill left
    # triggers marked "sent" whose bodies were never actually returned.
    # Now: if we never get here (process killed, deadline blown), NOTHING
    # has been marked suppressed, so every dropped candidate is correctly
    # still eligible on the next tick.
    actions = []
    for action in composed_actions:
        suppression.suppress_trigger(action["_merchant_id"], action["_suppression_key"])
        suppression.register_open_conversation(
            action["_merchant_id"], action["_trigger_id"], conversation_id=action["conversation_id"],
            customer_id=action.get("customer_id"),
        )
        actions.append({k: v for k, v in action.items() if not k.startswith("_")})

    return {"actions": actions}


def _recover_thread_context(
    conversation_id: str, merchant_id: str | None, customer_id: str | None = None
) -> tuple[str | None, dict | None, dict | None, dict | None, dict | None]:
    """Best-effort recovery of (merchant_id, merchant_payload,
    trigger_payload, category_payload, customer_payload) for a reply
    thread. The published /v1/reply contract doesn't guarantee
    merchant_id/trigger_id/customer_id are echoed back on every reply, so
    we fall back to whatever the conversation state machine already
    remembered from earlier turns in this same conversation_id, and to
    the open-conversation dedup index (registered in /v1/tick) to recover
    which trigger (and, for customer-facing threads, which customer) this
    thread started from."""
    mid = merchant_id or conv_manager.get_remembered_merchant_id(conversation_id)
    trigger_payload = None
    category_payload = None
    merchant_payload = None
    customer_payload = None

    if mid:
        merchant_rec = store.get("merchant", mid)
        merchant_payload = merchant_rec.payload if merchant_rec else None

        trigger_id = suppression.get_open_trigger_for_merchant(mid)
        if trigger_id:
            trigger_rec = store.get("trigger", trigger_id)
            trigger_payload = trigger_rec.payload if trigger_rec else None

        cid = customer_id or suppression.get_open_customer_for_merchant(mid)
        if cid:
            customer_rec = store.get("customer", cid)
            customer_payload = customer_rec.payload if customer_rec else None

        if merchant_payload:
            merchant_category = _merchant_category(merchant_payload)
            if merchant_category:
                cat_rec = _category_lookup(merchant_category)
                category_payload = cat_rec.payload if cat_rec else None

    return mid, merchant_payload, trigger_payload, category_payload, customer_payload


@app.post("/v1/reply")
async def post_reply(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})

    try:
        validate_reply_request(body)
    except ValidationError as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

    conversation_id = body["conversation_id"]
    incoming_message = body["message"]

    result = conv_manager.handle_reply(
        conversation_id=conversation_id,
        from_role=body["from_role"],
        message=incoming_message,
        turn_number=body["turn_number"],
        merchant_id=body.get("merchant_id"),
    )
    intent = result["intent"]
    action = result["action"]

    mid, merchant_payload, trigger_payload, category_payload, customer_payload = _recover_thread_context(
        conversation_id, body.get("merchant_id"), body.get("customer_id")
    )
    merchant_category = _merchant_category(merchant_payload) if merchant_payload else None

    if action == "send" and intent in _LLM_GROUNDED_REPLY_INTENTS:
        # Grounded, context-aware reply: acknowledges what they actually
        # said and answers/redirects using real thread facts, rather than
        # a static per-intent string (section 4's replay tests explicitly
        # cover engaged/off-topic/intent-transition replies with varied
        # phrasing, which a fixed template can't credibly answer).
        composed = compose_reply(
            intent=intent,
            incoming_message=incoming_message,
            merchant_payload=merchant_payload,
            trigger_payload=trigger_payload,
            customer_payload=customer_payload,
            category=merchant_category,
            category_payload=category_payload,
        )
        body_text = composed["body"]
        rationale = composed["rationale"]
    else:
        # Terminal/low-content moves (opt_out, auto_reply_*, turn_cap) and
        # the plain "wait" case stay on the static template library --
        # deterministic, compliance-safe closing lines with no thread fact
        # worth grounding them in.
        fb = compose_reply_fallback(intent)
        body_text = fb["body"]
        rationale = fb["rationale"]

    if action == "send" and body_text and conv_manager.has_been_sent(conversation_id, body_text):
        # anti-repetition guard: never send an exact/near-duplicate again.
        # Now that replies are grounded/dynamic per-message rather than a
        # fixed string per intent, this only fires on genuine repeats, not
        # on every second reply that happens to share an intent bucket.
        action = "wait"
        rationale = "anti-repetition guard: identical message already sent, waiting instead"
        body_text = ""
    elif action == "send" and body_text:
        conv_manager.record_sent(conversation_id, body_text)

    if action in ("end",) and mid:
        trigger_id = suppression.get_open_trigger_for_merchant(mid)
        if trigger_id:
            suppression.close_open_conversation(mid, trigger_id)

    logger.info("reply decision: conv=%s intent=%s action=%s", conversation_id, intent, action)

    response = {"action": action, "rationale": rationale}
    if action != "wait" or body_text:
        response["body"] = body_text
    if action == "wait":
        # Official contract (challenge-testing-brief.md 2.3;
        # api-call-examples.md 2.5/4.1) always carries wait_seconds on a
        # "wait" action -- previously omitted entirely.
        response["wait_seconds"] = _WAIT_SECONDS_BY_INTENT.get(intent, DEFAULT_WAIT_SECONDS)
    return response
