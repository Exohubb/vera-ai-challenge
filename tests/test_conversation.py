"""Conversation state machine tests (section 8.1 item 2, examples C/D/E)."""
from conversation import ConversationManager


def test_hard_opt_out_ends_and_suppresses():
    mgr = ConversationManager()
    result = mgr.handle_reply(
        conversation_id="conv_test_optout",
        from_role="merchant",
        message="please stop messaging me",
        turn_number=3,
        merchant_id="m_test_optout",
    )
    assert result["action"] == "end"
    assert mgr.suppression.is_merchant_suppressed("m_test_optout")


def test_auto_reply_escalation_send_wait_end():
    mgr = ConversationManager()
    conv_id = "conv_test_autoreply"
    msg = "Thank you for your message. I am currently unavailable."

    r1 = mgr.handle_reply(conv_id, "merchant", msg, 1)
    assert r1["action"] == "send"

    r2 = mgr.handle_reply(conv_id, "merchant", msg, 2)
    assert r2["action"] == "wait"

    r3 = mgr.handle_reply(conv_id, "merchant", msg, 3)
    assert r3["action"] == "end"


def test_intent_transition_hinglish():
    mgr = ConversationManager()
    result = mgr.handle_reply(
        conversation_id="conv_test_intent",
        from_role="merchant",
        message="haan kar do, chalu karo",
        turn_number=2,
    )
    assert result["action"] == "send"
    assert result["intent"] == "intent_transition"


def test_intent_transition_english():
    mgr = ConversationManager()
    result = mgr.handle_reply(
        conversation_id="conv_test_intent_en",
        from_role="merchant",
        message="yes go ahead, send it",
        turn_number=2,
    )
    assert result["action"] == "send"
    assert result["intent"] == "intent_transition"


def test_turn_depth_cap():
    mgr = ConversationManager()
    conv_id = "conv_test_turncap"
    for i in range(1, 9):
        result = mgr.handle_reply(conv_id, "merchant", "just checking in", i)
    assert result["action"] == "end"
    assert result["intent"] == "turn_cap"


def test_anti_repetition_via_manager_fingerprint():
    mgr = ConversationManager()
    conv_id = "conv_test_dedup"
    body = "Great — sending your offer now."
    assert mgr.has_been_sent(conv_id, body) is False
    mgr.record_sent(conv_id, body)
    assert mgr.has_been_sent(conv_id, body) is True


def test_auto_reply_detects_official_canned_phrasing():
    """Test report §3.5: the official challenge-testing-brief.md worked
    example (§2.5) and judge_simulator.py's own hardcoded auto-reply test
    message use "Thank you for contacting ... Our team will respond
    shortly." -- this must be detected as auto_reply, not fall through to
    the generic wait bucket."""
    mgr = ConversationManager()
    conv_id = "conv_test_official_autoreply"
    msg = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."

    r1 = mgr.handle_reply(conv_id, "merchant", msg, 1)
    assert r1["intent"] == "auto_reply_first"
    assert r1["action"] == "send"

    r2 = mgr.handle_reply(conv_id, "merchant", msg, 2)
    assert r2["intent"] == "auto_reply_repeat"
    assert r2["action"] == "wait"

    r3 = mgr.handle_reply(conv_id, "merchant", msg, 3)
    assert r3["intent"] == "auto_reply_final"
    assert r3["action"] == "end"


def test_engaged_intent_catches_imperative_rephrasing():
    """Test report §3.6: 'tell me the offer details again please' (no "?",
    no listed question word) must classify as engaged, not fall through
    to generic/wait."""
    mgr = ConversationManager()
    result = mgr.handle_reply(
        conversation_id="conv_test_imperative",
        from_role="merchant",
        message="tell me the offer details again please",
        turn_number=2,
    )
    assert result["intent"] == "engaged"
    assert result["action"] == "send"


def test_bare_stop_keyword_is_a_hard_opt_out():
    """challenge-brief.md section 3 specifies Vera's CTAs as a "binary choice
    (YES/STOP)" -- Vera itself tells merchants to reply STOP. Every
    _OPT_OUT_PATTERNS entry required "stop" plus a second word ("stop
    messaging", "please stop", "stop sending"), so a message of exactly
    "STOP" matched none of them and fell through to the generic
    "no clear signal -> wait" branch. Verified live against the deployed
    service before the fix: "STOP" -> action=wait (conversation left open,
    merchant left un-suppressed), while "Stop messaging me" correctly ended.
    """
    from conversation import _is_bare_opt_out

    for text in ["STOP", "stop", "Stop", "STOP.", " stop ", "STOP!", "stop all",
                 "unsubscribe", "UNSUB", "opt out", "opt-out", "optout", "quit", "revoke"]:
        assert _is_bare_opt_out(text) is True, f"{text!r} must be treated as a hard opt-out"


def test_bare_opt_out_does_not_misfire_on_ambiguous_or_embedded_text():
    from conversation import _is_bare_opt_out

    # Substring matches must not fire -- the opposite meaning.
    assert _is_bare_opt_out("don't stop sending these") is False
    assert _is_bare_opt_out("please don't stop") is False
    # Ambiguous single words: suppressing a merchant for 30 days on either of
    # these would be much worse than missing an opt-out.
    assert _is_bare_opt_out("no") is False
    assert _is_bare_opt_out("cancel") is False
    # Normal replies.
    assert _is_bare_opt_out("yes please send it") is False
    assert _is_bare_opt_out("") is False
    assert _is_bare_opt_out(None) is False


def test_bare_stop_ends_conversation_and_suppresses_merchant():
    """End-to-end through the real conversation manager, not just the helper."""
    import uuid

    from conversation import ConversationManager

    manager = ConversationManager()
    merchant_id = f"m_test_bare_stop_{uuid.uuid4().hex[:8]}"

    result = manager.handle_reply(
        conversation_id=f"conv_bare_stop_{uuid.uuid4().hex[:8]}",
        merchant_id=merchant_id,
        from_role="merchant",
        message="STOP",
        turn_number=1,
    )
    assert result["action"] == "end"
    assert result["intent"] == "opt_out"
    assert manager.suppression.is_merchant_suppressed(merchant_id) is True
