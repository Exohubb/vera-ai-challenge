"""Payload validation: size caps and schema shape checks.

Keeps `/v1/context` from ever crashing on malformed or oversized judge
payloads -- section 7 of the build spec requires a clean 4xx here, not a
500 or a hang.
"""
from __future__ import annotations

import json
from typing import Any

MAX_CONTEXT_PAYLOAD_BYTES = 500 * 1024  # 500 KB cap per spec section 2

VALID_SCOPES = {"category", "merchant", "customer", "trigger"}


class ValidationError(Exception):
    """Raised for any malformed/oversized input. Caught in main.py -> 4xx."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def check_payload_size(raw_bytes: bytes) -> None:
    if len(raw_bytes) > MAX_CONTEXT_PAYLOAD_BYTES:
        raise ValidationError(
            f"payload exceeds 500KB cap ({len(raw_bytes)} bytes)", status_code=413
        )


def validate_context_request(body: dict[str, Any]) -> None:
    required = ["scope", "context_id", "version", "payload"]
    for field in required:
        if field not in body:
            raise ValidationError(f"missing required field: {field}", status_code=400)

    if body["scope"] not in VALID_SCOPES:
        raise ValidationError(
            f"invalid scope '{body['scope']}', must be one of {sorted(VALID_SCOPES)}",
            status_code=400,
        )

    if not isinstance(body["context_id"], str) or not body["context_id"]:
        raise ValidationError("context_id must be a non-empty string", status_code=400)

    if not isinstance(body["version"], int) or body["version"] < 0:
        raise ValidationError("version must be a non-negative integer", status_code=400)

    if not isinstance(body["payload"], dict):
        raise ValidationError("payload must be a JSON object", status_code=400)

    # re-serialize to double check size honestly reflects payload, not just
    # the outer envelope (defensive, cheap at this scale)
    try:
        raw = json.dumps(body["payload"]).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"payload not JSON-serializable: {exc}", status_code=400) from exc
    check_payload_size(raw)


def validate_tick_request(body: dict[str, Any]) -> None:
    if "now" not in body:
        raise ValidationError("missing required field: now", status_code=400)
    if "available_triggers" not in body:
        raise ValidationError("missing required field: available_triggers", status_code=400)
    if not isinstance(body["available_triggers"], list):
        raise ValidationError("available_triggers must be a list", status_code=400)


def validate_reply_request(body: dict[str, Any]) -> None:
    required = ["conversation_id", "from_role", "message", "turn_number"]
    for field in required:
        if field not in body:
            raise ValidationError(f"missing required field: {field}", status_code=400)
    if body["from_role"] not in {"merchant", "customer"}:
        raise ValidationError(
            "from_role must be 'merchant' or 'customer'", status_code=400
        )
    if not isinstance(body["turn_number"], int) or body["turn_number"] < 1:
        raise ValidationError("turn_number must be a positive integer", status_code=400)
