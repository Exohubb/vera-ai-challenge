"""One-line-style smoke test for the Bedrock connection, per section 1.1
step 4 of the build prompt: confirm boto3 can actually call InvokeModel
before wiring the full app around it. Run directly:

    python3 bedrock_smoke_test.py

Loads .env (AWS_BEARER_TOKEN_BEDROCK / AWS_REGION / BEDROCK_MODEL_ID) and
makes one real call to Nova through composer.compose(), using the same
Vera persona/system-prompt path the live app uses. Prints whether the LLM
path was actually used or whether it silently fell back to the
deterministic templates (which would mean auth/model-access failed).
"""
from __future__ import annotations

import json
import os

from dotenv import load_dotenv

load_dotenv()

# Import after load_dotenv so composer picks up env vars at client-build time.
import composer  # noqa: E402


def main():
    print(f"AWS_REGION={os.environ.get('AWS_REGION')}")
    print(f"BEDROCK_MODEL_ID={os.environ.get('BEDROCK_MODEL_ID')}")
    print(f"AWS_BEARER_TOKEN_BEDROCK set: {'AWS_BEARER_TOKEN_BEDROCK' in os.environ}")

    merchant_payload = {
        "identity": {"name": "Dr. Meera", "category": "dentists", "languages": ["en"]},
        "performance": {"ctr": 0.021, "peer_ctr_median": 0.030},
        "offers": [{"name": "Dental Check Up", "price": 299}],
    }
    trigger = {
        "kind": "research_digest",
        "fact": "190 people in your area searched for 'Dental Check Up' this week",
        "trigger_id": "trg_research_digest_dentists",
    }

    # Call the raw Bedrock path directly so we can tell definitively
    # whether the LLM responded, vs. compose() silently using the fallback.
    from vera_persona import build_user_message
    from trigger_router import get_strategy
    from category_styles import get_category_style

    strategy = get_strategy(trigger["kind"])
    style = get_category_style("dentists")
    user_message = build_user_message("dentists", merchant_payload, trigger, strategy, style)

    print("\n--- calling Bedrock directly (composer._call_bedrock) ---")
    raw = composer._call_bedrock(user_message)
    if raw is None:
        print("RESULT: Bedrock call FAILED or returned unusable output (see WARNING log above).")
        print("The app would fall back to deterministic templates for this request.")
    else:
        print("RESULT: Bedrock call SUCCEEDED. Raw model output:")
        print(json.dumps(raw, indent=2, ensure_ascii=False))

    print("\n--- full compose() call (includes fact-check + fallback safety net) ---")
    result = composer.compose(category="dentists", merchant_payload=merchant_payload, trigger=trigger)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    used_llm = raw is not None and result.get("body") == raw.get("body")
    print(f"\nComposer used: {'BEDROCK/NOVA' if used_llm else 'DETERMINISTIC FALLBACK'}")


if __name__ == "__main__":
    main()
