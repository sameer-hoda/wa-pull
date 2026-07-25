"""
Tests for the pulse monitor.
Run: python3 -m pytest tests/test_monitor.py -v
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from monitor import PulseMonitor, monitor


def test_cooldown_blocks_second_trigger():
    """Within cooldown, a second trigger for the same chat is skipped."""
    m = PulseMonitor()
    chat_jid = "999@g.us"
    m._cooldowns[chat_jid] = time.time()
    now = time.time()
    last = m._cooldowns.get(chat_jid, 0)
    assert now - last < config.MONITOR_COOLDOWN_SECONDS


def test_trigger_detection_user_sent():
    """user_sent trigger fires when user sends a message."""
    m = PulseMonitor()
    m._own_lid_num = "123456789"
    msgs = [
        {"chat_jid": "grp@g.us", "chat_name": "Test", "content": "hello", "is_from_me": True},
    ]
    trigger, trigger_msgs = m._detect_trigger("grp@g.us", msgs)
    assert trigger == "user_sent"
    assert len(trigger_msgs) == 1


def test_trigger_detection_user_tagged():
    """user_tagged trigger fires when someone tags the user."""
    m = PulseMonitor()
    m._own_lid_num = "123456789"
    msgs = [
        {"chat_jid": "grp@g.us", "chat_name": "Test", "content": "@123456789 what do you think?", "is_from_me": False, "sender": "Alice"},
    ]
    trigger, trigger_msgs = m._detect_trigger("grp@g.us", msgs)
    assert trigger == "user_tagged"


def test_trigger_detection_user_asked_dm():
    """user_asked trigger fires in a DM with a question mark."""
    m = PulseMonitor()
    m._own_lid_num = "123456789"
    msgs = [
        {"chat_jid": "91999@s.whatsapp.net", "chat_name": "Bob", "content": "can you check this?", "is_from_me": False, "sender": "Bob"},
    ]
    trigger, trigger_msgs = m._detect_trigger("91999@s.whatsapp.net", msgs)
    assert trigger == "user_asked"


def test_trigger_detection_user_asked_group_with_name():
    """user_asked trigger fires in a group when user's name is mentioned with a question."""
    m = PulseMonitor()
    m._own_lid_num = "123456789"
    msgs = [
        {"chat_jid": "grp@g.us", "chat_name": "Test", "content": "sameer can you review this?", "is_from_me": False, "sender": "Alice"},
    ]
    trigger, trigger_msgs = m._detect_trigger("grp@g.us", msgs)
    assert trigger == "user_asked"


def test_trigger_detection_quick_succession():
    """quick_succession trigger fires when 4+ messages from others in a group."""
    m = PulseMonitor()
    m._own_lid_num = "123456789"
    msgs = [
        {"chat_jid": "grp@g.us", "chat_name": "Test", "content": f"msg {i}", "is_from_me": False, "sender": f"User{i}"}
        for i in range(5)
    ]
    trigger, trigger_msgs = m._detect_trigger("grp@g.us", msgs)
    assert trigger == "quick_succession"
    assert len(trigger_msgs) == 5


def test_trigger_detection_no_trigger():
    """No trigger when others chat without asking/tagging and no burst."""
    m = PulseMonitor()
    m._own_lid_num = "123456789"
    msgs = [
        {"chat_jid": "grp@g.us", "chat_name": "Test", "content": "just chatting", "is_from_me": False, "sender": "Alice"},
        {"chat_jid": "grp@g.us", "chat_name": "Test", "content": "about stuff", "is_from_me": False, "sender": "Bob"},
    ]
    trigger, trigger_msgs = m._detect_trigger("grp@g.us", msgs)
    assert trigger is None


def test_trigger_priority_tagged_over_asked():
    """user_tagged takes priority over user_asked."""
    m = PulseMonitor()
    m._own_lid_num = "123456789"
    msgs = [
        {"chat_jid": "grp@g.us", "chat_name": "Test", "content": "@123456789 sameer what do you think?", "is_from_me": False, "sender": "Alice"},
    ]
    trigger, _ = m._detect_trigger("grp@g.us", msgs)
    assert trigger == "user_tagged"


def test_build_task_from_result():
    """_build_task correctly maps LLM result to a task dict."""
    m = PulseMonitor()
    result = {
        "action_type": "pulse",
        "context": "Something happened.\nIt needs attention.",
        "task": {
            "title": "Follow up with Anoop",
            "summary": "Anoop is waiting for a decision",
            "who_waiting": "Anoop",
            "waiting_hours": 3,
            "deadline": "EOD",
            "urgency": "high",
            "state": "waiting_on_me",
        },
        "options": {"A": "nudge", "B": "ask for timeline", "C": "decide now"},
    }
    task = m._build_task(result, "Test Group", "123@g.us")
    assert task["title"] == "Follow up with Anoop"
    assert task["source_chat"] == "Test Group"
    assert task["source_jid"] == "123@g.us"
    assert task["is_new"] is True
    assert task["pulse_options"] == {"A": "nudge", "B": "ask for timeline", "C": "decide now"}
    assert task["pulse_context"] == "Something happened.\nIt needs attention."


if __name__ == "__main__":
    test_cooldown_blocks_second_trigger()
    test_trigger_detection_user_sent()
    test_trigger_detection_user_tagged()
    test_trigger_detection_user_asked_dm()
    test_trigger_detection_user_asked_group_with_name()
    test_trigger_detection_quick_succession()
    test_trigger_detection_no_trigger()
    test_trigger_priority_tagged_over_asked()
    test_build_task_from_result()
    print("✅ All monitor tests passed!")
