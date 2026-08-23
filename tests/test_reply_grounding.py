"""Regression tests for the 5 fixes made after manual/live review:

1. /v1/reply must produce grounded, context-aware bodies (not one fixed
   string per intent) for intent_transition / off_topic / engaged.
2. Anti-repetition must not swallow a second, genuinely different reply
   in the same conversation just because it shares an intent bucket.
3. register_open_conversation/has_open_conversation must actually be wired
   so a merchant with an open thread isn't offered the same trigger again.
4. Off-topic detection must not false-positive on normal engaged replies.
5. VERA_ENV=production must refuse to boot on the in-memory backend.
"""
from __future__ import annotations

import importlib
import os
import sys

from fastapi.testclient import TestClient


def _fresh_client():
    """Reimport main with clean module state so context/suppression stores
    start empty for each test in this file (they're process-global)."""
    for mod in ("main", "composer", "conversation", "suppression", "context_store", "state_backend"):
        sys.modules.pop(mod, None)
    import main as main_module
    return TestClient(main_module.app), main_module


def test_intent_transition_reply_is_grounded_in_offer_not_generic():
    client, _ = _fresh_client()
    client.post("/v1/context", json={
        "scope": "merchant", "context_id": "m_grounding_1", "version": 1,
        "payload": {"identity": {"name": "Grounding Dental", "category": "dentists", "languages": ["en"]},
                     "offers": [{"name": "Root Canal Package", "price": 4500}]},
    })
    client.post("/v1/context", json={
        "scope": "trigger", "context_id": "trg_grounding_1", "version": 1,
        "payload": {"kind": "research_digest", "merchant_id": "m_grounding_1",
                     "fact": "80 people searched for 'Root Canal' this week"},
    })
    client.post("/v1/tick", json={"now": "2026-04-29T10:30:00Z", "available_triggers": ["trg_grounding_1"]})

    r = client.post("/v1/reply", json={
        "conversation_id": "conv_grounding_1", "from_role": "merchant",
        "message": "yes go ahead send it", "turn_number": 2, "merchant_id": "m_grounding_1",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "send"
    # Grounded reply should reference the real offer/price, not the old
    # fixed string "Great — I'll get that moving now and update you shortly."
    assert "4500" in body["body"] or "Root Canal" in body["body"]


def test_two_different_replies_same_conversation_both_get_sent():
    """Regression for the anti-repetition bug: two distinct merchant
    messages mapped to the same intent bucket must not collide just
    because the old implementation used one fixed string per intent."""
    client, _ = _fresh_client()
    client.post("/v1/context", json={
        "scope": "merchant", "context_id": "m_antirep_2", "version": 1,
        "payload": {"identity": {"name": "AntiRep Biz", "category": "restaurants", "languages": ["en"]},
                     "offers": [{"name": "Weekend Thali", "price": 349}]},
    })
    client.post("/v1/context", json={
        "scope": "trigger", "context_id": "trg_antirep_2", "version": 1,
        "payload": {"kind": "festival_upcoming", "merchant_id": "m_antirep_2", "fact": "Diwali is in 4 days"},
    })
    client.post("/v1/tick", json={"now": "2026-04-29T10:30:00Z", "available_triggers": ["trg_antirep_2"]})

    r1 = client.post("/v1/reply", json={
        "conversation_id": "conv_antirep_2", "from_role": "merchant",
        "message": "ok go ahead", "turn_number": 2, "merchant_id": "m_antirep_2",
    })
    r2 = client.post("/v1/reply", json={
        "conversation_id": "conv_antirep_2", "from_role": "merchant",
        "message": "sure send it now please", "turn_number": 4, "merchant_id": "m_antirep_2",
    })
    assert r1.json()["action"] == "send"
    # Second, genuinely different reply must not be silently dropped to
    # "wait" just because the first reply's intent bucket matched.
    assert r2.json()["action"] == "send", f"second reply wrongly suppressed: {r2.json()}"


def test_open_conversation_dedup_blocks_repeat_tick_for_same_merchant_trigger():
    client, _ = _fresh_client()
    client.post("/v1/context", json={
        "scope": "merchant", "context_id": "m_dedup_1", "version": 1,
        "payload": {"identity": {"name": "Dedup Biz", "category": "gyms"}, "offers": []},
    })
    client.post("/v1/context", json={
        "scope": "trigger", "context_id": "trg_dedup_1", "version": 1,
        "payload": {"kind": "perf_dip", "merchant_id": "m_dedup_1", "fact": "footfall dropped"},
    })
    r1 = client.post("/v1/tick", json={"now": "2026-04-29T10:30:00Z", "available_triggers": ["trg_dedup_1"]})
    assert len(r1.json()["actions"]) == 1

    # Same trigger offered again in a later tick, before any reply -- the
    # dedup guard (now actually wired via register_open_conversation) must
    # prevent a duplicate action for the same open (merchant, trigger) pair.
    r2 = client.post("/v1/tick", json={"now": "2026-04-29T10:35:00Z", "available_triggers": ["trg_dedup_1"]})
    assert r2.json()["actions"] == [], "open-conversation dedup did not block a repeat tick"


def test_off_topic_denylist_does_not_false_positive_on_normal_reply():
    client, _ = _fresh_client()
    r = client.post("/v1/reply", json={
        "conversation_id": "conv_offtopic_check", "from_role": "merchant",
        "message": "Can you tell me more about this before I decide?", "turn_number": 2,
    })
    assert r.status_code == 200
    # This should be classified as an engaged/on-topic question, not
    # off_topic -- verified via the rationale text since intent isn't
    # returned directly on the wire, but the old off_topic redirect string
    # ("that's outside what I can help with here") must not appear.
    assert "outside what i can help with" not in r.json()["body"].lower()


def test_off_topic_denylist_still_catches_genuinely_unrelated_ask():
    client, _ = _fresh_client()
    r = client.post("/v1/reply", json={
        "conversation_id": "conv_offtopic_real", "from_role": "merchant",
        "message": "Can you help me with a loan for my shop instead?", "turn_number": 2,
    })
    assert r.status_code == 200
    assert r.json()["action"] == "send"
    # Should decline/redirect rather than actually engage with loan terms
    # (rates, eligibility, amounts, application steps) -- mentioning the
    # word "loan" once while declining ("can't help with loan requests") is
    # fine and expected; actually answering the loan question is not.
    body_lower = r.json()["body"].lower()
    assert not any(term in body_lower for term in ("interest rate", "emi of", "eligib", "apply for a loan at")), (
        f"reply appears to engage with the loan question instead of declining: {body_lower}"
    )


def test_production_mode_refuses_to_boot_without_redis(monkeypatch):
    for mod in ("main", "composer", "conversation", "suppression", "context_store", "state_backend"):
        sys.modules.pop(mod, None)
    monkeypatch.setenv("VERA_ENV", "production")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")  # unreachable port
    try:
        raised = False
        try:
            importlib.import_module("main")
        except RuntimeError:
            raised = True
        assert raised, "expected RuntimeError when VERA_ENV=production without reachable Redis"
    finally:
        monkeypatch.delenv("VERA_ENV", raising=False)
        for mod in ("main", "composer", "conversation", "suppression", "context_store", "state_backend"):
            sys.modules.pop(mod, None)
