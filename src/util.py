"""Small helpers: local time, deadline parsing, formatting."""

from __future__ import annotations

import datetime as dt
import re
import time

_DATE_FORMATS = (
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%Y-%m-%d",
    "%d %b %Y %I:%M %p",
    "%d %B %Y %I:%M %p",
)


def enable_utf8() -> None:
    """Windows consoles still default to cp1252 - emoji would raise UnicodeEncodeError."""
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def local_tz() -> dt.tzinfo:
    """The machine's current local timezone (no tzdata package required)."""
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def now_ts() -> int:
    return int(time.time())


def parse_deadline(value: str | None) -> tuple[int | None, bool]:
    """Parse an LLM-supplied deadline into epoch seconds.

    Returns (epoch_seconds, date_only). Date-only deadlines are pushed to
    23:59 local time so they stay "upcoming" for the whole day.
    """
    if not value:
        return None, False
    raw = value.strip()
    if raw.lower() in {"none", "null", "n/a", "-", "unknown"}:
        return None, False

    date_only = "T" not in raw and " " not in raw
    parsed: dt.datetime | None = None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for fmt in _DATE_FORMATS:
            try:
                parsed = dt.datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        return None, False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz())
    if date_only:
        parsed = parsed.replace(hour=23, minute=59, second=0, microsecond=0)
    return int(parsed.timestamp()), date_only


def fmt_dt(ts: int | None) -> str:
    if not ts:
        return "no date"
    return dt.datetime.fromtimestamp(ts, local_tz()).strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def fmt_day(ts: int | None) -> str:
    if not ts:
        return "no date"
    return dt.datetime.fromtimestamp(ts, local_tz()).strftime("%a %d %b")


def relative(ts: int | None) -> str:
    """Human phrasing of the distance between now and a deadline."""
    if not ts:
        return ""
    diff = ts - now_ts()
    past = diff < 0
    diff = abs(diff)
    if diff < 90:
        phrase = "less than a minute"
    elif diff < 5400:
        phrase = f"{int(diff // 60)} min"
    elif diff < 172800:
        phrase = f"{int(diff // 3600)} h"
    else:
        phrase = f"{int(diff // 86400)} d"
    if diff < 90:
        return "now" if not past else "now (overdue)"
    return (f"in {phrase}" if not past else f"{phrase} overdue")


def days_until(ts: int | None) -> float:
    if not ts:
        return 9999.0
    return (ts - now_ts()) / 86400.0


def slug(text: str, limit: int = 60) -> str:
    """Normalised key-safe form of a title, for de-duplication."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    return cleaned[:limit].strip()


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
