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
