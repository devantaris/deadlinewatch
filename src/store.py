"""SQLite persistence for emails, extracted items and state."""

from __future__ import annotations

import functools
import re
import sqlite3
import threading
from pathlib import Path

from . import util


def synchronized(method):
    """SQLite connections are shared across the bot/dashboard threads."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    uid          INTEGER PRIMARY KEY,
    message_id   TEXT,
    subject      TEXT,
    sender       TEXT,
    sender_email TEXT,
    received_ts  INTEGER,
    snippet      TEXT,
    body         TEXT,
    processed_at INTEGER,
    item_count   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    email_uid           INTEGER,
    dedupe_key          TEXT UNIQUE,
    event_key           TEXT,
    title               TEXT NOT NULL,
    category            TEXT,
    kind                TEXT,
    action              TEXT,
    deadline_ts         INTEGER,
    deadline_text       TEXT,
    deadline_confirmed  INTEGER DEFAULT 0,
    deadline_inferred   INTEGER DEFAULT 0,
    urgency             TEXT,
    importance          INTEGER DEFAULT 3,
    source_label        TEXT,
    source_url          TEXT,
    evidence            TEXT,
    status              TEXT DEFAULT 'open',
    snooze_until        INTEGER,
    alerted_urgent      INTEGER DEFAULT 0,
    alerted_digest      TEXT,
    created_at          INTEGER,
    updated_at          INTEGER
);

CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_deadline ON items(deadline_ts);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _token_set(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(word) > 2}


def _similarity(a: str, b: str) -> float:
    """Jaccard similarity of two titles."""
    left, right = _token_set(a), _token_set(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _same_source(a: str, b: str) -> bool:
    """Two sender names can differ slightly ("LeetCode" vs "LeetCode Support")."""
    left, right = (a or "").strip().lower(), (b or "").strip().lower()
    if not left or not right:
        return True
    if left == right:
        return True
    if left in right or right in left:
        return True
    return _similarity(left, right) >= 0.6


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # -- meta ------------------------------------------------------------
    @synchronized
    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    @synchronized
    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # -- emails ----------------------------------------------------------
    @synchronized
    def last_uid(self, folder: str = "INBOX") -> int:
        return int(self.get_meta(f"last_uid:{folder}", "0") or 0)

    @synchronized
    def set_last_uid(self, uid: int, folder: str = "INBOX") -> None:
        self.set_meta(f"last_uid:{folder}", str(uid))

    @synchronized
    def save_email(self, mail: dict) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO emails
               (uid, message_id, subject, sender, sender_email, received_ts,
                snippet, body, processed_at, item_count)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                mail["uid"],
                mail.get("message_id"),
                mail.get("subject"),
                mail.get("sender"),
                mail.get("sender_email"),
                mail.get("received_ts"),
                mail.get("snippet"),
                mail.get("body"),
                util.now_ts(),
                0,
            ),
        )
        self.conn.commit()

    # -- items -----------------------------------------------------------
    @synchronized
    def upsert_item(self, item: dict) -> tuple[int, bool]:
        """Insert an item, or update the existing one with the same dedupe key.

        Returns (item_id, created_new).
        """
        now = util.now_ts()
        dedupe_key = item["dedupe_key"]
        existing = self.conn.execute(
            "SELECT id, deadline_ts FROM items WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()

        if not existing:
            # The same event often arrives with a slightly different wording
            # ("contest ban ends" vs "contest ban lift"). Merge those instead
            # of showing the user the same thing twice.
            existing = self._find_similar(item)

        if existing:
            item_id = existing["id"]
            old_deadline = existing["deadline_ts"]
            new_deadline = item.get("deadline_ts")
            # keep the earliest known deadline, but upgrade to a confirmed one
            if old_deadline and new_deadline:
                deadline = min(old_deadline, new_deadline)
            else:
                deadline = new_deadline or old_deadline
            self.conn.execute(
                """UPDATE items SET title = ?, category = ?, kind = ?, action = ?,
                   deadline_ts = ?, deadline_text = ?, deadline_confirmed = ?,
                   deadline_inferred = ?, urgency = ?, importance = ?,
                   source_label = ?, source_url = ?, evidence = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    item.get("title"),
                    item.get("category"),
                    item.get("kind"),
                    item.get("action"),
                    deadline,
                    item.get("deadline_text"),
                    1 if item.get("deadline_confirmed") else 0,
                    1 if item.get("deadline_inferred") else 0,
                    item.get("urgency"),
                    item.get("importance"),
                    item.get("source_label"),
                    item.get("source_url"),
                    item.get("evidence"),
                    now,
                    item_id,
                ),
            )
            self.conn.commit()
            return item_id, False

        cursor = self.conn.execute(
            """INSERT INTO items
               (email_uid, dedupe_key, event_key, title, category, kind, action,
                deadline_ts, deadline_text, deadline_confirmed, deadline_inferred,
                urgency, importance, source_label, source_url, evidence,
                status, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?)""",
            (
                item.get("email_uid"),
                dedupe_key,
                item.get("event_key"),
                item.get("title"),
                item.get("category"),
                item.get("kind"),
                item.get("action"),
                item.get("deadline_ts"),
                item.get("deadline_text"),
                1 if item.get("deadline_confirmed") else 0,
                1 if item.get("deadline_inferred") else 0,
                item.get("urgency"),
                item.get("importance", 3),
                item.get("source_label"),
                item.get("source_url"),
                item.get("evidence"),
                now,
                now,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid), True

    @synchronized
    def open_items(self, limit: int = 200) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """SELECT * FROM items
                   WHERE status = 'open'
                     AND (snooze_until IS NULL OR snooze_until <= ?)
                   ORDER BY
                     CASE WHEN deadline_ts IS NULL THEN 1 ELSE 0 END,
                     deadline_ts ASC,
                     importance DESC
                   LIMIT ?""",
                (util.now_ts(), limit),
            )
        )

    @synchronized
    def all_items(self, limit: int = 500) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM items ORDER BY COALESCE(deadline_ts, 9999999999) ASC LIMIT ?",
                (limit,),
            )
        )

    @synchronized
    def due_soon(self, hours: int) -> list[sqlite3.Row]:
        cutoff = util.now_ts() + hours * 3600
        return list(
            self.conn.execute(
                """SELECT * FROM items
                   WHERE status = 'open'
                     AND deadline_ts IS NOT NULL
                     AND deadline_ts <= ?
                     AND deadline_ts >= ?
                     AND (snooze_until IS NULL OR snooze_until <= ?)
                   ORDER BY deadline_ts ASC""",
                (cutoff, util.now_ts() - 86400, util.now_ts()),
            )
        )

    @synchronized
    def unalerted_urgent(self, hours: int) -> list[sqlite3.Row]:
        cutoff = util.now_ts() + hours * 3600
        floor = util.now_ts() - 3 * 86400  # ancient overdue items are not "urgent"
        return list(
            self.conn.execute(
                """SELECT * FROM items
                   WHERE status = 'open'
                     AND alerted_urgent = 0
                     AND deadline_ts IS NOT NULL
                     AND deadline_ts <= ?
                     AND deadline_ts >= ?
                     AND (snooze_until IS NULL OR snooze_until <= ?)
                   ORDER BY deadline_ts ASC""",
                (cutoff, floor, util.now_ts()),
            )
        )

    @synchronized
    def mark_urgent_alerted(self, item_id: int) -> None:
        self.conn.execute("UPDATE items SET alerted_urgent = 1 WHERE id = ?", (item_id,))
        self.conn.commit()

    @synchronized
    def mark_digest_sent(self, item_ids: list[int], digest_date: str) -> None:
        if not item_ids:
            return
        placeholders = ",".join("?" * len(item_ids))
        self.conn.execute(
            f"UPDATE items SET alerted_digest = ? WHERE id IN ({placeholders})",
            [digest_date, *item_ids],
        )
        self.conn.commit()

    @synchronized
    def set_status(self, item_id: int, status: str) -> bool:
        cur = self.conn.execute(
            "UPDATE items SET status = ?, updated_at = ? WHERE id = ?",
            (status, util.now_ts(), item_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    @synchronized
    def snooze(self, item_id: int, until_ts: int) -> bool:
        cur = self.conn.execute(
            "UPDATE items SET snooze_until = ?, updated_at = ? WHERE id = ?",
            (until_ts, util.now_ts(), item_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    @synchronized
    def merge_similar_items(self) -> int:
        """Fold near-duplicate open items into one. Returns how many were merged."""
        rows = list(
            self.conn.execute(
                "SELECT id, title, source_label, deadline_ts FROM items "
                "WHERE status = 'open' AND alerted_urgent = 0 ORDER BY id"
            )
        )
        merged = 0
        for row in rows:
            keeper = self._find_similar(
                {
                    "title": row["title"],
                    "source_label": row["source_label"],
                    "deadline_ts": row["deadline_ts"],
                },
                exclude_id=row["id"],
            )
            if keeper is None:
                continue
            self.conn.execute("DELETE FROM items WHERE id = ?", (row["id"],))
            merged += 1
        self.conn.commit()
        return merged

    @synchronized
    def _find_similar(self, item: dict, exclude_id: int | None = None):
        """An open item from the same sender that is really the same event."""
        source = (item.get("source_label") or "").strip().lower()
        title = item.get("title") or ""
        if not title:
            return None
        deadline = item.get("deadline_ts")
        candidates = self.conn.execute(
            "SELECT id, title, source_label, deadline_ts FROM items WHERE status = 'open'"
        ).fetchall()
        for row in candidates:
            if exclude_id is not None and row["id"] == exclude_id:
                continue
            other_source = (row["source_label"] or "").strip().lower()
            if not _same_source(source, other_source):
                continue
            if _similarity(title, row["title"]) < 0.55:
                continue
            if deadline and row["deadline_ts"]:
                gap = abs(deadline - row["deadline_ts"])
                if gap > 3 * 86400:
                    continue
            return row
        return None

    @synchronized
    def get_item(self, item_id: int):
        return self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()

    @synchronized
    def clear_snooze(self, item_id: int) -> None:
        self.conn.execute(
            "UPDATE items SET snooze_until = NULL, updated_at = ? WHERE id = ?",
            (util.now_ts(), item_id),
        )
        self.conn.commit()

    @synchronized
    def stats(self) -> dict:
        row = self.conn.execute(
            """SELECT
                 COUNT(*) AS total,
                 SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_items,
                 SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done_items
               FROM items"""
        ).fetchone()
        emails = self.conn.execute("SELECT COUNT(*) AS c FROM emails").fetchone()["c"]
        return {
            "emails_seen": emails,
            "total": row["total"] or 0,
            "open": row["open_items"] or 0,
            "done": row["done_items"] or 0,
        }
