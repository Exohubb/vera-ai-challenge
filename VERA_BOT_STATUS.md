# Vera — Status Report

**Team**: Airo (Kapil Meena) · **Version**: 2.1.4 · **Endpoint**: `https://100-56-247-98.sslip.io`
**Model**: `amazon.nova-pro-v1:0` via Amazon Bedrock, with a deterministic grounded-template fallback
**Report date**: 2026-09-10

---

## 1. Current status

| Check | Result |
|---|---|
| Service | Live, `systemd` + Gunicorn/Uvicorn (2 workers), Redis-backed state |
| All 5 endpoints | Implemented and schema-conformant (`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`) |
| Local test suite | **124 passing**, 0 failing |
| Canonical 30 pairs | **30/30 compose**, two ticks, both HTTP 200, no crashes |
| Independent judge score | **42.00/50 (84.0%)**, **0 penalties** across all 30 messages |
| `/v1/tick` latency, 20 actions cold cache | **12.81s** against a 30s budget (~17s headroom) |
| `/v1/healthz` under tick load | 7/7 probes HTTP 200, all < 2s |
| Phase 3 adaptation | Verified — adopts newly injected context, no stale leakage |
| Phase 4 replay behaviours | Verified — auto-reply, intent transition, hostile/off-topic, opt-out |

Scoring was performed by an **independent judge model** (Google Gemini) against the
5-dimension rubric in `challenge-brief.md` §8. This matters: the only earlier
self-evaluation available used Amazon Nova to grade Nova's own output, which is
favourably biased and was treated as unusable.

---

## 2. Measured score progression

Every row is a full 30-pair run against the live service, same judge, same rubric.

| Build | Score | Specificity | Category | Merchant | Trigger | Engagement | Penalties |
|---|---|---|---|---|---|---|---|
| baseline | 32.97 / 65.9% | 5.90 | 6.83 | 6.60 | 7.20 | 6.53 | 3 |
| iteration 1 | 36.90 / 73.8% | 7.17 | 7.57 | 7.27 | 8.00 | 7.13 | 7 |
| iteration 2 | 39.53 / 79.1% | 7.63 | 7.83 | 8.43 | 8.27 | 7.57 | 6 |
| iteration 3 | 41.00 / 82.0% | 8.20 | 7.93 | 8.63 | 8.53 | 7.77 | 2 |
| **2.1.4 (current)** | **42.00 / 84.0%** | **8.30** | **8.20** | **8.93** | **8.50** | **8.07** | **0** |

Net: **+9.03 points (+27% relative)**, every dimension improved, and penalties
driven from 3 to **0**. One pair (T07) scored a perfect 50/50.

The penalty column is the figure worth reading. It rose to 7 during iteration 1 —
pushing for specificity increased fabrication risk — and only closed to 0 once the
grounding gates caught up. Specificity and zero-fabrication are in direct tension on
this dataset, and the current build sits where they balance.

---

## 3. Root cause behind the original score

The dataset ships 25 hand-authored triggers with real event payloads and expands the
remaining 75 synthetically, emitting a bare payload:

```json
{ "placeholder": true, "metric_or_topic": "<kind>" }
```

**13 of the 30 graded pairs land on one of those.** Splitting the baseline run by
payload type isolated the problem precisely:

| Pair group | n | Baseline average |
|---|---|---|
| Real hand-authored payload | 17 | **39.8 / 50** |
| Synthetic placeholder payload | 13 | **24.1 / 50** |

All five worst-scoring messages were placeholder pairs, and the judge's stated reason
was consistently the same: *"too generic"*, *"no concrete verifiable facts"*,
*"could be copy-pasted to any merchant"*.

This was **not a model-capability problem**. Nova scored 39–48/50 whenever it had real
data to work with. Every one of the worst messages came from a deterministic fallback
template that the model never had a chance to improve on, because the trigger carried
nothing to anchor to.

---

## 4. What changed

### 4.1 Derived-fact layer for placeholder payloads — the primary fix

When a trigger payload carries no event data, the merchant, customer and category
contexts pushed alongside it are still full of real, citable numbers. The resolver now
falls back to those instead of degrading to generic copy:

- 7-day performance deltas (`performance.delta_7d`)
- category peer benchmarks (`peer_stats`) with above/below/at-par handling
- cumulative customer counts (`customer_aggregate`)
- subscription state (lapsed / renewing soon)
- customer visit history (visit count, last-visit date)

Every value is read verbatim from a pushed payload. Where nothing exists anywhere, the
resolver returns nothing, which is what lets templates fall back honestly rather than
inventing specificity. A regression test asserts that **no resolver emits a digit when
given empty contexts**.

*Effect, live:* `"Vikas, your numbers are trending up this week"` → `"Vikas, your calls
are up 5% week-on-week (now 14 over 30 days)"`.

### 4.2 Defects found by the independent judge

Each of these was a real bug that the unit suite passed over. All are now covered by a
regression test naming the pair it came from.

| Defect | Consequence | Fix |
|---|---|---|
| Peer comparison returned nothing when a merchant sat **exactly** at the category average | Fully generic message (scored 15/50) | Sentinel corrected; exact ties now report "in line with the peer benchmark" |
| A dentist was sent a pharmacy refill message (*"your regular medicines are running low"*) | 5/50 with a fabrication penalty | Refill wording restricted to dispensing categories; others reframe as care-continuity |
| `vs_baseline` (a window total) rendered as a **per-day** rate | Judge flagged the misread | Now cited without inventing a time unit |
| A Tue–Thu-only offer recommended on a **Saturday** | −1 penalty, merchant fit 3/10 | Day-restricted offers suppressed when the day conflicts |
| Diwali pitched with live urgency **188 days** out | Trigger relevance 3/10 | Beyond ~8 weeks reframed as a planning horizon |
| A new-patients-only offer promised to a patient with **5 prior visits** | −1 penalty, false eligibility claim | New gate rejects new-customer offers for established customers |
| A confirmed appointment asserted to a **lapsed** customer with no booking data | −2 penalty, trigger relevance 2/10 | New gate; message now offers an open slot instead |
| `"CTR are 0.025"`, and a comparison claiming *"above"* two identical numbers | Sloppy voice, self-contradiction | Per-metric verb agreement; correct at-par wording |
| Trend tokens rendered as `"ORS demand +40"` (unitless) | Specificity 4/10 | Now `"ORS demand +40%"` |
| Passive dormancy close (*"let me know if you ever need help"*) | Engagement 3/10, twice | Now offers to draft one concrete thing |

### 4.3 Compliance defect (not a scoring issue)

**A bare `STOP` was not recognised as an opt-out.** Every pattern required "stop" plus a
second word (`"stop messaging"`, `"please stop"`), so a message of exactly `"STOP"` fell
through to *"no clear signal → wait"*: the conversation stayed open and the merchant was
never suppressed. `challenge-brief.md` §3 specifies Vera's own CTAs as a **"binary choice
(YES/STOP)"** — the service was instructing merchants to reply STOP and then ignoring it.

Verified live before the fix (`action: wait`) and after (`action: end` with suppression).
Standalone-keyword detection now covers `STOP`, `STOPALL`, `UNSUBSCRIBE`, `OPT OUT`,
`QUIT`, `REVOKE` and variants, matched only when the keyword is the **entire** message —
so `"don't stop sending these"` does not trip it. Ambiguous single words (`"no"`,
`"cancel"`) are deliberately excluded: wrongly suppressing a merchant for 30 days is
worse than missing one opt-out.

### 4.4 A measurement bug in our own evaluation harness

Two apparent fabrication penalties were **artefacts of the test harness, not the bot**.
The harness omitted `customer_aggregate` from the merchant context it showed the scoring
model, so a legitimately grounded figure (`total_unique_ytd: 889`) was judged invented;
it also never stated who a message was addressed to, so a correct customer-directed
message was marked as misdirected to the merchant.

After passing the full merchant/customer payload and declaring the addressee, those two
pairs moved **13 → 43** and **34 → 44** with no change to the bot. Recorded here because
it is a live risk: if the official judge shows its scorer less context than the bot
received, correctly grounded citations can still read as fabricated.

---

## 5. Verification performed

### Operational limits

| Measurement | Result | Limit |
|---|---|---|
| `/v1/tick`, 20 actions, cold cache | 12.81s / 12.41s | 30s |
| Per-action composition | 0.64s | — |
| `/v1/tick`, warm cache | 3.70s | 30s |
| `/v1/healthz` during a 12.41s tick | 7/7 × HTTP 200, max 1.87s | 3 consecutive failures = disqualification |
| 355 context pushes (reused connection) | 93s (263ms each) | 15-minute warmup window |

Composition is concurrent, not sequential — the 0.64s-per-action figure confirms it.
Worst case is also bounded: on a transient Bedrock failure the composer returns
immediately rather than retrying, so a throttle burst cannot multiply into a timeout.

### Phase 3 — adaptive context injection

Simulated the injection sequence from the testing brief §4:

- **New digest item** (category re-pushed at a higher version) — the send cited the new
  41% finding and new source, and leaked **no** stale facts from the superseded item.
- **Updated performance snapshot** (views 2410→9999, calls 18→77) — the send used the
  **fresh** figures (*"views are up 44% week-on-week (now 9999 over 30 days)"*) with zero
  stale leakage.
- **Mid-test customer push followed by `recall_due`** — composed correctly, greeting the
  new customer by name and citing the real due date, real open slot and real offer, with
  `send_as: merchant_on_behalf`.
- No hallucination in any case.

### Phase 4 — replay scenarios

| Scenario | Expected | Observed |
|---|---|---|
| Auto-reply hell (same canned text ×4) | detect and exit | `send` → `wait` → `end` → `end` |
| Intent transition (*"ok let's do it"*) | switch to action, no re-pitch | confirmed the specific agreed item |
| Hostile, then off-topic (GST question) | stay on-mission politely | declined the unrelated ask, redirected to the live topic |
| Hard opt-out (`STOP`) | end + suppress | `end` + 30-day merchant suppression |

---

## 6. Deliverables

| Path | Role |
|---|---|
| `bot.py` | `compose(category, merchant, trigger, customer)` per `challenge-brief.md` §7.1 — deterministic, standalone-runnable |
| `submission.jsonl` | 30 canonical messages per §7.2, regenerated from the live build; contract-checked (non-empty bodies, valid CTA, valid `send_as`, no URLs, no internal jargon) |
| `README.md` | Approach, tradeoffs, and the context that would have helped most (§7.3) |
| `vera-bot/` | The live service and shared composition pipeline |
| `vera-bot/tests/` | 124 tests, including a regression per judge finding above |
| `gemini_judge.py` | Independent-judge harness used for every measurement in this report |

`send_as` distribution across the 30: 21 `vera`, 9 `merchant_on_behalf`.

---

## 7. Known limitations

Stated plainly rather than omitted.

1. **Run-to-run variance is significant.** Nova is not perfectly deterministic on a cold
   cache, so individual pairs swung widely across runs (one moved 22 → 43 → 20 → 43). The
   aggregate trend across five runs is the trustworthy signal; a single pair's number is
   not. Treat 42.00 as roughly ±1.5–2.
2. **One judge model was used.** The official judge is a different, unknown model with a
   different prompt. The direction of these fixes should transfer; the absolute figure may
   not.
3. **The remaining low scorers are data-limited.** Four pairs sit at 26–37, all
   placeholder-payload cases. They rose from 5–15, but cannot realistically reach 45+
   without real trigger data — and closing the gap by asserting more would reintroduce the
   fabrication penalties that are currently at zero.
4. **Determinism has a boundary.** `compose()` caches on a hash of the full input context
   and Bedrock Nova accepts no seed and rejects `temperature=0` exactly. Repeat calls with
   identical inputs are stable, and match the deployed service; byte-identical output
   across a cold cache on different hardware is not guaranteed.
5. **Dataset coherence is handled defensively, not solved.** The synthetic expander
   attaches trigger kinds to categories where they make no sense (`chronic_refill_due` to a
   dentist, `appointment_tomorrow` to a merchant with no booking data). These are detected
   and reframed; a coherent assignment would be better than any mitigation.
