"""Telegram notifications and message formatting."""

from __future__ import annotations

import html
import json
import urllib.error
import urllib.request

from . import util

API_ROOT = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, chat_id: str = "", timeout: int = 30):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout

    def _call(self, method: str, payload: dict | None = None) -> dict:
        url = f"{API_ROOT}/bot{self.token}/{method}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if data else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            raise TelegramError(f"Telegram HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise TelegramError(f"cannot reach Telegram: {exc.reason}") from exc

    def send(self, text: str, notify: bool = True) -> bool:
        if not self.chat_id:
            raise TelegramError("TELEGRAM_CHAT_ID is not set")
        result = self._call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": not notify,
            },
        )
        return bool(result.get("ok"))

    def get_updates(self, offset: int = 0) -> list[dict]:
        """Read pending bot updates. offset=0 peeks without confirming them."""
        payload: dict = {"limit": 100, "timeout": 0}
        if offset:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        if not result.get("ok"):
            return []
        return result.get("result", []) or []

    def known_chat_ids(self) -> list[str]:
        """Read pending updates to discover chat ids (run after messaging the bot)."""
        ids = []
        for update in self.get_updates():
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat") or {}
            if chat.get("id") is not None and str(chat["id"]) not in ids:
                ids.append(str(chat["id"]))
        return ids


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

CATEGORY_EMOJI = {
    "college": "🎓",
    "internship": "💼",
    "assessment": "🧠",
    "billing": "💳",
    "personal": "🏠",
    "opportunity": "✨",
    "event": "📅",
    "other": "📌",
}

KIND_EMOJI = {
    "deadline": "⏰",
    "opportunity": "✨",
    "payment": "💳",
    "event": "📅",
    "info": "ℹ️",
}


def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def format_item(row, include_link: bool = True) -> str:
    emoji = KIND_EMOJI.get(row["kind"], "📌")
    cat_emoji = CATEGORY_EMOJI.get(row["category"], "")
    lines = [f"{emoji} <b>{esc(row['title'])}</b>"]

    if row["deadline_ts"]:
        flag = ""
        if row["deadline_inferred"]:
            flag = " (inferred)"
        elif not row["deadline_confirmed"]:
            flag = " (unclear)"
        lines.append(
            f"   🗓 {esc(util.fmt_dt(row['deadline_ts']))} {esc(util.relative(row['deadline_ts']))}{flag}"
        )
    if row["action"]:
        lines.append(f"   ➜ {esc(row['action'])}")
    if include_link and row["source_url"]:
        lines.append(f"   🔗 {esc(row['source_url'])}")
    source = row["source_label"] or ""
    if source:
        lines.append(f"   <i>{cat_emoji} {esc(source)}</i>")
    return "\n".join(lines)


def format_urgent(item_row) -> str:
    header = "⚠️ <b>Due soon</b>"
    return f"{header}\n{format_item(item_row)}"


def format_digest(items_by_group: dict[str, list], stats: dict, heading: str) -> str:
    sections = [f"🗓 <b>{esc(heading)}</b>"]

    order = [
        ("overdue", "🚨 <b>Overdue</b>"),
        ("due_soon", "🔥 <b>Due within 24 hours</b>"),
        ("week", "📅 <b>Due this week</b>"),
        ("later", "🗓 <b>Later / no fixed date</b>"),
    ]
    for key, title in order:
        rows = items_by_group.get(key) or []
        if not rows:
            continue
        sections.append("")
        sections.append(title)
        for row in rows:
            sections.append(format_item(row))

    sections.append("")
    sections.append(
        f"<i>📊 {stats.get('open', 0)} open · {stats.get('emails_seen', 0)} emails scanned</i>"
    )
    return "\n".join(sections)
