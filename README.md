# Vera — magicpin AI Challenge

An AI assistant that engages merchants (and their customers) over WhatsApp: it decides
*whether* to message, *why now*, and *what to say* — grounded strictly in the context it
has been given.

**Team**: Airo (Kapil Meena) · **Version**: 2.1.4
**Live endpoint**: `https://100-56-247-98.sslip.io`
**Model**: `amazon.nova-pro-v1:0` via Amazon Bedrock, with a deterministic grounded-template fallback

| | |
|---|---|
| Independent judge score | **42.00 / 50 (84.0%)** across all 30 canonical pairs, **0 penalties** |
| Test suite | **124 passing** |
| `/v1/tick` latency (20 actions, cold cache) | **12.8s** against a 30s budget |
| Endpoints | All 5 implemented and schema-conformant |

Scored by an **independent model** (Google Gemini) against the 5-dimension rubric in
`challenge-brief.md` §8 — deliberately not by Nova, which would be grading its own output.
Full methodology and per-defect history: [`VERA_BOT_STATUS.md`](VERA_BOT_STATUS.md).

---

## Approach

Composition is treated as a **decision problem first, a wording problem second**. The model
never picks strategy; it only writes prose for a decision already made in code.

```
trigger ──► trigger_router    27 kinds → (lever, CTA type, priority), an explicit table
        ──► trigger_facts     structured payload → ONE verifiable plain-English claim
        ──► composer          Nova drafts → 6 grounding gates → named-correction retry
                                          → deterministic template if still unclean
        ──► suppression       per-trigger + per-merchant, so restraint is enforced
```

**1. Strategy lives in a table, not a prompt.** All 27 trigger kinds present in the dataset
map to an explicit lever, CTA type and priority (`trigger_router.py`). A drug recall cannot
accidentally inherit the same low-pressure framing as a dormancy check-in, and ranking
across competing triggers in one tick is testable without invoking a model.

**2. Every message is anchored to one verifiable fact.** `trigger_facts.py` converts each
kind's structured payload into a single claim built only from fields verbatim present in the
pushed contexts. Where a payload carries no event data — the dataset synthesises 75 of its
100 triggers as `{"placeholder": true}` — a **derived-fact layer** grounds the message in
real adjacent signals instead: 7-day performance deltas, category peer benchmarks,
cumulative customer counts, subscription state, visit history. When nothing exists anywhere
it returns nothing, which is what lets templates degrade honestly rather than invent
specificity. A test asserts no resolver emits a digit when given empty contexts.

**3. Nova drafts; gates decide what ships.** Every draft must clear numeric-claim
verification, fabricated clock times, invented URLs, category-vs-merchant offer leakage,
new-customer offer eligibility, and unsupported appointment assertions — plus per-category
banned-phrase and single-CTA checks. A failure produces a *named* correction prompt (the
model is told which check it failed) and one retry, then a deterministic grounded template.
Roughly a third of canonical messages ship from templates by design, not as an error path.

**4. Conversations are a state machine.** `/v1/reply` classifies intent (opt-out, auto-reply,
intent transition, off-topic, engaged) and maps it to `send`/`wait`/`end`. Auto-reply loops
escalate send → wait → end rather than burning turns; a hard opt-out suppresses the merchant
for 30 days.

---

## Layout

| Path | Role |
|---|---|
| `bot.py` | §7.1 deliverable — `compose(category, merchant, trigger, customer)`; run directly to regenerate `submission.jsonl` |
| `submission.jsonl` | §7.2 deliverable — 30 canonical messages |
| `main.py` | FastAPI service, all 5 endpoints |
| `trigger_router.py` | trigger kind → lever / CTA / priority table |
| `trigger_facts.py` | payload → verifiable claim, incl. the derived-fact layer |
| `composer.py` | draft → gates → correction retry → fallback |
| `fact_check.py` | the six grounding gates |
| `fallback.py` | deterministic grounded-template library |
| `conversation.py` | reply intent classification + state machine |
| `offers.py` · `category_styles.py` · `vera_persona.py` | offer parsing, per-category voice, prompts |
| `context_store.py` · `state_backend.py` · `suppression.py` · `validators.py` | state, persistence, suppression, request validation |
| `tests/` | 124 tests, including a regression per defect in `VERA_BOT_STATUS.md` |
| `gemini_judge.py` | independent-judge harness used for every score in this repo |

---

## Running it

```bash
pip install -r requirements.txt

cp .env.example .env        # then add your Bedrock credentials
uvicorn main:app --host 0.0.0.0 --port 8080
```

Without credentials the service still starts and returns valid, grounded messages via the
fallback library — the LLM path simply never engages.

```bash
python3 -m pytest tests/ -q     # 124 tests
python3 bot.py                  # regenerate submission.jsonl
```

`bot.py` and `gemini_judge.py` need magicpin's dataset package, which is **not vendored
here** (it is their material). Place `magicpin-ai-challenge/` alongside this directory or
one level up, or set `MAGICPIN_DATASET_DIR`.

### Configuration

| Variable | Purpose |
|---|---|
| `AWS_BEARER_TOKEN_BEDROCK`, `AWS_REGION`, `BEDROCK_MODEL_ID` | Bedrock access for the Nova path |
| `REDIS_URL` | state backend; falls back to in-memory when unset |
| `VERA_STATE_BACKEND=memory` | force in-memory state (used by `bot.py`) |
| `TEAM_CONTACT_EMAIL`, `SUBMITTED_AT` | override `/v1/metadata` fields |

`.env` and `.judge_env` hold live credentials and are gitignored. Never commit them.

---

## Tradeoffs

- **Determinism over variety.** `compose()` caches on a hash of the full input context. Nova
  exposes no seed and rejects `temperature=0` exactly, so identical inputs are served from
  cache rather than re-rolled — repeatable, at the cost of never finding a better second
  phrasing.
- **A large fallback library instead of retry-until-good.** More code, but a bounded latency
  budget (8s LLM timeout inside a 30s contract) and no scenario where nothing gets sent.
- **Zero fabrication enforced mechanically, accepting lower specificity.** Several gates
  suppress a claim rather than let a plausible-but-ungrounded number through. Some messages
  score lower on specificity as a direct result. Since the rubric penalises fabrication and
  the judge injects unseen context after submission, this is the deliberate trade — and it
  is why the current build reports 0 penalties.
- **Fail-soft everywhere.** Malformed context degrades one message, never a batch;
  `/v1/tick` drops only an offending action rather than the whole response.

## What additional context would have helped most

1. **Real payloads for the synthesised triggers.** 13 of the 30 graded pairs carry
   `{"placeholder": true}`. Split by payload type, hand-authored pairs averaged 39.8/50
   against 24.1/50 for placeholders — the single largest scoring gap.
2. **Category coherence in trigger assignment.** `chronic_refill_due` is attached to
   dentists, `appointment_tomorrow` to merchants with no booking data. Handling these means
   detecting and reframing a semantically impossible trigger.
3. **A trustworthy "now" per trigger.** Without one, day-count arithmetic against
   `last_visit` is unsafe, so dates are cited verbatim rather than as "N days ago" — less
   natural, but never wrong.
4. **Offer eligibility metadata on merchant offers.** Category catalogs carry `audience`;
   merchant offers do not, so eligibility has to be inferred from title wording.
