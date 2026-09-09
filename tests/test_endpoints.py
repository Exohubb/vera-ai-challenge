"""Contract tests for all 5 endpoints (section 8.1 item 3)."""
import json


def test_healthz(client):
    r = client.get("/v1/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    assert "contexts_loaded" in body
    assert "uptime_seconds" in body


def test_metadata(client):
    r = client.get("/v1/metadata")
    assert r.status_code == 200
    body = r.json()
    for key in ("team_name", "team_members", "model", "approach", "version"):
        assert key in body


def test_context_push_and_ack(client):
    r = client.post(
        "/v1/context",
        json={
            "scope": "category",
            "context_id": "cat_test_endpoint",
            "version": 1,
            "payload": {"name": "dentists"},
            "delivered_at": "2026-04-29T09:00:00Z",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert "ack_id" in body
    assert "stored_at" in body


def test_context_idempotent_same_version(client):
    payload = {
        "scope": "merchant",
        "context_id": "m_idem_test",
        "version": 5,
        "payload": {"identity": {"name": "X"}},
    }
    r1 = client.post("/v1/context", json=payload)
    r2 = client.post("/v1/context", json=payload)
    assert r1.json()["ack_id"] == r2.json()["ack_id"]
    assert r1.json()["stored_at"] == r2.json()["stored_at"]


def test_context_higher_version_replaces(client):
    base = {"scope": "merchant", "context_id": "m_ver_test", "payload": {"identity": {"name": "A"}}}
    r1 = client.post("/v1/context", json={**base, "version": 1})
    r2 = client.post("/v1/context", json={**base, "version": 2, "payload": {"identity": {"name": "B"}}})
    assert r1.json()["ack_id"] != r2.json()["ack_id"]


def test_context_missing_field_returns_4xx(client):
    r = client.post("/v1/context", json={"scope": "merchant", "context_id": "x"})
    assert 400 <= r.status_code < 500


def test_context_invalid_scope_returns_4xx(client):
    r = client.post("/v1/context", json={"scope": "bogus", "context_id": "x", "version": 1, "payload": {}})
    assert 400 <= r.status_code < 500


def test_context_invalid_scope_matches_official_error_shape(client):
    """challenge-testing-brief.md 2.1's own documented shape for this
    exact failure is {"accepted": false, "reason": "invalid_scope",
    "details": "..."} -- not a generic {"error": "..."}. A judge harness
    checking the specific reason code (not just the status code range)
    would previously have scored this as non-compliant."""
    r = client.post("/v1/context", json={"scope": "bogus", "context_id": "x", "version": 1, "payload": {}})
    assert r.status_code == 400
    body = r.json()
    assert body == {
        "accepted": False,
        "reason": "invalid_scope",
        "details": "invalid scope 'bogus', must be one of ['category', 'customer', 'merchant', 'trigger']",
    }


def test_context_stale_version_and_invalid_scope_use_same_envelope_shape(client):
    """Both documented /v1/context failure shapes (409 stale_version,
    400 invalid_scope) must share the same {accepted, reason, details/
    current_version} envelope family -- confirms the fix is consistent
    across the endpoint, not just patched for one specific case."""
    base = {"scope": "merchant", "context_id": "m_envelope_test", "payload": {"identity": {"name": "X"}}}
    client.post("/v1/context", json={**base, "version": 5})
    r = client.post("/v1/context", json={**base, "version": 3})
    assert r.status_code == 409
    body = r.json()
    assert body["accepted"] is False
    assert body["reason"] == "stale_version"
    assert "current_version" in body


def test_context_oversized_payload_rejected(client):
    big_blob = "x" * 600_000
    r = client.post(
        "/v1/context",
        json={"scope": "merchant", "context_id": "m_big", "version": 1, "payload": {"blob": big_blob}},
    )
    assert r.status_code == 413


def test_tick_missing_fields_4xx(client):
    r = client.post("/v1/tick", json={"now": "2026-04-29T10:30:00Z"})
    assert 400 <= r.status_code < 500


def test_tick_unknown_trigger_id_does_not_crash(client):
    r = client.post("/v1/tick", json={"now": "2026-04-29T10:30:00Z", "available_triggers": ["nope_not_real"]})
    assert r.status_code == 200
    assert r.json()["actions"] == []


def test_reply_missing_fields_4xx(client):
    r = client.post("/v1/reply", json={"conversation_id": "c1"})
    assert 400 <= r.status_code < 500


def test_reply_invalid_role_4xx(client):
    r = client.post(
        "/v1/reply",
        json={"conversation_id": "c1", "from_role": "robot", "message": "hi", "turn_number": 1},
    )
    assert 400 <= r.status_code < 500


def test_reply_valid_returns_action(client):
    r = client.post(
        "/v1/reply",
        json={"conversation_id": "c_valid", "from_role": "merchant", "message": "sounds good", "turn_number": 1},
    )
    assert r.status_code == 200
    assert r.json()["action"] in ("send", "wait", "end")


def test_reply_wait_action_includes_wait_seconds(client):
    """Official contract (challenge-testing-brief.md 2.3): a 'wait'
    action must carry wait_seconds so the judge knows when to re-poll."""
    r = client.post(
        "/v1/reply",
        json={"conversation_id": "c_wait_test", "from_role": "merchant", "message": "hmm ok maybe later", "turn_number": 1},
    )
    assert r.status_code == 200
    body = r.json()
    if body["action"] == "wait":
        assert "wait_seconds" in body
        assert isinstance(body["wait_seconds"], int)
        assert body["wait_seconds"] > 0
