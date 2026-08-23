#!/usr/bin/env bash
# Runs the 10-step manual test walkthrough against the local server and
# logs every request + response into one file for review.
set -u
BASE="http://127.0.0.1:8000"
OUT="/home/exo/buildathon-2/vera-bot/test_run_results.txt"
CURL="curl -sS -m 10"

echo "VERA BOT TEST RUN -- $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$OUT"
echo "==========================================================" >> "$OUT"

step() {
  echo "" >> "$OUT"
  echo "--- STEP $1: $2 ---" >> "$OUT"
}

step 1 "healthz"
echo "REQUEST: GET /v1/healthz" >> "$OUT"
$CURL "$BASE/v1/healthz" >> "$OUT" 2>&1
echo "" >> "$OUT"

step 2 "metadata"
echo "REQUEST: GET /v1/metadata" >> "$OUT"
$CURL "$BASE/v1/metadata" >> "$OUT" 2>&1
echo "" >> "$OUT"

step 3 "push category context (dentists)"
echo 'REQUEST: POST /v1/context (scope=category, cat_dentists)' >> "$OUT"
$CURL -X POST "$BASE/v1/context" -H "Content-Type: application/json" -d '{
  "scope": "category", "context_id": "cat_dentists", "version": 1,
  "payload": {"name": "dentists", "tone": "clinical, reassuring", "banned_phrases": ["guaranteed cure"]}
}' >> "$OUT" 2>&1
echo "" >> "$OUT"

step 4 "push merchant context (Dr. Meera, has 299 offer)"
echo 'REQUEST: POST /v1/context (scope=merchant, m_001_drmeera)' >> "$OUT"
$CURL -X POST "$BASE/v1/context" -H "Content-Type: application/json" -d '{
  "scope": "merchant", "context_id": "m_001_drmeera", "version": 1,
  "payload": {
    "identity": {"name": "Dr. Meera", "category": "dentists", "languages": ["en"]},
    "performance": {"ctr": 0.021, "peer_ctr_median": 0.030},
    "offers": [{"name": "Dental Check Up", "price": 299}]
  }
}' >> "$OUT" 2>&1
echo "" >> "$OUT"

step 5 "push trigger context (research digest)"
echo 'REQUEST: POST /v1/context (scope=trigger, trg_research_digest_dentists)' >> "$OUT"
$CURL -X POST "$BASE/v1/context" -H "Content-Type: application/json" -d '{
  "scope": "trigger", "context_id": "trg_research_digest_dentists", "version": 1,
  "payload": {
    "kind": "research_digest", "merchant_id": "m_001_drmeera",
    "fact": "190 people in your area searched for '"'"'Dental Check Up'"'"' this week",
    "suppression_key": "research:dentists:2026-W17"
  }
}' >> "$OUT" 2>&1
echo "" >> "$OUT"

step 6 "tick -- should produce a composed message citing 190 and 299"
echo 'REQUEST: POST /v1/tick' >> "$OUT"
$CURL -X POST "$BASE/v1/tick" -H "Content-Type: application/json" -d '{
  "now": "2026-04-29T10:30:00Z", "available_triggers": ["trg_research_digest_dentists"]
}' >> "$OUT" 2>&1
echo "" >> "$OUT"

step 7 "tick again immediately -- suppression should return empty actions"
echo 'REQUEST: POST /v1/tick (repeat)' >> "$OUT"
$CURL -X POST "$BASE/v1/tick" -H "Content-Type: application/json" -d '{
  "now": "2026-04-29T10:35:00Z", "available_triggers": ["trg_research_digest_dentists"]
}' >> "$OUT" 2>&1
echo "" >> "$OUT"

step 8 "reply: merchant says yes send it -- expect intent transition, action=send"
echo 'REQUEST: POST /v1/reply (yes send it)' >> "$OUT"
$CURL -X POST "$BASE/v1/reply" -H "Content-Type: application/json" -d '{
  "conversation_id": "conv_001", "from_role": "merchant", "message": "yes send it", "turn_number": 2
}' >> "$OUT" 2>&1
echo "" >> "$OUT"

step 9 "reply: hard opt-out -- expect action=end + merchant suppression"
echo 'REQUEST: POST /v1/reply (please stop messaging me)' >> "$OUT"
$CURL -X POST "$BASE/v1/reply" -H "Content-Type: application/json" -d '{
  "conversation_id": "conv_002", "from_role": "merchant", "message": "please stop messaging me", "turn_number": 3, "merchant_id": "m_001_drmeera"
}' >> "$OUT" 2>&1
echo "" >> "$OUT"

step "9b" "tick after opt-out -- merchant should be fully suppressed now (empty actions)"
echo 'REQUEST: POST /v1/context (push a 2nd trigger for the now-suppressed merchant)' >> "$OUT"
$CURL -X POST "$BASE/v1/context" -H "Content-Type: application/json" -d '{
  "scope": "trigger", "context_id": "trg_perfdip_drmeera", "version": 1,
  "payload": {"kind": "perf_dip", "merchant_id": "m_001_drmeera", "fact": "CTR dipped this week"}
}' >> "$OUT" 2>&1
echo "" >> "$OUT"
echo 'REQUEST: POST /v1/tick (should be empty -- merchant suppressed after opt-out)' >> "$OUT"
$CURL -X POST "$BASE/v1/tick" -H "Content-Type: application/json" -d '{
  "now": "2026-04-29T10:40:00Z", "available_triggers": ["trg_perfdip_drmeera"]
}' >> "$OUT" 2>&1
echo "" >> "$OUT"

step 10 "unseen scenario: new gym merchant, Hinglish, perf_dip trigger"
echo 'REQUEST: POST /v1/context (scope=merchant, m_gym_test)' >> "$OUT"
$CURL -X POST "$BASE/v1/context" -H "Content-Type: application/json" -d '{
  "scope": "merchant", "context_id": "m_gym_test", "version": 1,
  "payload": {"identity": {"name": "PowerHouse Gym", "category": "gyms", "languages": ["hi","en"]}, "offers": [{"name": "Annual Membership", "price": 8999}]}
}' >> "$OUT" 2>&1
echo "" >> "$OUT"
echo 'REQUEST: POST /v1/context (scope=trigger, trg_gym_dip)' >> "$OUT"
$CURL -X POST "$BASE/v1/context" -H "Content-Type: application/json" -d '{
  "scope": "trigger", "context_id": "trg_gym_dip", "version": 1,
  "payload": {"kind": "perf_dip", "merchant_id": "m_gym_test", "fact": "footfall down 20% this week"}
}' >> "$OUT" 2>&1
echo "" >> "$OUT"
echo 'REQUEST: POST /v1/tick (final -- Hinglish message expected, citing Annual Membership 8999)' >> "$OUT"
$CURL -X POST "$BASE/v1/tick" -H "Content-Type: application/json" -d '{
  "now": "2026-04-29T10:30:00Z", "available_triggers": ["trg_gym_dip"]
}' >> "$OUT" 2>&1
echo "" >> "$OUT"

echo "" >> "$OUT"
echo "==========================================================" >> "$OUT"
echo "TEST RUN COMPLETE" >> "$OUT"
