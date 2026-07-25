"""
Tests for OKF builder and reader.
Run: python3 -m pytest tests/test_okf.py -v
"""

import sys
import os
import tempfile
from pathlib import Path

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import okf_builder
import okf_reader


def test_safe_filename():
    """Filename sanitization works."""
    assert okf_builder._safe_filename("Product Team") == "Product_Team"
    assert okf_builder._safe_filename("Test/Group: 1") == "TestGroup_1"
    assert okf_builder._safe_filename("") == "unknown"


def test_slug_uniqueness():
    """Slugs are unique per chat."""
    slug1 = okf_builder._slug("Test Group", "123@g.us")
    slug2 = okf_builder._slug("Test Group", "456@g.us")
    assert slug1 != slug2


def test_fallback_concept():
    """Fallback concept has valid OKF frontmatter."""
    from datetime import datetime
    msgs = [
        {"time": datetime.now(), "sender": "Alice", "content": "Hello"},
        {"time": datetime.now(), "sender": "Bob", "content": "World"},
    ]
    md = okf_builder._fallback_concept("Test Chat", msgs, "2026-01-01")
    assert md.startswith("---")
    assert "type: WhatsApp Chat" in md
    assert "title: Test Chat" in md
    assert "# Recent Activity" in md
    assert "Alice" in md


def test_index_generation():
    """Index file is generated correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config.OKF_DIR = Path(tmpdir)
        chats = [
            {"jid": "123@g.us", "name": "Group A", "msg_count": 100, "last_msg_time": None},
            {"jid": "456@s.whatsapp.net", "name": "Contact B", "msg_count": 50, "last_msg_time": None},
        ]
        okf_builder._write_index(Path(tmpdir), chats, "2026-01-01")
        index = (Path(tmpdir) / "index.md").read_text()
        assert "Group A" in index
        assert "Contact B" in index
        assert "groups/" in index
        assert "contacts/" in index


def test_log_generation_new():
    """Log file is created from scratch."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config.OKF_DIR = Path(tmpdir)
        okf_builder._write_log(Path(tmpdir), "2026-01-01", 5, [])
        log = (Path(tmpdir) / "log.md").read_text()
        assert "2026-01-01" in log
        assert "5 concepts" in log


def test_log_generation_update():
    """Log file is updated with new date entry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config.OKF_DIR = Path(tmpdir)
        okf_builder._write_log(Path(tmpdir), "2026-01-01", 5, [])
        okf_builder._write_log(Path(tmpdir), "2026-01-02", 7, [])
        log = (Path(tmpdir) / "log.md").read_text()
        assert "2026-01-01" in log
        assert "2026-01-02" in log


def test_reader_empty_bundle():
    """Reader handles missing bundle gracefully."""
    config.OKF_DIR = Path("/nonexistent/path")
    assert okf_reader.read_bundle() == ""
    assert okf_reader.get_index() == ""
    assert okf_reader.read_concept("anything") is None
    assert okf_reader.search_bundle("test") == []


def test_reader_read_bundle():
    """Reader can read a simple bundle."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config.OKF_DIR = Path(tmpdir)
        (Path(tmpdir) / "groups").mkdir()
        (Path(tmpdir) / "groups" / "test.md").write_text(
            "---\ntype: WhatsApp Chat\ntitle: Test\n---\n# Test\nHello"
        )

        content = okf_reader.read_bundle()
        assert "Test" in content
        assert "Hello" in content


def test_reader_search():
    """Search finds matching concepts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config.OKF_DIR = Path(tmpdir)
        (Path(tmpdir) / "groups").mkdir()
        (Path(tmpdir) / "groups" / "upi.md").write_text(
            "---\ntype: WhatsApp Chat\ntitle: UPI\n---\n# UPI Discussion\nPayments"
        )
        (Path(tmpdir) / "groups" / "other.md").write_text(
            "---\ntype: WhatsApp Chat\ntitle: Other\n---\n# Other\nStuff"
        )

        results = okf_reader.search_bundle("UPI")
        assert len(results) == 1
        assert "Payments" in results[0]


if __name__ == "__main__":
    test_safe_filename()
    test_slug_uniqueness()
    test_fallback_concept()
    test_index_generation()
    test_log_generation_new()
    test_log_generation_update()
    test_reader_empty_bundle()
    test_reader_read_bundle()
    test_reader_search()
    print("✅ All OKF tests passed!")
