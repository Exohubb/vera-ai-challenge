# Vera Bot — magicpin AI Challenge submission

## Approach

A stateful FastAPI service exposing the 5 required endpoints
(`/v1/healthz`, `/v1/metadata`, `/v1/context`, `/v1/tick`, `/v1/reply`).
The core design principle: the LLM does *style*, an explicit rule layer
does *decisions*. This is what the challenge brief calls out as the
difference between submissions that survive unseen-scenario injection and
ones that only pattern-match the 30 sample pairs.

```
main.py            route wiring, request validation, 4xx handling, async
                    parallel tick composition, customer-facing fan-out
context_store.py   versioned, idempotent (scope, context_id, version) storage,
                    CAS writes, 409 stale_version contract
trigger_router.py  trigger-kind -> lever/CTA lookup table + candidate ranking
                    (24 kinds covered -- every kind in the real dataset)
trigger_facts.py    resolves a concrete groundable "fact" string from a real
                    TriggerContext's kind-specific inner payload (delta_pct,
                    top_item_id digest lookups, competitor_name, etc.)
offers.py            normalizes real merchant offers ("Dental Cleaning @
                     ₹299") and category offer_catalog entries into
                     {name, price}; detects category-catalog-offer leakage
composer.py         LLM prompt + call + fact-verification post-check
fact_check.py        numeric-claim / URL / CTA-count extraction and
                      verification against source context
category_styles.py   per-category tone + banned-phrase config
fallback.py         deterministic template composer (24 trigger kinds x
                     merchant-facing + customer-facing variants), used
                     whenever the LLM path is unavailable or fails verification
conversation.py      /v1/reply state machine: auto-reply, opt-out, intent
                     transition, off-topic, engaged, anti-repetition,
                     turn-depth cap
suppression.py       per-trigger (7d) and per-merchant (30d) cooldowns,
                     conversation dedup per (merchant_id, trigger_id),
                     remembers customer_id per open conversation too
state_backend.py     shared Redis-or-in-memory KV backend used by the above
validators.py        payload size/shape checks -> clean 4xx, never a 500
```

## Fixes made after testing against the REAL official dataset

An earlier test pass (`VERA_BOT_TEST_REPORT.md` in the parent folder) ran the
deployed bot against the actual `magicpin-ai-challenge/dataset/` package
(not hand-written fixtures) and surfaced concrete gaps. All of the
following are now fixed and covered by regression tests in
`tests/test_real_dataset.py`:

1. **Customer-facing composition didn't exist.** `/v1/tick` never looked at
   a trigger's `customer_id`/`scope`, so 4 of the 10 official case studies
   (recall reminders, bridal followups, lapse winbacks) were structurally
   unreachable. Now: customer context is loaded, a dedicated customer-facing
   prompt/voice/template path is used, and `send_as` is correctly
   `merchant_on_behalf`.
2. **Tick response was missing 5 of 11 spec-required fields** (`conversation_id`,
   `customer_id`, `send_as`, `template_name`, `template_params`). Now present
   on every action.
3. **Large tick batches (20+ triggers) caused silent data loss.** The
   per-trigger `compose()` loop ran sequentially and committed suppression
   writes *before* the response was assembled, so a mid-batch Gunicorn
   timeout left triggers marked "sent" with no message ever delivered.
   Fixed with bounded-concurrency `asyncio.gather`, a hard deadline check
   (22s, under the judge's 30s budget), and suppression writes deferred
   until after the full action list is built.
4. **Context version conflicts always returned `200 accepted:true`**, even
   for a stale (lower-version) write that was silently rejected. Now
   returns the spec's `409 {"accepted": false, "reason": "stale_version",
   "current_version": N}`.
5. **Auto-reply detection missed the exact official test phrase**
   ("Thank you for contacting ... Our team will respond shortly.") used in
   both `challenge-testing-brief.md`'s own worked example and
   `judge_simulator.py`'s hardcoded test message. Added.
6. **The reply intent classifier had a blind spot for imperative
   rephrasings** ("tell me the offer details again please" vs "what's the
   offer?") -- fixed.
7. **`category_slug`/bare-slug lookup mismatch**: the real dataset puts the
   category at the merchant's top level (`category_slug`) and pushes
   category contexts under the bare slug (`"dentists"`, not `"cat_dentists"`)
   -- the original code checked `identity.category` and a `cat_`-prefixed
   id, so category context (peer stats, digest, voice) never loaded against
   real data. Fixed with a helper that checks both shapes.
8. **Offer parsing never matched the real dataset shape.** Real merchant/
   category offers are titles like `"Dental Cleaning @ ₹299"` with a
   `status` field; the original code filtered on flat `name`/`price` keys
   that never existed in real data, so the mandatory-offer-citation rule
   (the single highest-leverage decision-quality rule in the spec) silently
   never fired for any real merchant. New `offers.py` parses the real
   `"<name> @ ₹<price>"` pattern.
9. **Category-catalog offer leakage** (found during this pass): the model
   would sometimes cite a category-level `offer_catalog` template price
   (shared across every merchant in a vertical) as if it were a specific
   merchant's own live offer. Added a dedicated post-generation check
   (`offers.find_category_offer_leakage`) plus a hard persona rule
   distinguishing "your own offers" from "generic category reference."
10. **14 of 24 real trigger kinds had no strategy or fallback template**
    (`ipl_match_today` = case study 5, `supply_alert` = case study 9,
    `active_planning_intent` = case studies 3/6, plus `renewal_due`,
    `winback_eligible`, `seasonal_perf_dip`, `review_theme_emerged`,
    `category_seasonal`, `gbp_unverified`, `cde_opportunity`,
    `customer_lapsed_hard`, `trial_followup`, `wedding_package_followup`,
    `chronic_refill_due`) and silently fell to the weakest default lever
    regardless of real urgency. All 24 kinds now have a dedicated
    strategy + fallback template, verified against the full dataset.
11. **Merchant greeting used the business name, not the owner's first
    name.** Every gold-standard case study addresses the merchant as
    "Dr. Meera" / "Suresh" / "Karthik" / "Ramesh", never "Dr. Meera's
    Dental Clinic" -- the latter scores the same as a generic "Hi" on
    merchant fit. Fixed to prefer `identity.owner_first_name`.
12. **No URL-fabrication check.** `api-call-examples.md`'s own failure-mode
    table treats any URL in a message as a hard fail; a live test showed
    the model inventing a plausible-looking event registration link. Added
    a mechanical gate (`fact_check.find_urls`) plus a persona rule.
13. **`/v1/reply`'s `wait` action never carried `wait_seconds`**, which the
    official examples always include. Added.

## Model choice

Primary: Amazon Bedrock, Nova Pro (`amazon.nova-pro-v1:0`), invoked via
`boto3`'s `bedrock-runtime` client. In production this authenticates via
an EC2 instance IAM role (no long-lived credentials); for local dev it
also supports Bedrock API-key/bearer-token auth via the
`AWS_BEARER_TOKEN_BEDROCK` env var, which boto3/botocore picks up
automatically — no custom auth code needed either way.

The persona and decision rules given to the model live in
`vera_persona.py` (`SYSTEM_PROMPT` + `build_user_message`), separate from
`composer.py`'s orchestration logic. The system prompt encodes who Vera
is, the four-context framework, the zero-invented-facts rule, decision
discipline (pick one signal, don't dump every fact), Hinglish handling,
and the single-CTA constraint — but deliberately contains no hardcoded
example messages to imitate, since memorizing sample phrasing is exactly
the overfitting failure mode the challenge brief warns against. The
per-call user message injects only the actual context payloads plus the
already-decided lever/CTA type from `trigger_router.py` — the LLM's job is
wording, not re-deciding strategy.

**Determinism**: Nova's Invoke API has no seed parameter and rejects
`temperature=0` exactly (valid range is `0.00001–1`), so two live calls
with identical input can produce different but equally valid phrasing.
Since the challenge spec requires strict "same input → same output",
`composer.py` runs `temperature=0.00001, topK=1` to get the model as close
to deterministic as possible, and then memoizes every `compose()` result
by a hash of its full input (category, merchant, trigger, customer,
category context) — so repeated identical calls return a byte-identical
cached result rather than re-invoking the model. This was found and fixed
via a failing test during verification (see below).

**Decision-quality gate on offer citation**: verified live that Nova
sometimes chose an open-ended curiosity question over directly citing a
matching offer+price for `research_digest`/`perf_dip`/`recall_due`/
`milestone_reached` triggers — a valid-sounding message, but a
decision-quality miss per section 3.2 (offer citation shouldn't be left to
the model's per-call judgment when a matching offer exists). Fixed by
adding an explicit post-generation check: if the trigger kind requires
offer citation and a matching offer exists in the merchant's catalog, the
LLM output is rejected (routes to the deterministic fallback, which always
cites it correctly) unless it actually names the offer and price.

If Bedrock is unreachable, times out, or output fails verification
(invented numbers, banned phrase, more than one CTA, missed offer
citation), the composer falls back to `fallback.py`'s deterministic
templates, which only interpolate values pulled directly from the context
payload.

**Live-tested in this session** with a real Bedrock API key (Nova Pro,
`us-east-1`): confirmed working end-to-end, including a close reproduction
of the exact worked example from the challenge brief (Dr. Meera / 190
searches / ₹299 offer) and correct natural Hinglish output for a merchant
with `languages: ["hi", "en"]`. The API key used for this test was pasted
directly into chat, which counts as exposed regardless of where it ends
up stored — **rotate/delete it in the Bedrock API keys console now**; it's
been placed in `.env` (gitignored, never committed) purely so the smoke
test could run, not as a place to safely park a compromised key long-term.

## Post-review fixes (this session)

A manual code review + live testing against the running server surfaced 5
real defects not caught by the original 33-test suite (all of which
tested `/v1/reply`'s *action/intent* but never its *body* content). All
five are now fixed, covered by new regression tests in
`tests/test_reply_grounding.py`, and re-verified live:

1. **`/v1/reply` never called the LLM composer.** It only ever returned
   one of ~8 static canned strings keyed by intent, regardless of
   merchant, language, or actual message content. Added
   `composer.compose_reply()` (mirrors the tick composer's Nova-first,
   fact-verified, one-correction-retry, deterministic-fallback pipeline)
   and a matching `REPLY_SYSTEM_PROMPT` in `vera_persona.py`. Now wired
   into `main.py` for `intent_transition`, `off_topic`, and the new
   `engaged` intent. Terminal/low-content intents (`opt_out`,
   `auto_reply_*`, `turn_cap`) intentionally stay on static templates --
   there's no thread fact worth grounding a goodbye in, and deterministic
   closing lines are the safer choice for compliance-adjacent messages.
2. **Anti-repetition guard was silently dropping legitimate replies.**
   Because every reply in an intent bucket produced the same fixed string,
   two different real merchant messages mapped to `intent_transition`
   would fingerprint identically and the second one got swallowed to
   `wait`. Now that replies are grounded per-message, this only fires on
   genuine verbatim repeats.
3. **`register_open_conversation`/`close_open_conversation` were dead
   code.** Defined in `suppression.py` but never called anywhere, so the
   "at most one open conversation per (merchant, trigger)" guarantee from
   section 5 didn't actually exist -- `/v1/tick` checked
   `has_open_conversation` against a store nothing ever wrote to. Now
   registered when a tick action is sent and closed when a reply resolves
   to `end`. Added `get_open_trigger_for_merchant` so `/v1/reply` can
   recover which trigger a thread belongs to even though the published
   `/v1/reply` contract doesn't echo `trigger_id` back.
4. **Off-topic detector had a high false-positive rate.** The original
   allowlist-of-keywords approach classified any reply longer than 3
   words without one of ~12 hardcoded words as off-topic -- e.g. "Can you
   tell me more about this before I decide?" got redirected as
   off-topic. Replaced with a denylist of genuinely unrelated domains
   (loans, insurance, wrong number, etc.) plus a new `engaged` intent for
   normal on-topic questions, which now get a real grounded answer instead
   of a redirect or a silent `wait`.
5. **In-memory state silently corrupts under multi-worker deploys.** The
   systemd unit in the build spec runs 2 Gunicorn workers; each would get
   its own in-memory dict if Redis isn't reachable, with no error raised
   anywhere. `main.py` now checks `VERA_ENV=production` at startup and
   refuses to boot if the backend isn't Redis, so a misconfigured EC2
   deploy fails loudly instead of quietly corrupting suppression/
   conversation state under load.

Live-verified end-to-end against the running server: pushed the exact
Dr. Meera/190-searches/₹299 worked example, confirmed a repeat tick is
now correctly deduped to `[]`, then ran a 4-turn reply sequence
(`intent_transition` -> `engaged` follow-up question -> `off_topic` loan
question -> hard `opt_out`) and confirmed every reply was grounded in real
context, single-CTA, zero invented facts, and that opt-out correctly
suppressed a brand-new `milestone_reached` trigger on a subsequent tick.

## Tradeoffs / what I'd do with more time

- **Real dataset**: the official challenge zip (`magicpin-ai-challenge.zip`)
  was not reachable from this environment (404 on fetch), so `dataset/generate_dataset.py`
  and the 30 canonical test pairs / case studies were not available to
  test against directly. All logic was instead verified against the
  worked examples embedded in the two spec markdown files (section 8.2,
  section 11) and 6 hand-written unseen-scenario tests. Once the real
  dataset is available, it should be loaded via repeated `/v1/context`
  calls and every `/v1/tick` output spot-checked against
  `examples/case-studies.md`.
- **Bedrock wiring**: now confirmed working end-to-end with a real API key
  against Nova Pro in `us-east-1` (see `bedrock_smoke_test.py` and
  `tests/test_live_bedrock.py`). Still needs the EC2-instance-role path
  tested separately once deployed — bearer-token auth (used for this local
  test) and IAM-role auth are different code paths inside
  boto3/botocore, and only the former has actually been exercised here.
- **Redis**: no `redis-server` binary is present in this sandbox, so all
  testing ran on the in-memory fallback backend (single-process only,
  fixed a real bug where two independently-constructed store objects
  didn't share state — now fixed via a per-namespace shared dict). Before
  a multi-worker EC2 deploy (`-w 2` per the systemd unit in section 6.4),
  install Redis and confirm `REDIS_URL` points at it; the in-memory
  fallback must never be used across multiple Gunicorn workers.
- **judge_simulator.py**: not available without the challenge zip. Once
  obtained, run it locally per section 8.1 step 8 and read every
  per-dimension score before deploying to EC2.
- With more time: expand `_select_candidate_triggers` to also fan out
  direct-to-customer triggers (currently the router only handles
  merchant-scoped and category-wide digest triggers), and add
  Hinglish-specific composition templates in `fallback.py` (currently
  Hinglish is only exercised via the conversation intent-transition
  detector, not the outbound composer templates).

## Testing performed in this environment

- `pytest tests/` — 65 tests, all passing, including a new
  `tests/test_real_dataset.py` suite that loads directly from
  `magicpin-ai-challenge/dataset/*.json` (not hand-written fixtures):
  - Contract tests for all 5 endpoints, including idempotency, the 409
    stale-version contract, oversized-payload rejection (413),
    missing-field validation (4xx), and unknown-trigger-id handling.
  - Offer-parsing tests against real "@ ₹NNN" merchant/category titles.
  - Trigger-fact resolution tests against real kind-specific payloads
    (perf_dip deltas, research_digest→category digest lookups, competitor
    names, supply-alert batch numbers).
  - End-to-end `compose()` tests against real merchant+category+trigger
    JSON, both merchant-facing and customer-facing (Priya/recall_due),
    with live Bedrock calls (a `.env` with real Bedrock credentials is
    present in this environment) verified fact-clean.
  - Category-catalog-offer-leakage regression test reproducing a live
    bug found during this pass (SK Pizza Junction / IPL trigger citing a
    category-template offer it doesn't actually have).
  - Full-trigger-kind-coverage test asserting every kind in the real
    dataset has a dedicated strategy + fallback template.
  - Large-batch (25-trigger) tick test verifying the 20-action cap,
    zero suppression-without-delivery data loss, and correct dedup on
    a follow-up tick.
  - Conversation state machine tests reproducing the official worked
    examples (hard opt-out + suppression, auto-reply send→wait→end
    escalation using the *exact* official canned phrase, Hinglish intent
    transition, imperative-rephrasing engaged-intent detection).
  - Concurrency test (10 parallel requests) and a determinism test.
- Full local harness runs (`test_harness.py` adapted to `localhost:8000`)
  against a live `uvicorn` server with real Bedrock calls: pushed the
  complete real dataset (5 categories, 10 merchants, 15 customers, 25
  triggers), ran all 25 triggers through one `/v1/tick` (confirmed 20-cap,
  zero data loss on the follow-up tick), ran the full reply-scenario suite
  (engaged, auto-reply escalation, opt-out, intent transition, curveball,
  hostile, Hinglish, anti-repetition), and spot-checked message quality
  across all 5 verticals plus customer-facing recall/lapse/bridal triggers.
- Not yet run: deployment to EC2, Caddy/TLS, systemd unit under multiple
  Gunicorn workers with real Redis, and the official `judge_simulator.py`
  against a public URL (this pass focused on local verification per the
  user's request — EC2 push is the next step once this is reviewed).

## Running locally

```bash
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 8000
```

Optional `.env` (see `.env.example`) for local Bedrock testing with an IAM
user key, and `REDIS_URL` if running Redis. Without either, the service
runs correctly on the deterministic fallback composer and in-memory state
(single process only).

Run tests:
```bash
pytest tests/ -v
```
