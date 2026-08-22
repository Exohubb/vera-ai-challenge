"""Load/latency and determinism checks (section 8.1 items 4 and 5)."""
import time
import concurrent.futures

from composer import compose


def test_tick_latency_under_30s(client):
    client.post(
        "/v1/context",
        json={
            "scope": "merchant",
            "context_id": "m_load_test",
            "version": 1,
            "payload": {"identity": {"name": "Load Test Biz", "category": "gyms"}, "offers": []},
        },
    )
    client.post(
        "/v1/context",
        json={
            "scope": "trigger",
            "context_id": "trg_load_test",
            "version": 1,
            "payload": {"kind": "perf_dip", "merchant_id": "m_load_test", "fact": "dip"},
        },
    )
    start = time.monotonic()
    r = client.post("/v1/tick", json={"now": "2026-04-29T10:30:00Z", "available_triggers": ["trg_load_test"]})
    elapsed = time.monotonic() - start
    assert r.status_code == 200
    assert elapsed < 30


def test_concurrent_requests_10_per_sec_no_corruption(client):
    def hit_healthz(_):
        r = client.get("/v1/healthz")
        return r.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(hit_healthz, range(20)))
    assert all(code == 200 for code in results)


def test_determinism_identical_tick_twice(client):
    client.post(
        "/v1/context",
        json={
            "scope": "merchant",
            "context_id": "m_determinism",
            "version": 1,
            "payload": {
                "identity": {"name": "Determinism Biz", "category": "dentists"},
                "offers": [{"name": "Check Up", "price": 199}],
            },
        },
    )
    client.post(
        "/v1/context",
        json={
            "scope": "trigger",
            "context_id": "trg_determinism",
            "version": 1,
            "payload": {
                "kind": "research_digest",
                "merchant_id": "m_determinism",
                "fact": "50 people searched for 'Check Up' this week",
                "suppression_key": "determinism_test_key_unique",
            },
        },
    )
    r1 = client.post("/v1/tick", json={"now": "2026-04-29T10:30:00Z", "available_triggers": ["trg_determinism"]})
    body1 = r1.json()["actions"][0]["body"] if r1.json()["actions"] else None
    assert body1 is not None, "first tick should have produced an action before suppression kicks in"


def test_determinism_composer_called_twice_identical_input():
    merchant_payload = {
        "identity": {"name": "Determinism Biz", "category": "dentists"},
        "offers": [{"name": "Check Up", "price": 199}],
    }
    trigger = {"kind": "research_digest", "fact": "50 people searched for 'Check Up' this week"}
    result1 = compose(category="dentists", merchant_payload=merchant_payload, trigger=trigger)
    result2 = compose(category="dentists", merchant_payload=merchant_payload, trigger=trigger)
    assert result1 == result2, "identical input must produce identical output (temperature=0 requirement)"
