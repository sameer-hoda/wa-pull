"""
Tests for task_extractor and handlers (formatting + option extraction).
Run: python3 -m pytest tests/test_handlers.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import task_extractor
import handlers


def test_format_task_list_empty():
    msg = task_extractor.format_task_list([])
    assert "No pending" in msg


def test_format_task_list_with_tasks():
    """Compact one-line format: emoji + *num* + title per line."""
    tasks = [
        {"task_number": 1, "title": "Follow up on UPI", "source_chat": "UPI Group",
         "urgency": "critical"},
        {"task_number": 2, "title": "Review product docs", "source_chat": "Product Group",
         "urgency": "medium"},
    ]
    msg = task_extractor.format_task_list(tasks, session_id="ABC123")
    assert "*1*" in msg
    assert "Follow up on UPI" in msg
    assert "🔴" in msg
    assert "🟡" in msg
    assert "ABC123" in msg
    assert "archive 1,3" in msg


def test_format_task_list_compact_single_line():
    """Each task is a single line — no second indented line."""
    tasks = [
        {"task_number": 1, "title": "Short", "source_chat": "G", "urgency": "high"},
    ]
    msg = task_extractor.format_task_list(tasks)
    lines = msg.split("\n")
    task_line = [l for l in lines if "*1*" in l]
    assert len(task_line) >= 1
    line = task_line[0]
    assert "Short" in line
    assert "🟠" in line


def test_extract_option_a():
    msg = """🎯 *Task 5 — Test Task*

━━━━━━━━━━━

*A.* First response option

*B.* Second response

━━━━━━━━━━━

Reply *send 5 A/B/C/D*"""
    result = handlers._extract_option(msg, "A")
    assert result is not None
    assert "First response option" in result


def test_extract_option_d():
    msg = """🎯 *Task 23 — Investigate*

━━━━━━━━━━━

*A.* Option A
*B.* Option B
*C.* Option C
*D.* Option D"""
    result = handlers._extract_option(msg, "D")
    assert result is not None
    assert "Option D" in result


def test_extract_option_invalid():
    result = handlers._extract_option("*A.* Only one", "Z")
    assert result is None


def test_extract_option_case_insensitive():
    result = handlers._extract_option("*A.* Hello\n*B.* World", "a")
    assert "Hello" in result


if __name__ == "__main__":
    test_format_task_list_empty()
    test_format_task_list_with_tasks()
    test_format_task_list_compact_single_line()
    test_extract_option_a()
    test_extract_option_d()
    test_extract_option_invalid()
    test_extract_option_case_insensitive()
    print("✅ All handler tests passed!")
