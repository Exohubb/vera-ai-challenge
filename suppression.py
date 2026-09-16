"""Per-trigger and per-merchant suppression windows (section 5).

- Per-trigger suppression: once a trigger fires for a merchant, suppress
  that exact trigger for a cooldown window (default 7 days) even if
  /v1/tick offers it again.
- Per-merchant suppression: after a hard opt-out, suppress ALL outbound
  messages to that merchant for a longer window (default 30 days).
- Conversation dedup: at most one open conversation per
  (merchant_id, trigger_id) pair at a time.
"""
from __future__ import annotations

import time
from typing import Optional

from state_backend import StateBackend

TRIGGER_COOLDOWN_SECONDS = 7 * 24 * 3600
MERCHANT_OPT_OUT_COOLDOWN_SECONDS = 30 * 24 * 3600


class SuppressionStore:
    def __init__(self, redis_url: Optional[str] = None):
        self._trigger_backend = StateBackend("suppress:trigger", redis_url)
        self._merchant_backend = StateBackend("suppress:merchant", redis_url)
        self._open_conv_backend = StateBackend("suppress:openconv", redis_url)

    # -- per-trigger suppression -------------------------------------
    def suppress_trigger(
        self,
        merchant_id: str,
        suppression_key: str,
        cooldown_seconds: int = TRIGGER_COOLDOWN_SECONDS,
    ) -> None:
        key = f"{merchant_id}:{suppression_key}"
        self._trigger_backend.set(
            key, {"suppressed_until": time.time() + cooldown_seconds}, ttl_seconds=cooldown_seconds
        )

    def is_trigger_suppressed(self, merchant_id: str, suppression_key: str) -> bool:
        key = f"{merchant_id}:{suppression_key}"
        rec = self._trigger_backend.get(key)
        if rec is None:
            return False
        return time.time() < rec["suppressed_until"]

    # -- per-merchant hard suppression (opt-out) ----------------------
    def suppress_merchant(
        self, merchant_id: str, cooldown_seconds: int = MERCHANT_OPT_OUT_COOLDOWN_SECONDS
    ) -> None:
        self._merchant_backend.set(
            merchant_id,
            {"suppressed_until": time.time() + cooldown_seconds, "reason": "opt_out"},
            ttl_seconds=cooldown_seconds,
        )

    def is_merchant_suppressed(self, merchant_id: str) -> bool:
        rec = self._merchant_backend.get(merchant_id)
        if rec is None:
            return False
        return time.time() < rec["suppressed_until"]

    # -- conversation dedup: 1 open conversation per (merchant, trigger) --
    #
    # NOTE ON conversation_id LINKAGE: the /v1/tick contract does not return
    # a conversation_id (the harness only gets merchant_id + trigger_id back),
    # and the /v1/reply contract's example payload only carries
    # conversation_id + from_role + message + turn_number -- merchant_id and
    # trigger_id are NOT guaranteed to be present on the reply. Because of
    # that gap in the published contract, we register/close this dedup
    # guard keyed on (merchant_id, trigger_id) at the two points we DO
    # reliably know that pair: when we send a tick action (register), and
    # when a reply resolves to a terminal state AND we've been able to
    # recover the trigger_id for that merchant (close). A TTL is applied so
    # a merchant who never replies doesn't block that trigger forever.
    def register_open_conversation(
        self,
        merchant_id: str,
        trigger_id: str,
        conversation_id: str,
        customer_id: Optional[str] = None,
        ttl_seconds: int = TRIGGER_COOLDOWN_SECONDS,
    ) -> bool:
        """Returns True if newly registered, False if one is already open
        (caller should not start a duplicate). customer_id is remembered
        too (when the trigger was customer-scoped) so /v1/reply can
        recover CustomerContext for a thread even though the published
        /v1/reply contract doesn't echo customer_id back on every turn."""
        key = f"{merchant_id}:{trigger_id}"
        existing = self._open_conv_backend.get(key)
        if existing is not None:
            return False
        self._open_conv_backend.set(
            key,
            {"conversation_id": conversation_id, "customer_id": customer_id},
            ttl_seconds=ttl_seconds,
        )
        return True

    def close_open_conversation(self, merchant_id: str, trigger_id: str) -> None:
        self._open_conv_backend.delete(f"{merchant_id}:{trigger_id}")

    def has_open_conversation(self, merchant_id: str, trigger_id: str) -> bool:
        return self._open_conv_backend.get(f"{merchant_id}:{trigger_id}") is not None

    def is_bot_opened_conversation(self, merchant_id: str, conversation_id: str) -> bool:
        """True when `conversation_id` is one this bot itself opened for
        this merchant via /v1/tick (it was registered above with that exact
        id), as opposed to a thread id minted elsewhere and pointed at this
        merchant.

        This is the attribution test behind conversation.py's opt-out scope
        rule: a merchant-wide 30-day suppression is applied only for an
        opt-out arriving on an outbound we can trace to a tick decision of
        ours. See _apply_opt_out_suppression for the full reasoning and the
        measured failure it prevents.
        """
        if not merchant_id or not conversation_id:
            return False
        for key in self._open_conv_backend.keys(f"{merchant_id}:"):
            rec = self._open_conv_backend.get(key)
            if rec and rec.get("conversation_id") == conversation_id:
                return True
        return False

    def get_open_trigger_for_merchant(self, merchant_id: str) -> Optional[str]:
        """Best-effort recovery of which trigger a merchant's currently-open
        conversation belongs to, used by /v1/reply to ground LLM replies
        when the harness doesn't echo trigger_id back to us. Returns the
        first open trigger_id found for this merchant, or None."""
        for key in self._open_conv_backend.keys(f"{merchant_id}:"):
            return key.split(":", 1)[1] if ":" in key else None
        return None

    def get_open_customer_for_merchant(self, merchant_id: str) -> Optional[str]:
        """Companion to get_open_trigger_for_merchant: recovers the
        customer_id (if any) tied to a merchant's currently-open
        conversation, so /v1/reply can reload CustomerContext when
        grounding a customer-facing thread's reply."""
        for key in self._open_conv_backend.keys(f"{merchant_id}:"):
            rec = self._open_conv_backend.get(key)
            return rec.get("customer_id") if rec else None
        return None
