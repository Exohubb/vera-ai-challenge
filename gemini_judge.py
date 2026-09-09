#!/usr/bin/env python3
"""
Independent Gemini judge for the magicpin AI Challenge canonical 30
test pairs.

Why this exists: the only prior "judge" run (judge_simulator_bedrock.py,
Aug 30) used Amazon Bedrock Nova to score Nova-composed messages -- same
model family scoring its own output, which the file's own docstring flags
as an unreliable, favorably-biased sanity check, not a real signal. This
script uses Gemini (a genuinely independent model family) against the
actual 5-dimension rubric from challenge-brief.md section 8, scoring the
already-composed, already-verified 30 canonical action bodies pulled
live from the deployed bot (deploy_logs/canonical_30_v144_final.json).

Usage:
    python3 gemini_judge.py

Reads the API key from vera-bot/.judge_env (GEMINI_API_KEY=...), which is
gitignored and never printed. Writes a full report to
deploy_logs/gemini_judge_report.json and a human-readable summary to
stdout (redirect to a deploy_logs file per this workspace's convention).
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _find_challenge_package() -> Path:
    """magicpin's dataset package is not vendored into this repo. Look for
    it alongside this file, then one level up (the original layout), or
    honour an explicit MAGICPIN_DATASET_DIR override."""
    override = os.environ.get("MAGICPIN_DATASET_DIR")
    candidates = [Path(override)] if override else []
    candidates += [ROOT / "magicpin-ai-challenge", ROOT.parent / "magicpin-ai-challenge"]
    for candidate in candidates:
        if (candidate / "dataset").is_dir():
            return candidate
    raise SystemExit(
        "Could not find the magicpin-ai-challenge dataset package. Place it "
        "alongside this file or one directory up, or set MAGICPIN_DATASET_DIR."
    )


_PACKAGE = _find_challenge_package()
EXPANDED = _PACKAGE / "expanded"
DATASET = _PACKAGE / "dataset"

# Actions to score: override with VERA_ACTIONS_FILE. Default is a tick
# capture sitting next to this script.
ACTIONS_FILE = Path(os.environ.get("VERA_ACTIONS_FILE", ROOT / "canonical_30_actions.json"))
REPORT_FILE = Path(os.environ.get("VERA_JUDGE_REPORT", ROOT / "gemini_judge_report.json"))

GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


def load_api_key() -> str:
    env_path = ROOT / ".judge_env"
    with open(env_path) as f:
        for line in f:
            if line.startswith("GEMINI_API_KEY="):
                return line.strip().split("=", 1)[1]
    raise RuntimeError("GEMINI_API_KEY not found in vera-bot/.judge_env")


def load_json(path: Path):
    with open(path) as f:
        return json.load(f)


SYSTEM_PROMPT = """You are a STRICT, independent judge for the magicpin AI Challenge.
You score a merchant-engagement WhatsApp-style message from an AI assistant
called Vera. Score across exactly 5 dimensions, each 0-10 (5 = average,
7+ = good, 9+ = excellent). Be strict -- do not default to the middle.

1. SPECIFICITY: Does the message anchor on a concrete, VERIFIABLE fact drawn
   from the contexts provided (a number, date, headline, peer stat, price)?
   Penalize generic framings like "10% off" or "increase your sales" that
   aren't tied to real data below.

2. CATEGORY FIT: Does the voice, vocabulary, and offer format match the
   category? (Dentists: clinical peer tone. Salons: warm/practical.
   Restaurants: fellow-operator tone. Gyms: coaching/motivational.
   Pharmacies: trustworthy/precise.) Penalize retail-promo tone in
   clinical categories or vice versa.

3. MERCHANT FIT: Is the message personalized to THIS SPECIFIC merchant's
   actual state (their real numbers, real offers, real conversation
   history)? Is their language preference honored? Penalize anything that
   could be copy-pasted to a different merchant unchanged.

4. TRIGGER RELEVANCE: Does the message clearly communicate WHY NOW -- the
   specific trigger that prompted this message, using its actual payload
   data? Penalize generic nudges not tied to the stated trigger kind.

5. ENGAGEMENT COMPULSION: Would a real merchant/customer want to reply?
   Does it use compulsion levers (curiosity, social proof, loss aversion,
   effort externalization, single clear low-friction CTA)?

ALSO check for penalties (apply as negative adjustment, 0 to -3 total):
- Fabricating a fact/number/date NOT present in any context below: -2
- Exposing internal jargon (e.g. "suppression_key", "trigger_id") to the
  merchant/customer in the message body itself: -1
- Grammatically broken or nonsensical English/Hindi: -1

Respond with ONLY this exact JSON (no markdown fences, no commentary):
{
  "specificity": <0-10 int>,
  "specificity_reason": "<1 sentence>",
  "category_fit": <0-10 int>,
  "category_fit_reason": "<1 sentence>",
  "merchant_fit": <0-10 int>,
  "merchant_fit_reason": "<1 sentence>",
  "trigger_relevance": <0-10 int>,
  "trigger_relevance_reason": "<1 sentence>",
  "engagement_compulsion": <0-10 int>,
  "engagement_reason": "<1 sentence>",
  "penalties": <0-3 int>,
  "penalty_reasons": ["<reason>", ...],
  "one_line_verdict": "<1 sentence overall verdict>"
}"""


def build_prompt(test_id, action, category, merchant, trigger, customer):
    body = action.get("body", "")
    return f"""SCORE THIS MESSAGE (test pair {test_id}):

=== CATEGORY CONTEXT ({category.get('slug', 'unknown')}) ===
Voice: {json.dumps(category.get('voice', {}))}
Offer catalog: {json.dumps(category.get('offer_catalog', []))}
Peer stats: {json.dumps(category.get('peer_stats', {}))}
Digest items: {json.dumps(category.get('digest', []))}

=== MERCHANT CONTEXT (complete, verbatim -- this is the FULL merchant
=== payload the bot received; any number appearing anywhere in this JSON
=== is grounded, including nested fields like customer_aggregate) ===
{json.dumps(merchant, indent=1)}

=== TRIGGER CONTEXT ===
Kind: {trigger.get('kind')}
Payload: {json.dumps(trigger.get('payload', {}))}
Urgency: {trigger.get('urgency')}

=== CUSTOMER CONTEXT (complete, verbatim) ===
{json.dumps(customer, indent=1) if customer else "None -- this is a merchant-facing message"}

=== WHO THIS MESSAGE IS ADDRESSED TO ===
{
"The END CUSTOMER named above, written in the merchant's voice on their behalf "
"(send_as=merchant_on_behalf). Judge it as a message the CUSTOMER receives, "
"NOT as a message to the merchant/owner."
if str(action.get("send_as")) == "merchant_on_behalf" and customer else
"The MERCHANT/owner directly, from Vera the assistant (send_as=vera)."
}

=== VERA'S COMPOSED MESSAGE ===
send_as: {action.get('send_as')}
Body: "{body}"
CTA: {action.get('cta')}
Rationale given by bot: "{action.get('rationale', '')}"

Score strictly per the 5-dimension rubric. Watch specifically for whether
any fact in the body (numbers, dates, percentages) actually appears in the
contexts above -- if not, that's fabrication (-2 penalty) and should also
tank the Specificity score."""


def call_gemini(api_key: str, system: str, prompt: str, retries: int = 5) -> str:
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system}]},
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        GEMINI_URL, data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    last_err = None
    for attempt in range(retries):
        try:
            resp = urllib.request.urlopen(req, timeout=45)
            data = json.loads(resp.read().decode("utf-8"))
            candidates = data.get("candidates", [])
            if not candidates:
                raise RuntimeError(f"No candidates in response: {data!r}")
            parts = candidates[0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            last_err = f"HTTP {e.code}: {err_body[:500]}"
            if e.code in (429, 503):
                time.sleep(20 * (attempt + 1))
                continue
            raise RuntimeError(last_err)
        except Exception as e:
            last_err = str(e)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Gemini call failed after {retries} retries: {last_err}")


def parse_score(text: str) -> dict:
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        raise RuntimeError(f"No JSON found in response: {text[:300]}")
    data = json.loads(match.group())
    for k in ("specificity", "category_fit", "merchant_fit", "trigger_relevance", "engagement_compulsion"):
        data[k] = min(10, max(0, int(data.get(k, 0))))
    data["penalties"] = min(3, max(0, int(data.get("penalties", 0))))
    return data


def main():
    api_key = load_api_key()

    actions_data = load_json(ACTIONS_FILE)
    actions_by_trigger = {a["trigger_id"]: a for a in actions_data["actions"]}

    pairs = load_json(EXPANDED / "test_pairs.json")["pairs"]

    categories = {}
    for f in sorted((DATASET / "categories").glob("*.json")):
        c = load_json(f)
        categories[c["slug"]] = c

    merchants = {}
    for f in sorted((EXPANDED / "merchants").glob("*.json")):
        m = load_json(f)
        merchants[m["merchant_id"]] = m

    triggers = {}
    for f in sorted((EXPANDED / "triggers").glob("*.json")):
        t = load_json(f)
        triggers[t["id"]] = t

    customers = {}
    for f in sorted((EXPANDED / "customers").glob("*.json")):
        c = load_json(f)
        customers[c["customer_id"]] = c

    # Resume support: if a prior partial report exists, keep its already
    # -scored results (avoid burning free-tier quota re-scoring pairs that
    # already succeeded across restarted runs).
    prior_scored = {}
    if REPORT_FILE.exists():
        try:
            prior = load_json(REPORT_FILE)
            for r in prior.get("results", []):
                if "total" in r:
                    prior_scored[r["test_id"]] = r
        except Exception:
            pass

    results = []
    print(f"Judging {len(pairs)} canonical test pairs with {GEMINI_MODEL} (independent judge)...")
    if prior_scored:
        print(f"Resuming: {len(prior_scored)} pairs already scored in a prior run, reusing those.")
    print(f"{'='*70}")

    for pair in pairs:
        if pair["test_id"] in prior_scored:
            r = prior_scored[pair["test_id"]]
            results.append(r)
            print(f"[{r['test_id']}] {r['total']:2d}/50  (cached from prior run)  {r.get('one_line_verdict', '')[:80]}")
            continue
        test_id = pair["test_id"]
        trigger_id = pair["trigger_id"]
        merchant_id = pair["merchant_id"]
        customer_id = pair.get("customer_id")

        action = actions_by_trigger.get(trigger_id)
        if not action:
            print(f"[{test_id}] SKIP -- no action found for trigger {trigger_id}")
            results.append({"test_id": test_id, "trigger_id": trigger_id, "error": "no_action_found"})
            continue

        merchant = merchants.get(merchant_id, {})
        category = categories.get(merchant.get("category_slug"), {})
        trigger = triggers.get(trigger_id, {})
        customer = customers.get(customer_id) if customer_id else None

        prompt = build_prompt(test_id, action, category, merchant, trigger, customer)

        try:
            raw = call_gemini(api_key, SYSTEM_PROMPT, prompt)
            score = parse_score(raw)
            total = (score["specificity"] + score["category_fit"] + score["merchant_fit"]
                     + score["trigger_relevance"] + score["engagement_compulsion"] - score["penalties"])
            score["total"] = max(0, total)
            score["test_id"] = test_id
            score["trigger_id"] = trigger_id
            score["merchant_id"] = merchant_id
            score["body"] = action.get("body", "")
            results.append(score)
            print(f"[{test_id}] {score['total']:2d}/50  "
                  f"(spec={score['specificity']} cat={score['category_fit']} "
                  f"merch={score['merchant_fit']} trig={score['trigger_relevance']} "
                  f"eng={score['engagement_compulsion']} pen=-{score['penalties']})  "
                  f"{score.get('one_line_verdict', '')[:80]}")
        except Exception as e:
            print(f"[{test_id}] ERROR: {e}")
            results.append({"test_id": test_id, "trigger_id": trigger_id, "error": str(e)})

        # Incremental checkpoint so a crash/quota-cutoff doesn't lose progress.
        with open(REPORT_FILE, "w") as f:
            json.dump({"model": GEMINI_MODEL, "results": results}, f, indent=2, ensure_ascii=False)

        time.sleep(4.5)  # free tier: 20 req/min => stay safely under that

    scored = [r for r in results if "total" in r]
    errored = [r for r in results if "error" in r]

    print(f"\n{'='*70}")
    print(f"SCORED: {len(scored)}/{len(pairs)}  ERRORED: {len(errored)}")

    if scored:
        avg_total = sum(r["total"] for r in scored) / len(scored)
        avg_spec = sum(r["specificity"] for r in scored) / len(scored)
        avg_cat = sum(r["category_fit"] for r in scored) / len(scored)
        avg_merch = sum(r["merchant_fit"] for r in scored) / len(scored)
        avg_trig = sum(r["trigger_relevance"] for r in scored) / len(scored)
        avg_eng = sum(r["engagement_compulsion"] for r in scored) / len(scored)
        total_penalties = sum(r["penalties"] for r in scored)

        print(f"\nAvg Specificity:          {avg_spec:.2f}/10")
        print(f"Avg Category Fit:         {avg_cat:.2f}/10")
        print(f"Avg Merchant Fit:         {avg_merch:.2f}/10")
        print(f"Avg Trigger Relevance:    {avg_trig:.2f}/10")
        print(f"Avg Engagement:           {avg_eng:.2f}/10")
        print(f"Total penalties applied:  {total_penalties}")
        print(f"\nAVERAGE SCORE: {avg_total:.2f}/50 ({avg_total/50*100:.1f}%)")

        worst = sorted(scored, key=lambda r: r["total"])[:5]
        print(f"\nLowest-scoring 5 (fix these first):")
        for r in worst:
            print(f"  {r['test_id']}: {r['total']}/50 -- {r.get('one_line_verdict', '')[:90]}")

    if errored:
        print(f"\nErrored pairs: {[r['test_id'] for r in errored]}")

    with open(REPORT_FILE, "w") as f:
        json.dump({"model": GEMINI_MODEL, "results": results}, f, indent=2, ensure_ascii=False)
    print(f"\nFull report written to {REPORT_FILE}")


if __name__ == "__main__":
    main()
