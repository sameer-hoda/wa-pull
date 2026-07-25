"""
Database layer — all SQLite queries for wa-pull.

Reuses the same messages.db + whatsapp.db produced by the Go bridge
(wa-slash-commands/bridge).  Provides:
  - get_own_jid()           → the user's phone-based JID (MeChat)
  - get_non_archived_chats()→ list of non-archived chat JIDs + names
  - get_chat_messages()     → formatted messages for a single chat
  - get_all_non_archived_messages() → messages across all non-archived chats
  - get_mechat_messages_since()     → new MeChat messages since a timestamp
"""

import sqlite3
import datetime
from typing import Optional

import config


def _connect():
    conn = sqlite3.connect(config.MESSAGES_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(f"ATTACH DATABASE '{config.WHATSAPP_DB}' AS wa")
    return conn


def get_own_jid() -> str:
    """Return the owner's phone JID, e.g. 919876543210@s.whatsapp.net."""
    conn = _connect()
    row = conn.execute("SELECT jid FROM wa.whatsmeow_device LIMIT 1").fetchone()
    conn.close()
    if not row:
        raise RuntimeError("No device row in whatsmeow_device — is the bridge paired?")
    full_jid = row["jid"]  # e.g. 919876543210:16@s.whatsapp.net
    phone = full_jid.split(":")[0]
    return f"{phone}@s.whatsapp.net"


def get_mechat_chat_jid() -> str:
    """
    Return the JID the bridge actually uses as chat_jid for MeChat messages.
    WhatsApp's newer protocol uses LID-based JIDs. We find ours via the
    whatsmeow_lid_map (maps LID → phone number).
    """
    own_phone = get_own_jid().split("@")[0]
    conn = _connect()
    # Try finding by phone number in lid_map
    row = conn.execute(
        "SELECT lid FROM wa.whatsmeow_lid_map WHERE pn = ?",
        (own_phone,),
    ).fetchone()
    if row:
        # lid looks like "123456789012:16@lid" → strip to "123456789012@lid"
        lid = row["lid"].split(":")[0]
        conn.close()
        return f"{lid}@lid"

    # Fallback: check device table for lid
    row2 = conn.execute(
        "SELECT lid FROM wa.whatsmeow_device WHERE jid LIKE ?",
        (f"{own_phone}%",),
    ).fetchone()
    if row2 and row2["lid"]:
        lid = row2["lid"].split(":")[0]
        conn.close()
        return f"{lid}@lid"

    conn.close()
    # Ultimate fallback: phone JID (works for some bridge versions)
    return get_own_jid()



def get_own_phone() -> str:
    """Return just the phone number, e.g. 919876543210."""
    jid = get_own_jid()
    return jid.split("@")[0]


def _resolve_sender_name(row) -> str:
    """Best-effort sender name from joined whatsmeow contacts."""
    name = (
        row["full_name"]
        or row["push_name"]
        or row["first_name"]
        or row["business_name"]
    )
    if name:
        return name
    jid = row["sender_jid"] or ""
    return jid.split("@")[0] if jid else "Unknown"


def get_non_archived_chats(min_messages: int = 1) -> list[dict]:
    """
    Return all non-archived chats with >= *min_messages* in the lookback window.

    Each dict:  {jid, name, msg_count, last_msg_time}
    """
    conn = _connect()
    threshold = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=config.OKF_LOOKBACK_DAYS)
    ).isoformat()

    rows = conn.execute(
        """
        SELECT ch.jid,
               ch.name,
               COUNT(m.id)   AS cnt,
               MAX(m.timestamp) AS last_msg
        FROM chats ch
        LEFT JOIN messages m
            ON ch.jid = m.chat_jid
           AND m.timestamp >= ?
           AND m.content IS NOT NULL
           AND m.content != ''
        LEFT JOIN wa.whatsmeow_chat_settings cs ON ch.jid = cs.chat_jid
        WHERE (cs.archived IS NULL OR cs.archived = 0)
          AND ch.jid != 'status@broadcast'
          AND (ch.jid LIKE '%@g.us' OR ch.jid LIKE '%@s.whatsapp.net'
               OR ch.jid LIKE '%@lid')
        GROUP BY ch.jid, ch.name
        HAVING cnt >= ?
        ORDER BY cnt DESC
        """,
        (threshold, min_messages),
    ).fetchall()
    conn.close()

    # Exclude MeChat from the chat list — it's the bot's own output channel,
    # not a source of tasks. We identify it via the LID map.
    mechat_jid = ""
    try:
        mechat_jid = get_mechat_chat_jid()
    except Exception:
        pass

    return [
        {
            "jid": r["jid"],
            "name": r["name"] or r["jid"].split("@")[0],
            "msg_count": r["cnt"],
            "last_msg_time": r["last_msg"],
        }
        for r in rows
        if r["jid"] != mechat_jid
    ]


def get_chat_messages(chat_jid: str, days: int = 14, limit: int = 2000) -> list[dict]:
    """
    Return messages for a single chat, oldest-first.
    Each dict: {time, sender, content, is_from_me, chat_name}
    """
    conn = _connect()
    threshold = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=days)
    ).isoformat()

    rows = conn.execute(
        """
        SELECT m.content, m.timestamp, m.is_from_me,
               COALESCE(c.full_name, c.push_name, c.first_name,
                        c.business_name) AS contact_name,
               ms.sender_jid,
               ch.name AS chat_name
        FROM messages m
        LEFT JOIN chats ch ON m.chat_jid = ch.jid
        LEFT JOIN wa.whatsmeow_message_secrets ms
            ON m.id = ms.message_id AND m.chat_jid = ms.chat_jid
        LEFT JOIN wa.whatsmeow_contacts c
            ON ms.sender_jid = c.their_jid
        WHERE m.chat_jid = ?
          AND m.timestamp >= ?
          AND m.content IS NOT NULL
          AND m.content != ''
        ORDER BY m.timestamp ASC
        LIMIT ?
        """,
        (chat_jid, threshold, limit),
    ).fetchall()
    conn.close()

    results = []
    for r in rows:
        try:
            dt = datetime.datetime.fromisoformat(
                r["timestamp"].replace(" ", "T")
            )
        except (ValueError, AttributeError):
            dt = datetime.datetime.now(datetime.timezone.utc)

        sender = "You" if r["is_from_me"] else (
            r["contact_name"]
            or (r["sender_jid"].split("@")[0] if r["sender_jid"] else "Unknown")
        )

        results.append(
            {
                "time": dt,
                "sender": sender,
                "content": r["content"].strip(),
                "is_from_me": bool(r["is_from_me"]),
                "chat_name": r["chat_name"] or chat_jid.split("@")[0],
            }
        )
    return results


def get_all_non_archived_messages(
    days: int = 14, per_chat_limit: int = 500
) -> list[dict]:
    """
    Return messages from *all* non-archived chats within *days*.
    Each dict: {time, sender, content, is_from_me, chat_name, chat_jid}
    """
    chats = get_non_archived_chats(min_messages=1)
    all_msgs: list[dict] = []
    for chat in chats:
        msgs = get_chat_messages(
            chat["jid"], days=days, limit=per_chat_limit
        )
        for m in msgs:
            m["chat_jid"] = chat["jid"]
            all_msgs.append(m)
    # sort by time
    all_msgs.sort(key=lambda m: m["time"])
    return all_msgs


def get_mechat_messages_since(since: Optional[datetime.datetime] = None) -> list[dict]:
    """
    Return messages from the user's MeChat (chat with themselves) newer than
    *since*.  If *since* is None, returns the last 5 messages.

    Each dict: {id, time, content, is_from_me}
    """
    chat_jid = get_mechat_chat_jid()
    conn = _connect()

    if since is not None:
        # DB stores timestamps as "2026-07-23 14:55:48+05:30" in IST.
        # Must format since EXACTLY the same way for SQLite string compare.
        IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        since_ist = since.astimezone(IST)
        since_str = since_ist.strftime("%Y-%m-%d %H:%M:%S+05:30")
        rows = conn.execute(
            """
            SELECT id, timestamp, content, is_from_me
            FROM messages
            WHERE chat_jid = ?
              AND content IS NOT NULL
              AND content != ''
              AND timestamp > ?
            ORDER BY timestamp ASC
            """,
            (chat_jid, since_str),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, timestamp, content, is_from_me
            FROM messages
            WHERE chat_jid = ?
              AND content IS NOT NULL
              AND content != ''
            ORDER BY timestamp DESC
            LIMIT 5
            """,
            (chat_jid,),
        ).fetchall()
        rows = list(reversed(rows))

    conn.close()

    results = []
    for r in rows:
        try:
            dt = datetime.datetime.fromisoformat(
                r["timestamp"].replace(" ", "T")
            )
        except (ValueError, AttributeError):
            dt = datetime.datetime.now(datetime.timezone.utc)
        results.append(
            {
                "id": r["id"],
                "time": dt,
                "content": r["content"].strip(),
                "is_from_me": bool(r["is_from_me"]),
            }
        )
    return results


def get_recent_activity(hours: int = 24, per_chat_limit: int = 60) -> list[dict]:
    """
    Return messages from all non-archived chats within the last *hours*.

    Used to build the "what got done today" and "left on read" summaries.
    Each dict: {time, sender, content, is_from_me, chat_name, chat_jid}
    Sorted oldest-first.
    """
    chats = get_non_archived_chats(min_messages=1)
    threshold_dt = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=hours)
    )
    all_msgs: list[dict] = []
    for chat in chats:
        msgs = get_chat_messages(chat["jid"], days=max(1, hours // 24 + 1),
                                 limit=per_chat_limit)
        for m in msgs:
            # get_chat_messages returns tz-aware datetimes (IST); compare as
            # aware datetimes so the timezone offset is handled correctly.
            try:
                if m["time"] >= threshold_dt:
                    m["chat_jid"] = chat["jid"]
                    all_msgs.append(m)
            except TypeError:
                # naive vs aware fallback: treat as within window
                m["chat_jid"] = chat["jid"]
                all_msgs.append(m)
    all_msgs.sort(key=lambda m: m["time"])
    return all_msgs


def get_chats_active_since(hours: float = 1.0, min_messages: int = 1) -> list[dict]:
    """
    Return non-archived chats that received at least one message in the last
    *hours*.  Used to drive incremental OKF rebuilds at the end-of-hour wrap-up.

    Each dict: {jid, name, msg_count, last_msg_time}
    """
    conn = _connect()
    threshold = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=hours)
    ).isoformat()

    rows = conn.execute(
        """
        SELECT ch.jid,
               ch.name,
               COUNT(m.id)   AS cnt,
               MAX(m.timestamp) AS last_msg
        FROM chats ch
        LEFT JOIN messages m
            ON ch.jid = m.chat_jid
           AND m.timestamp >= ?
           AND m.content IS NOT NULL
           AND m.content != ''
        LEFT JOIN wa.whatsmeow_chat_settings cs ON ch.jid = cs.chat_jid
        WHERE (cs.archived IS NULL OR cs.archived = 0)
          AND ch.jid != 'status@broadcast'
          AND (ch.jid LIKE '%@g.us' OR ch.jid LIKE '%@s.whatsapp.net')
        GROUP BY ch.jid, ch.name
        HAVING cnt >= ?
        ORDER BY cnt DESC
        """,
        (threshold, min_messages),
    ).fetchall()
    conn.close()

    return [
        {
            "jid": r["jid"],
            "name": r["name"] or r["jid"].split("@")[0],
            "msg_count": r["cnt"],
            "last_msg_time": r["last_msg"],
        }
        for r in rows
    ]


def get_message_count(days: int = 14) -> int:
    """Quick count of non-archived messages in the window."""
    conn = _connect()
    threshold = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=days)
    ).isoformat()
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM messages m
        LEFT JOIN wa.whatsmeow_chat_settings cs ON m.chat_jid = cs.chat_jid
        WHERE m.timestamp >= ?
          AND m.content IS NOT NULL
          AND m.content != ''
          AND (cs.archived IS NULL OR cs.archived = 0)
          AND (m.chat_jid LIKE '%@g.us' OR m.chat_jid LIKE '%@s.whatsapp.net')
        """,
        (threshold,),
    ).fetchone()
    conn.close()
    return row["n"] if row else 0


def get_new_messages_across_chats(
    since: datetime.datetime,
    exclude_jid: str = "",
    limit: int = 200,
) -> list[dict]:
    """
    Return new messages from all non-archived chats newer than *since*.
    Used by the real-time monitor to detect triggers across all chats.

    Each dict: {id, time, content, is_from_me, chat_jid, chat_name}
    Sorted oldest-first.

    *exclude_jid* is dropped from results (used to skip MeChat, which the
    main polling loop already handles).
    """
    conn = _connect()
    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    since_ist = since.astimezone(IST)
    # DB stores timestamps as "2026-07-23 14:55:48+05:30" — match the format
    # exactly so SQLite string comparison works (see §5.2 of MASTER_CONTEXT).
    since_str = since_ist.strftime("%Y-%m-%d %H:%M:%S+05:30")

    rows = conn.execute(
        """
        SELECT m.id, m.timestamp, m.content, m.is_from_me,
               ch.jid AS chat_jid, ch.name AS chat_name
        FROM messages m
        JOIN chats ch ON m.chat_jid = ch.jid
        LEFT JOIN wa.whatsmeow_chat_settings cs ON ch.jid = cs.chat_jid
        WHERE m.timestamp > ?
          AND m.content IS NOT NULL
          AND m.content != ''
          AND (cs.archived IS NULL OR cs.archived = 0)
          AND (ch.jid LIKE '%@g.us' OR ch.jid LIKE '%@s.whatsapp.net'
               OR ch.jid LIKE '%@lid')
          AND ch.jid != 'status@broadcast'
          AND (? = '' OR ch.jid != ?)
        ORDER BY m.timestamp ASC
        LIMIT ?
        """,
        (since_str, exclude_jid, exclude_jid, limit),
    ).fetchall()
    conn.close()

    results = []
    for r in rows:
        try:
            dt = datetime.datetime.fromisoformat(
                r["timestamp"].replace(" ", "T")
            )
        except (ValueError, AttributeError):
            dt = datetime.datetime.now(IST)
        results.append(
            {
                "id": r["id"],
                "time": dt,
                "content": r["content"].strip(),
                "is_from_me": bool(r["is_from_me"]),
                "chat_jid": r["chat_jid"],
                "chat_name": r["chat_name"] or r["chat_jid"].split("@")[0],
            }
        )
    return results
