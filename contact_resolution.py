"""
Contact Resolution — resolve raw WhatsApp LIDs/JIDs to human-readable names.

Uses the same whatsapp.db that the Go bridge produces.  The LLM sometimes
emits raw identifiers like "@123456789012" in task summaries and drafted
responses.  This module ensures they are replaced with actual contact names.

Adapted from sandbox/wa_productivity/contact_resolution.py.
"""

import sqlite3
import re
from typing import Optional

import config
import db as db_module


class ContactResolver:
    """Resolve JIDs, LIDs, and phone numbers to human names."""

    def __init__(self):
        self.db_path = config.WHATSAPP_DB
        self._cache: dict[str, str] = {}

    def resolve(self, identifier: str) -> str:
        """
        Resolve a JID, LID, or phone number to a human-readable name.
        Returns the original identifier if resolution fails.
        """
        if not identifier or not isinstance(identifier, str):
            return identifier

        clean = identifier.replace("@lid", "").strip()

        # Check cache
        if clean in self._cache:
            return self._cache[clean]

        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            cursor = conn.cursor()
            jid = _resolve_to_jid(cursor, clean)

            if not jid:
                conn.close()
                self._cache[clean] = identifier
                return identifier

            cursor.execute(
                """SELECT COALESCE(push_name, full_name, first_name, business_name)
                   FROM whatsmeow_contacts WHERE their_jid = ? LIMIT 1""",
                (jid,),
            )
            row = cursor.fetchone()
            conn.close()

            if row and row[0]:
                name = row[0].strip()
                self._cache[clean] = name
                return name
            else:
                self._cache[clean] = identifier
                return identifier
        except Exception:
            return identifier

    def resolve_text(self, text: str) -> str:
        """
        Scan text for raw LID/JID patterns (@<digits>) and resolve each to a name.
        """
        def _replace(m: re.Match) -> str:
            lid = m.group(1)
            name = self.resolve(lid)
            if name != lid:
                return f"@{name}"
            return m.group(0)

        return re.sub(r"@(\d{10,})", _replace, text)


def _resolve_to_jid(cursor, clean: str) -> Optional[str]:
    """Map a LID or phone number to a full JID."""
    if clean.isdigit():
        # Try LID → phone
        cursor.execute("SELECT pn FROM whatsmeow_lid_map WHERE lid = ?", (clean,))
        row = cursor.fetchone()
        if row and row[0]:
            return f"{row[0]}@s.whatsapp.net"
        # Treat as phone number
        return f"{clean}@s.whatsapp.net"

    if clean.endswith("@s.whatsapp.net") or clean.endswith("@g.us"):
        return clean

    return None


# Module-level singleton
_resolver: Optional[ContactResolver] = None


def resolve_contact(identifier: str) -> str:
    global _resolver
    if _resolver is None:
        _resolver = ContactResolver()
    return _resolver.resolve(identifier)


def resolve_text(raw: str) -> str:
    global _resolver
    if _resolver is None:
        _resolver = ContactResolver()
    return _resolver.resolve_text(raw)
