"""magicpin AI Challenge submission -- Team Airo (Vera).

This module is the `bot.py` deliverable described in challenge-brief.md
section 7.1: a single `compose(category, merchant, trigger, customer)`
entry point that takes the raw dataset dicts and returns the composed
message.

It is a thin adapter, not a second implementation. The live HTTP service
(challenge-testing-brief.md's 5-endpoint contract) and this function run
the exact same composition pipeline, so a message produced here is
identical to what the deployed endpoint returns for the same inputs:

    trigger_facts.resolve_fact()   turn the trigger's structured payload
                                   (or, when it's a bare placeholder, the
                                   real merchant/customer/category context
                                   pushed alongside it) into one verifiable
                                   plain-English fact
    trigger_router.get_strategy()  map the trigger kind to an explicit
                                   lever + CTA type (table in code, not
                                   left to the model)
    composer.compose()             Nova drafts, then every draft passes
                                   six grounding gates plus banned-phrase
                                   and single-CTA checks, with one named-
                                   correction retry, falling back to a
                                   deterministic grounded template if the
                                   model cannot produce a clean draft

Determinism (required by 7.1): `composer.compose` caches on a hash of the
full input context, and Nova is invoked at its minimum temperature. Repeat
calls with identical inputs return the identical dict. Note that Bedrock's
Nova Invoke API accepts no seed and rejects temperature=0 exactly, so
byte-identical output across a *cold* cache on *different* machines is not
something we can promise; within a process, and against the deployed
service, it holds.

Usage:
    from bot import compose
    result = compose(category_dict, merchant_dict, trigger_dict, customer_dict_or_None)
    # -> {"body", "cta", "send_as", "suppression_key", "rationale"}

Requires a .env alongside this file (AWS_BEARER_TOKEN_BEDROCK, AWS_REGION,
BEDROCK_MODEL_ID) for the Nova path. Without credentials it still returns
valid, grounded messages via the deterministic fallback library.

Running `python3 bot.py` regenerates submission.jsonl for the 30 canonical
pairs using only this module.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:  # load the same .env the live service uses; harmless if absent
    from dotenv import load_dotenv

    load_dotenv(_HERE / ".env")
except ImportError:
    pass

# In-process state backend: this deliverable is a pure function per the
# brief, with no judge pushing context over HTTP and no Redis to talk to.
os.environ.setdefault("VERA_STATE_BACKEND", "memory")

from composer import compose as _compose  # noqa: E402
from trigger_facts import resolve_fact  # noqa: E402


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    """Compose one merchant- or customer-directed WhatsApp message.

    Args:
        category: a `dataset/categories/<slug>.json` object.
        merchant: a merchant context object (merchants_seed.json shape).
        trigger:  a trigger context ENVELOPE (triggers_seed.json shape) --
                  i.e. the object with `kind`/`payload`/`suppression_key`,
                  not the inner payload alone.
        customer: a customer context object, or None for a merchant-facing
                  message. When present, the message is written to the
                  customer in the merchant's voice (`send_as` becomes
                  `merchant_on_behalf`).

    Returns:
        {"body", "cta", "send_as", "suppression_key", "rationale"}

    Never raises. Any malformed or missing field degrades to a lower-
    specificity but still fully grounded message rather than an exception,
    because a dropped message scores zero and a thrown exception could
    take down a whole batch.
    """
    category = category if isinstance(category, dict) else {}
    merchant = merchant if isinstance(merchant, dict) else {}
    trigger = trigger if isinstance(trigger, dict) else {}
    customer = customer if isinstance(customer, dict) else None

    category_slug = merchant.get("category_slug") or category.get("slug") or "unknown"
    trigger_kind = trigger.get("kind", "")
    inner_payload = trigger.get("payload") if isinstance(trigger.get("payload"), dict) else {}

    # Same fact-resolution the live /v1/tick path performs, including the
    # derived-fact layer that grounds placeholder-payload triggers in the
    # real numbers carried by the surrounding contexts.
    fact = resolve_fact(
        trigger_kind,
        inner_payload,
        category_payload=category,
        merchant_payload=merchant,
        customer_payload=customer,
    )

    result = _compose(
        category=category_slug,
        merchant_payload=merchant,
        trigger={
            "kind": trigger_kind,
            "fact": fact,
            "trigger_id": trigger.get("id", ""),
            "payload": inner_payload,
        },
        customer_payload=customer,
        category_payload=category,
    )

    return {
        "body": result["body"],
        "cta": result.get("cta", "open_ended"),
        "send_as": "merchant_on_behalf" if customer else "vera",
        "suppression_key": trigger.get("suppression_key", ""),
        "rationale": result.get("rationale", ""),
    }


def _find_challenge_package() -> Path | None:
    """Locate magicpin's dataset package, which is not vendored into this
    repo (it is their material, not ours). Checked in order: an explicit
    MAGICPIN_DATASET_DIR override, alongside this file, then one level up
    -- which is where it sits in the original working layout."""
    override = os.environ.get("MAGICPIN_DATASET_DIR")
    candidates = [Path(override)] if override else []
    candidates += [_HERE / "magicpin-ai-challenge", _HERE.parent / "magicpin-ai-challenge"]
    for candidate in candidates:
        if (candidate / "dataset").is_dir():
            return candidate
    return None


if __name__ == "__main__":
    import json

    package = _find_challenge_package()
    if package is None:
        sys.exit(
            "Could not find the magicpin-ai-challenge dataset package.\n"
            "Place it alongside this file or one directory up, or set "
            "MAGICPIN_DATASET_DIR to its path."
        )

    dataset = package / "dataset"
    expanded = package / "expanded"
    if not (expanded / "test_pairs.json").exists():
        sys.exit(
            f"Expected the expanded dataset at {expanded}.\n"
            "Generate it first with: python3 dataset/generate_dataset.py"
        )

    categories = {}
    for path in sorted((dataset / "categories").glob("*.json")):
        payload = json.load(open(path))
        categories[payload["slug"]] = payload

    def _load(subdir: str, key: str) -> dict:
        out = {}
        for path in sorted((expanded / subdir).glob("*.json")):
            payload = json.load(open(path))
            out[payload[key]] = payload
        return out

    merchants = _load("merchants", "merchant_id")
    triggers = _load("triggers", "id")
    customers = _load("customers", "customer_id")
    pairs = json.load(open(expanded / "test_pairs.json"))["pairs"]

    rows = []
    for pair in pairs:
        merchant = merchants.get(pair["merchant_id"], {})
        category = categories.get(merchant.get("category_slug"), {})
        trigger = triggers.get(pair["trigger_id"], {})
        customer = customers.get(pair["customer_id"]) if pair.get("customer_id") else None
        composed = compose(category, merchant, trigger, customer)
        composed["test_id"] = pair["test_id"]
        rows.append(composed)
        print(f"[{pair['test_id']}] {composed['body'][:100]}")

    with open(_HERE / "submission.jsonl", "w") as fh:
        for row in rows:
            fh.write(json.dumps({
                "test_id": row["test_id"], "body": row["body"], "cta": row["cta"],
                "send_as": row["send_as"], "suppression_key": row["suppression_key"],
                "rationale": row["rationale"],
            }, ensure_ascii=False) + "\n")
    print(f"\nwrote {len(rows)} lines to {_HERE / 'submission.jsonl'}")
