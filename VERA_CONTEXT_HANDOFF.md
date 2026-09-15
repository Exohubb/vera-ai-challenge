# Vera Bot — Full Context Handoff

**Purpose**: paste this into a new chat to restore complete context. Written 15 Sep 2026.

---

## 1. The situation in one paragraph

Kapil Meena (team **Airo**) built a bot for the **magicpin Vera AI Challenge** — a WhatsApp-style
merchant/customer message composer, judged live over HTTP by magicpin's automated harness. This is
tied to a **job application**: magicpin HR (Rishav Kumar) has an open **Product AI Analyst** role
(6-month internship at ₹38,000/month, converting to ₹10 LPA FTE, Gurgaon, in-office, Product Team,
immediate joining). **The challenge score gates the interview. The threshold is 70. Current score is
50.** First attempt scored 58. So the score went *down*, and we need 50 → 70+.

---

## 2. THE GOAL

Get the magicpin-reported score from **50 to 70+**. That is a ~40% relative improvement and will not
come from a re-roll or version bump. It needs real changes to the areas identified in §7.

---

## 3. Infrastructure / access

| Item | Value |
|---|---|
| Live endpoint | `https://100-56-247-98.sslip.io` |
| EC2 | `ubuntu@100.56.247.98`, key `~/Downloads/fun.pem` |
| Remote app path | `/home/ubuntu/vera-bot/` |
| Service | systemd `vera-bot.service`, Gunicorn + 2 Uvicorn workers, `--timeout 30` |
| State | Redis on `127.0.0.1`, `appendonly yes` |
| TLS | Caddy, auto Let's Encrypt via sslip.io |
| Live version | **2.2.0** |
| Local project root | `/home/exo/projects/buildathon-2/` |
| Bot source | `vera-bot/` (this IS the git repo) |
| GitHub | `https://github.com/Exohubb/vera-ai-challenge` (branch `main`) |
| Dataset | `magicpin-ai-challenge/` (magicpin's, not vendored into the repo) |
| Measurement artifacts | `evidence/` |

**Secrets** (never commit; both gitignored, never in git history):
- `vera-bot/.env` — `AWS_BEARER_TOKEN_BEDROCK`, `AWS_REGION`, `BEDROCK_MODEL_ID` (live Bedrock token)
- `vera-bot/.judge_env` — `GEMINI_API_KEY` (for the independent judge harness)

`deploy_logs/` was **deleted** — it contained the live AWS token in plaintext in one file.

---

## 4. CRITICAL WORKFLOW CONSTRAINTS

1. **Terminal stdout is suppressed for this user.** Every command must redirect output to a file, then
   read the file. Do not wait on stdout. File tools cannot access `/tmp` — write into the project dir.
2. **The `read_file` tool goes stale.** After writing a file, a read may return "does not exist" or old
   content. Re-read, or `sleep 2` first.
3. **Do not touch EC2 without explicit permission.** The user has repeatedly and emphatically said this
   (the endpoint is live and being graded). Read-only `journalctl`/`redis-cli` reads have been
   permitted when asked to investigate. **Never** restart, flush, deploy, or POST to
   `/v1/context`|`/v1/tick`|`/v1/reply` without asking — those mutate graded state.
4. Heredocs with redirects sometimes silently fail. Prefer writing a `.py` file then running it.

---

## 5. How magicpin actually grades (discovered from service logs — important)

The harness runs the **`phase2_short` scenario**, NOT the full 60-minute Phase 2 in the brief. Each run
takes ~40-60 seconds and pushes:

- 5 categories (all)
- 4 merchants: `m_001_drmeera_dentist_delhi`, `m_002_bharat_dentist_mumbai`,
  `m_003_studio11_salon_hyderabad`, `m_005_pizzajunction_restaurant_delhi`
- 1 customer: `c_001_priya_for_m001`
- **6 triggers, always the same ones, all from `dataset/triggers_seed.json` (all REAL payloads)**:

| Trigger | Kind | Merchant |
|---|---|---|
| `trg_001_research_digest_dentists` | research_digest | m_001 |
| `trg_002_compliance_dci_radiograph` | regulation_change | m_001 |
| `trg_003_recall_due_priya` | recall_due (customer) | m_001 |
| `trg_004_perf_dip_bharat` | perf_dip | m_002 |
| `trg_006_festival_diwali` | festival_upcoming | m_003 |
| `trg_011_review_theme_late_delivery` | review_theme_emerged | m_005 |

Then **10 reply turns across 7 conversations**: `conv_<runid>_merchant`, `_customer`, `_stop`,
`_intent`, `_hostile`, `_offtopic`, `_auto` (auto gets 4 turns).

The harness mints fresh trigger IDs per run (`trg_001_..._judge_2207_102`) and context
`version = 30000 + runid`.

**Observed runs**: `2195` (Sep 10, 6 actions), `2207` (Sep 14 07:31, 6 actions),
`2226` (Sep 14 07:45, **only 3 actions** — see §6). Also `2106` on Aug 31 (likely the 58).

**Key implication**: the 13 placeholder-payload canonical pairs are **never exercised** by the grader.
All 6 graded triggers have rich real payloads.

---

## 6. THE SUPPRESSION LEAK (highest-severity known bug)

The harness's own `_stop` and `_hostile` reply scenarios are classified as hard opt-out. Per the brief,
a hard opt-out applies a **30-day merchant-level suppression**. Those scenarios hit `m_001`, which owns
**3 of the 6 graded triggers**.

```
run N   → 6 messages composed OK
        → reply phase → STOP + hostile → opt_out → suppress:merchant:m_001 (30d TTL)
run N+1 → m_001's 3 triggers SKIPPED → only 3 of 6 messages
```

**This happened for real**: run `2226` returned only 3 actions, 13 minutes after run `2207`.
`main.py:315` does `if suppression.is_merchant_suppressed(mid): continue`.

The 3 lost messages are the **highest-scoring** ones (49, 48, 47 by our judge). If magicpin scores a
missing action as zero, a degraded run roughly halves the reported score. **This may well be why 50 < 58.**

State was manually flushed on 15 Sep, so it is currently clean — but the next run will re-create it.
**A durable fix is needed**, not a manual flush.

---

## 7. Remaining known weaknesses (the plan to reach 70)

Ordered by expected impact. **None of these are implemented yet.**

### 7.1 Reply handling — biggest suspect, never optimised
Every graded run shows the same pattern:
```
conv_*_merchant  → intent=generic → action=wait   (silent, no body sent)
conv_*_customer  → intent=generic → action=wait   (silent, no body sent)
conv_*_hostile   → intent=opt_out → action=end    (terminates on abuse)
```
3 of 7 conversations handled poorly, identically in every run. The brief's Phase 4 says the bot must
**"stay on-mission politely"** on hostile/off-topic — not hang up. A silent turn is plausibly scored as
a non-response. This code path has never been touched → genuine untouched headroom.

### 7.2 Durable fix for the suppression leak (§6)
Idea: only apply 30-day merchant suppression when the opt-out arrives on a conversation this bot
actually opened via a tick and can trace; expire far sooner otherwise. Must stay spec-compliant.

### 7.3 Preserve resolved hard numbers
`_f_research_digest` produces a fact containing `2100-patient trial` and `JIDA Oct 2026, p.14`, but Nova
paraphrases them away. In 4/4 controlled runs, `38%` and `2,100-patient` were **absent** from the final
message. The brief's **Appendix A uses this exact scenario as its gold standard** and names those
figures as why it scores well. Gates verify numbers present are grounded; nothing requires grounded
numbers be *used*. One-sided check.

### 7.4 Block wholly invented offers
One roll produced `'Diwali Glow' package with 20% off ... for the next 30 days` — none of which exists
in any context. `find_category_offer_leakage` catches citing a *category-catalog* offer as the
merchant's own, but not an offer invented from nothing. Bare `20`/`30` appear somewhere in payloads so
digit-matching passes it. Observed in ~1 of 5 rolls.

### 7.5 Get the breakdown from magicpin
`developers@magicpin.in` (note the **s** — `developer@` bounces). No reply yet. Highest-information
item: we don't know their scale or weighting, so we are partly optimising blind.

---

## 8. Nova non-determinism (why the score moved without code changes)

Bedrock Nova exposes **no seed** and **rejects `temperature=0` exactly** (valid range `(0.00001, 1]`).
Determinism is provided by a **cache**, not by the model.

- `_cache_key` (composer.py:113) hashes `{cache_version, category, merchant, trigger, customer, category_ctx}`
- `trigger` includes `trigger_id`, and the judge mints fresh IDs per run
- **Therefore pre-warming the cache CANNOT match a future graded run.** This idea is dead — verified.

Measured variance: composing the same 6 pairs 4× produced **2-4 distinct outputs per pair**.
So a chunk of 58 → 50 is genuinely the dice. Redis was flushed right before both submissions, so every
graded message was a fresh cold-cache roll.

---

## 9. Score history (all measured by us, independent Gemini judge)

magicpin's reported scores: **58** (attempt 1), **50** (attempt 2). Threshold **70**. Scale unknown.

Our own measurements using `gemini_judge.py` (Gemini = independent from Nova, so no self-grading bias):

| Build | 30 canonical pairs | Penalties |
|---|---|---|
| v1.4.4 baseline | 32.97/50 (65.9%) | 3 |
| v1.5.1 | 36.90/50 (73.8%) | 7 |
| v1.5.2 | 39.53/50 (79.1%) | 6 |
| v1.5.3 | 41.00/50 (82.0%) | 2 |
| **v1.5.5 / 2.x** | **42.00/50 (84.0%)** | **0** |

Final per-dimension: Specificity 8.30, Category Fit 8.20, Merchant Fit 8.93, Trigger Relevance 8.50,
Engagement 8.07.

**The 6 triggers magicpin actually scores**: our judge gave **45.67/50 (91.3%)**, 0 penalties
(49, 48, 47, 47, 44, 39).

**The gap between our 91% and their 50% is the central mystery.** It is far too large to be rubric
noise, and strongly implies their score includes something we don't measure — most likely reply
handling (§7.1) and/or a degraded run (§6).

Artifacts in `evidence/`: `final_judge_report_42of50.json`, `judge_run_6_scores.json`,
`judge_run_6_actions.json`, `variance_history.json`, `grounding_audit.txt`.

---

## 10. Bot architecture (what already exists and works)

```
trigger ──► trigger_router.py   27 kinds → (lever, CTA type, priority), explicit table
        ──► trigger_facts.py    payload → ONE verifiable fact; derived-fact layer for placeholders
        ──► composer.py         Nova draft → 6 gates → 1 named-correction retry → deterministic template
        ──► fallback.py         27-kind grounded template library
        ──► suppression.py      per-trigger 7d, per-merchant 30d, open-conversation dedup
```

Six grounding gates in `fact_check.py`: `verify_numeric_claims`, `find_fabricated_clock_times`,
`find_urls`, `find_new_customer_offer_mismatch`, `find_unsupported_appointment_assertion`, plus
`count_ctas`; and `find_category_offer_leakage` in `offers.py`, banned phrases in `category_styles.py`.

Design principle: **the LLM does wording; an explicit rule layer makes decisions.**

**Tests: 124 passing** (`cd vera-bot && python3 -m pytest tests/ -q`).
One pre-existing flaky test (`test_composer.py::test_example_b_missing_offer_no_invented_price`) fails
occasionally due to shared Redis state between tests — passes in isolation, not a real failure.

**Verified operational limits**: `/v1/tick` with 20 actions cold cache = **12.8s** of a 30s budget
(0.64s/action, composition is concurrent). `/v1/healthz` stayed 200 with <2s latency throughout a tick.
Phase 3 adaptation verified working (adopts newly injected digest/performance, no stale leakage).

---

## 11. Bugs already found and fixed (24 total, don't re-fix)

Highlights from this work — all covered by regression tests naming the canonical pair they came from:

- `category_styles` read a flat `payload["tone"]`; real data nests under `payload["voice"]` with
  `vocab_taboo`. **Every** real category push silently fell back to generic defaults.
- `appointment_tomorrow` + `customer_lapsed_soft` had no strategy mapping (4 canonical pairs affected).
- **Derived-fact layer**: 13 of 30 canonical pairs have `{"placeholder": true}` payloads. Split measured
  17 real-payload pairs at 39.8/50 vs 13 placeholder at 24.1/50. Now grounded in adjacent real context
  (7-day deltas, peer benchmarks, customer_aggregate, subscription, visit history). *Note: the grader
  never exercises these — see §5.*
- Peer comparison returned nothing when a merchant sat **exactly** at the category average (sentinel
  `0.0` vs `>= 0` guard) → fully generic 15/50 message.
- A **dentist** sent "your regular medicines are running low" (5/50 + fabrication penalty) — the
  generator attaches `chronic_refill_due` to non-pharmacies. Now category-gated.
- `vs_baseline` (a window total) was labelled a per-day rate.
- A Tue-Thu-only offer was recommended for a Saturday.
- Diwali pitched with live urgency 188 days out → now reframed as a planning horizon.
- New-patients-only offer promised to a patient with 5 prior visits.
- Confirmed appointment asserted to a lapsed customer with no booking data. First (enumeration-based)
  gate was **evaded live** by an unlisted phrasing; now works by co-occurrence + slot-offer exemption.
- **Bare `STOP` was not an opt-out** — every pattern needed "stop" + a second word, so `"STOP"` fell to
  *no-signal → wait*. The brief specifies Vera's CTAs as "binary choice (YES/STOP)", so the bot told
  merchants to reply STOP then ignored it. Compliance defect, now fixed.
- Surrogate-pair escapes crashed Starlette's JSON renderer → HTTP 500 for a whole tick batch.
- `/v1/context` invalid_scope returned a generic `{"error":...}` instead of the documented
  `{"accepted": false, "reason": "invalid_scope", "details": "..."}`.

**A measurement bug worth remembering**: `gemini_judge.py` originally omitted `customer_aggregate` from
the merchant context it showed the scorer, and never stated the addressee. Two pairs jumped **13→43**
and **34→44** with zero code change. If magicpin's judge shows its scorer less context than the bot
received, correctly grounded citations can still read as fabricated.

---

## 12. Deliverables (challenge-brief.md §7)

All committed and pushed to GitHub:
- `vera-bot/bot.py` — `compose(category, merchant, trigger, customer)`, deterministic, standalone-runnable
- `vera-bot/submission.jsonl` — 30 canonical messages, contract-checked
- `vera-bot/README.md` — rich format (badges, mermaid diagrams, LaTeX math, routing table, bug log).
  **The user specifically likes this format — preserve its structure, only update data.**
- `vera-bot/VERA_BOT_STATUS.md` — evidence trail
- `vera-bot/gemini_judge.py` — independent judge harness

`bot.py` and `gemini_judge.py` find the dataset via `MAGICPIN_DATASET_DIR` or a sibling/parent dir.

Git log (8 commits from this work, on top of 34 earlier): metadata/contract, category voice, trigger
routing, composer gates, opt-out, tests, deliverables, docs. Latest: `f74f9f0`.

---

## 13. HR / email timeline

| Date | Event |
|---|---|
| Sep 9 | Rishav emails: previous score **58**, invites retake within 24h |
| Sep 10 04:26 | Kapil resubmits + emails confirmation (location: Bhopal, MP) |
| Sep 10 09:24 | Judge run `2195` — 6 messages |
| Sep 10 11:35 | Rishav: updated score **50** |
| Sep 11 | Kapil asks for breakdown; `developer@magicpin.in` **bounces** |
| Sep 14 07:31 / 07:45 | Judge runs `2207` (6 msgs) and `2226` (**3 msgs**, suppression leak) |
| Sep 15 | Rishav gives correct address `developers@magicpin.in` (plural). No reply from them yet. |
| Sep 15 | Role details: Internship+FTE, 6mo, ₹38k/mo, ₹10 LPA FTE, Gurgaon in-office, Product Team, immediate joining |
| Sep 15 | **Rishav: "you need to score 70+ to qualify for the interview process"** — retake welcome |

Open threads: `developers@magicpin.in` (breakdown, no reply); Rishav (retake deadline).

---

## 14. Immediate next steps

1. **Implement §7.1 (reply handling)** — biggest suspected lever, never touched.
2. **Implement §7.2 (durable suppression fix)** — prevents a halved score on any second run.
3. Implement §7.3 and §7.4.
4. Run 124 tests, deploy, re-measure with `gemini_judge.py`.
5. **Ensure Redis state is clean immediately before the graded run.**
6. Chase `developers@magicpin.in` for the breakdown — we're optimising partly blind without it.

**Honest caveat to carry forward**: reaching 70 is not guaranteed. Their scale and weighting are
unknown, our judge already reports 91% on the graded 6, and single-run variance at n=6 is large. §7.1
and §7.2 are real defects with real headroom, but the mapping to their number is unproven.

---

## 15. Useful commands

```bash
# tests
cd /home/exo/projects/buildathon-2/vera-bot && python3 -m pytest tests/ -q

# independent judge (needs vera-bot/.judge_env)
cd vera-bot && VERA_ACTIVE_FILE=... python3 gemini_judge.py
# model: gemini-flash-lite-latest. NOTE: does NOT support thinkingConfig — omit it.
# free tier ~20 req/min; script paces at 4.5s and checkpoints incrementally

# regenerate submission.jsonl
cd vera-bot && python3 bot.py

# READ-ONLY EC2 inspection (safe)
ssh -i ~/Downloads/fun.pem ubuntu@100.56.247.98 \
  'sudo journalctl -u vera-bot.service --no-pager | grep -E "tick decision|reply decision|context push" | tail -40'
ssh -i ~/Downloads/fun.pem ubuntu@100.56.247.98 'redis-cli DBSIZE; redis-cli --scan --pattern "*suppress*"'

# DEPLOY (needs explicit user permission — note --exclude venv, and NEVER --delete)
rsync -avz -e "ssh -i ~/Downloads/fun.pem" --exclude='__pycache__' --exclude='.env' \
  --exclude='.judge_env' --exclude='.git' --exclude='venv' \
  vera-bot/ ubuntu@100.56.247.98:/home/ubuntu/vera-bot/
ssh -i ~/Downloads/fun.pem ubuntu@100.56.247.98 'sudo systemctl restart vera-bot.service'
```

⚠️ An earlier `rsync --delete` wiped the remote venv and it had to be rebuilt from `requirements.txt`.
Always exclude `venv`, never use `--delete`.
