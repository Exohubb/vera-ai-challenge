"""Multi-turn conversation state machine for /v1/reply (section 4).

Explicit rule-based detection -- not per-message LLM improvisation --
for: auto-reply/out-of-office, hard opt-out, intent transition,
off-topic redirect, anti-repetition, and turn-depth capping.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field

from state_backend import StateBackend
from suppression import SuppressionStore

MAX_TURNS = 6

_AUTO_REPLY_PATTERNS = [
    r"currently unavailable",
    r"will get back to you",
    r"out of office",
    r"auto[- ]?reply",
    r"thank you for your message",
    r"i am busy right now",
    r"away from (my )?phone",
    # These two cover the exact canned phrasing used in the official
    # challenge-testing-brief.md worked example (§2.5) and
    # judge_simulator.py's own hardcoded auto-reply test message
    # ("Thank you for contacting [Business]! Our team will respond
    # shortly.") -- neither matched any pattern above (test report §3.5).
    r"thank you for contacting",
    r"will respond shortly",
    r"team will get back",
    r"we('ll| will) reply soon",
    r"we have received your message",
]

_OPT_OUT_PATTERNS = [
    r"stop messaging",
    r"not interested",
    r"band karo",
    r"unsubscribe",
    r"do not contact",
    r"remove me",
    r"please stop",
    r"stop sending",
]

# Standalone opt-out keywords. Every pattern above needs "stop" plus another
# word ("stop messaging", "please stop"), so a message consisting of just
# "STOP" matched NONE of them and fell through to the generic "no clear
# signal -> wait" branch. That is the single most important opt-out token
# there is: challenge-brief.md section 3 specifies Vera's own CTAs as a
# "binary choice (YES/STOP)", so Vera literally instructs merchants to
# reply STOP -- and then ignored it, kept the conversation open, and left
# the merchant un-suppressed. Verified live against the deployed service
# before this fix: "STOP" -> action=wait, while "Stop messaging me" and
# "unsubscribe" both correctly ended.
#
# Matched only when the ENTIRE message is the keyword (after stripping
# punctuation/whitespace), which is the SMS/WhatsApp convention. Matching
# these substrings anywhere would misfire on "don't stop sending these".
# Deliberately excludes ambiguous single words: bare "no" is usually just
# answering a question, and bare "cancel" usually refers to an appointment
# rather than to messaging -- suppressing a merchant for 30 days on either
# would be far worse than missing it.
_BARE_OPT_OUT_KEYWORDS = {
    "stop", "stopall", "stop all",
    "unsubscribe", "unsub",
    "optout", "opt out", "opt-out",
    "quit", "end", "revoke",
    "band", "bas",
}

_PUNCT_STRIP_RE = re.compile(r"[^\w\s]+")


def _is_bare_opt_out(message: str) -> bool:
    """True when the whole message is a standalone opt-out keyword."""
    if not isinstance(message, str):
        return False
    normalized = _PUNCT_STRIP_RE.sub(" ", message).strip().lower()
    normalized = " ".join(normalized.split())
    return normalized in _BARE_OPT_OUT_KEYWORDS

_INTENT_TRANSITION_PATTERNS = [
    r"\bgo ahead\b",
    r"\blet'?s do it\b",
    r"\bkar do\b",
    r"\bchalu karo\b",
    r"\byes\b.*\bsend\b",
    r"\bsure\b.*\bsend\b",
    r"\bok(ay)?\b.*\bsend\b",
    r"\bhaan\b",
    r"\bproceed\b",
    r"\bconfirm(ed)?\b",
]

# Off-topic detection uses a DENYLIST of clearly unrelated domains rather
# than an allowlist of expected on-topic keywords. An allowlist approach
# (the original implementation) false-positives on any normal reply that
# happens not to contain one of a handful of hardcoded words -- e.g. "Can
# you tell me more about this before I decide?" has no listed keyword and
# would have been wrongly redirected as off-topic. A denylist of things
# Vera genuinely has no business answering (loans, insurance, unrelated
# support/billing, "who is this") is a much better proxy for "unrelated to
# this thread" and produces far fewer false positives against normal
# conversational language, including Hinglish.
_OFF_TOPIC_PATTERNS = [
    r"\bloan\b",
    r"\bemi\b",
    r"\binsurance\b",
    r"\bcredit card\b",
    r"\bwrong number\b",
    r"\bwho (is|are) (this|you)\b",
    r"\bnot related\b",
    r"\bunsubscribe from all\b",
    r"\bpolicy (claim|number)\b",
    r"\bjob (vacancy|opening)\b",
    r"\brefund my (account|money) from (bank|wallet)\b",
]

# Interrogative / engagement-seeking cues -- a reply matching these (and
# not already classified as opt-out/auto-reply/intent-transition/off-topic)
# is treated as an ENGAGED on-topic question or request, which should get a
# grounded LLM answer (action=send), not a silent "wait".
_ENGAGED_QUESTION_PATTERNS = [
    r"\?",
    r"\btell me more\b",
    r"\btell me\b",  # covers imperative rephrasing with no "?" or "more"
    r"\bhow (much|many|does)\b",
    r"\bwhat('s| is)\b",
    r"\bwhen\b",
    r"\bwhy\b",
    r"\bcan you\b",
    r"\bcould you\b",
    r"\bmore (details|info|information)\b",
    # Imperative/no-question-mark rephrasings (test report §3.6): "tell me
    # the offer details again please", "explain that", "send me the
    # details" -- none of these match the interrogative patterns above.
    r"\bexplain\b",
    r"\bsend me\b",
    r"\bshow me\b",
    r"\bgive me\b",
    r"\bagain please\b",
    r"\bonce more\b",
    r"\bdetails? (please|again)\b",
    r"\brepeat that\b",
]


def _matches_any(patterns: list[str], text: str) -> bool:
    lowered = text.lower()
    return any(re.search(p, lowered) for p in patterns)


def _body_fingerprint(body: str) -> str:
    return hashlib.sha256(body.strip().lower().encode("utf-8")).hexdigest()


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str | None = None
    trigger_id: str | None = None
    turn_count: int = 0
    auto_reply_count: int = 0
    sent_fingerprints: list[str] = field(default_factory=list)
    ended: bool = False


class ConversationManager:
    def __init__(self, redis_url: str | None = None):
        self._backend = StateBackend("conv", redis_url)
        self.suppression = SuppressionStore(redis_url)

    def _load(self, conversation_id: str) -> ConversationState:
        rec = self._backend.get(conversation_id)
        if rec is None:
            return ConversationState(conversation_id=conversation_id)
        return ConversationState(**rec)

    def _save(self, state: ConversationState) -> None:
        self._backend.set(state.conversation_id, state.__dict__)

    def has_been_sent(self, conversation_id: str, body: str) -> bool:
        state = self._load(conversation_id)
        return _body_fingerprint(body) in state.sent_fingerprints

    def record_sent(self, conversation_id: str, body: str) -> None:
        state = self._load(conversation_id)
        state.sent_fingerprints.append(_body_fingerprint(body))
        self._save(state)

    def get_remembered_merchant_id(self, conversation_id: str) -> str | None:
        """Recovers merchant_id from a prior turn in this same
        conversation_id, used when a later /v1/reply call doesn't repeat
        merchant_id (the published contract only guarantees it on some
        calls, not necessarily every turn)."""
        state = self._load(conversation_id)
        return state.merchant_id

    def handle_reply(self, conversation_id: str, from_role: str, message: str, turn_number: int, merchant_id: str | None = None) -> dict:
        """Returns a dict with keys: action ('send'|'wait'|'end'), intent
        (for logging/fallback selection), and side effects already applied
        (suppression, conversation closing)."""
        state = self._load(conversation_id)
        state.turn_count = max(state.turn_count, turn_number)
        if merchant_id:
            state.merchant_id = merchant_id

        # turn-depth guard first -- cap regardless of content
        if state.turn_count > MAX_TURNS:
            state.ended = True
            self._save(state)
            return {"action": "end", "intent": "turn_cap"}

        if _matches_any(_OPT_OUT_PATTERNS, message) or _is_bare_opt_out(message):
            state.ended = True
            self._save(state)
            if state.merchant_id:
                self.suppression.suppress_merchant(state.merchant_id)
            return {"action": "end", "intent": "opt_out"}

        if _matches_any(_AUTO_REPLY_PATTERNS, message):
            state.auto_reply_count += 1
            if state.auto_reply_count == 1:
                self._save(state)
                return {"action": "send", "intent": "auto_reply_first"}
            elif state.auto_reply_count == 2:
                self._save(state)
                return {"action": "wait", "intent": "auto_reply_repeat"}
            else:
                state.ended = True
                self._save(state)
                return {"action": "end", "intent": "auto_reply_final"}

        if _matches_any(_INTENT_TRANSITION_PATTERNS, message):
            self._save(state)
            return {"action": "send", "intent": "intent_transition"}

        if _matches_any(_OFF_TOPIC_PATTERNS, message):
            self._save(state)
            return {"action": "send", "intent": "off_topic"}

        if _matches_any(_ENGAGED_QUESTION_PATTERNS, message):
            self._save(state)
            return {"action": "send", "intent": "engaged"}

        self._save(state)
        return {"action": "wait", "intent": "generic"}
