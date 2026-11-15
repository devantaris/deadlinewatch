"""Telegram bot: lets you manage deadlines by replying, without opening a browser."""

from __future__ import annotations

import shlex

from . import notify, util

HELP = """<b>DeadlineWatch</b>

<b>View</b>
/today - due within 24 hours (and overdue)
/week - due within 7 days
/all - everything still open
/briefing - resend the daily briefing
/stats - counts

<b>Manage</b>
/done &lt;id&gt; - mark an item as done
/snooze &lt;id&gt; &lt;hours&gt; - hide an item for N hours
/undo &lt;id&gt; - bring an item back

<b>Setup</b>
/id - show this chat's id
/help - this message

Example: <code>/done 12</code>"""

WELCOME = "👋 <b>DeadlineWatch is live.</b>\n\nI'll nudge you before deadlines and keep a list here.\n" + HELP


class Bot:
    """Polls Telegram for commands and applies them to the store."""

    def __init__(self, settings, store, telegram, log=print, persist_chat_id: bool = True):
        self.settings = settings
        self.store = store
        self.telegram = telegram
        self.log = log
        self.persist_chat_id = persist_chat_id
        self.offset = int(self.store.get_meta("telegram_offset", "0") or 0)

    # ------------------------------------------------------------------
    def poll_once(self) -> int:
        """Fetch pending updates and handle them. Returns how many were handled."""
        try:
            updates = self.telegram.get_updates(self.offset)
        except Exception as exc:
            self.log(f"[bot] poll failed: {exc}")
            return 0

        handled = 0
        for update in updates:
            self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
            try:
                if self.handle_update(update):
                    handled += 1
            except Exception as exc:
                self.log(f"[bot] command failed: {exc!r}")
        if updates:
            self.store.set_meta("telegram_offset", str(self.offset))
        return handled

    def run_forever(self, interval: int = 4) -> None:
        self.log(f"[bot] listening for commands (offset {self.offset})")
        while True:
            self.poll_once()
            util_sleep(interval)

    # ------------------------------------------------------------------
    def handle_update(self, update: dict) -> bool:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if chat_id is None or not text:
            return False

        # Learn the chat id the first time the user writes to the bot.
        if not self.telegram.chat_id:
            self.telegram.chat_id = str(chat_id)
            self.store.set_meta("telegram_chat_id", str(chat_id))
            if self.persist_chat_id:
                from . import config

                config.persist_chat_id(str(chat_id))
            self.log(f"[bot] learned chat id {chat_id}")

        reply = self.handle_command(str(chat_id), text)
        if reply:
            try:
                self.telegram.send(reply)
            except Exception as exc:
                self.log(f"[bot] reply failed: {exc}")
        return True

    def _save_chat_id_to_env(self, chat_id: str) -> None:
        """Persist the discovered chat id into .env so it survives restarts."""
        from . import config

        config.persist_chat_id(chat_id)

    # ------------------------------------------------------------------
    def handle_command(self, chat_id: str, text: str) -> str:
        try:
            parts = shlex.split(text.replace("\n", " "))
        except ValueError:
            parts = text.split()
        if not parts:
            return ""

        command = parts[0].lstrip("/").split("@")[0].lower()
        args = parts[1:]

        if command in ("start", "help", "h"):
            return WELCOME
        if command == "id":
            return f"Chat id: <code>{chat_id}</code>"
        if command in ("today", "todayonly"):
            return self._render_groups({"overdue": self._rows_overdue(), "due_soon": self._rows_soon()}, "Due within 24 hours")
        if command in ("week", "upcoming"):
            return self._render_groups(self._groups(week_only=True), "Due this week")
        if command in ("all", "list"):
            return self._render_groups(self._groups(week_only=False), "Everything open")
        if command in ("briefing", "digest"):
            self._send_briefing()
            return ""
        if command == "stats":
            stats = self.store.stats()
            return (
                f"📊 <b>Stats</b>\n"
                f"emails scanned: {stats['emails_seen']}\n"
                f"items tracked: {stats['total']} ({stats['open']} open, {stats['done']} done)"
            )
        if command == "done":
            return self._done(args)
        if command == "snooze":
            return self._snooze(args)
        if command in ("undo", "reopen"):
            return self._undo(args)

        return f"Unknown command <code>/{command}</code>\n\n{HELP}"

    # ------------------------------------------------------------------
    def _rows_soon(self) -> list:
        return list(self.store.due_soon(self.settings.urgent_window_hours))

    def _rows_overdue(self) -> list:
        overdue = []
        for row in self.store.open_items(limit=300):
            deadline = row["deadline_ts"]
            if deadline and util.days_until(deadline) < 0:
                overdue.append(row)
        return overdue

    def _groups(self, week_only: bool) -> dict[str, list]:
        groups: dict[str, list] = {"overdue": [], "due_soon": [], "week": [], "later": []}
        for row in self.store.open_items(limit=300):
            if row["kind"] == "info" and not row["deadline_ts"]:
                continue
            deadline = row["deadline_ts"]
            if deadline is None:
                groups["later"].append(row)
                continue
            days = util.days_until(deadline)
            if days < -7:
                continue
            if days < 0:
                groups["overdue"].append(row)
            elif days <= self.settings.urgent_window_hours / 24:
                groups["due_soon"].append(row)
            elif days <= 7:
                groups["week"].append(row)
            elif not week_only:
                groups["later"].append(row)
        if week_only:
            groups["later"] = []
        else:
            groups["later"] = groups["later"][:8]
        return groups

    def _render_groups(self, groups: dict[str, list], heading: str) -> str:
        total = sum(len(v) for v in groups.values())
        if not total:
            return f"✅ <b>{heading}</b>\n\nNothing here. Enjoy the free time."
        sections = [f"🗓 <b>{heading}</b>"]
        order = [
            ("overdue", "🚨 <b>Overdue</b>"),
            ("due_soon", "🔥 <b>Due within 24 hours</b>"),
            ("week", "📅 <b>Due this week</b>"),
            ("later", "🗓 <b>Later</b>"),
        ]
        for key, title in order:
            rows = groups.get(key) or []
            if not rows:
                continue
            sections.append("")
            sections.append(title)
            for row in rows:
                sections.append(notify.format_item(row))
        return "\n".join(sections)

    def _send_briefing(self) -> None:
        from .pipeline import Pipeline

        pipeline = Pipeline(self.settings, self.store, None, None, self.telegram, log=self.log)
        pipeline.send_digest(force=True)

    # -- mutating commands ---------------------------------------------
    def _done(self, args: list[str]) -> str:
        if not args:
            return "Usage: <code>/done &lt;id&gt;</code>"
        try:
            item_id = int(args[0])
        except ValueError:
            return "That id is not a number."
        row = self.store.get_item(item_id)
        if not row:
            return f"No item with id {item_id}."
        self.store.set_status(item_id, "done")
        return f"✅ Done: <b>{notify.esc(row['title'])}</b>"

    def _snooze(self, args: list[str]) -> str:
        if len(args) < 2:
            return "Usage: <code>/snooze &lt;id&gt; &lt;hours&gt;</code>"
        try:
            item_id = int(args[0])
            hours = float(args[1])
        except ValueError:
            return "Usage: <code>/snooze &lt;id&gt; &lt;hours&gt;</code>"
        row = self.store.get_item(item_id)
        if not row:
            return f"No item with id {item_id}."
        until = util.now_ts() + int(hours * 3600)
        self.store.snooze(item_id, until)
        return f"😴 Snoozed <b>{notify.esc(row['title'])}</b> until {util.fmt_dt(until)}"

    def _undo(self, args: list[str]) -> str:
        if not args:
            return "Usage: <code>/undo &lt;id&gt;</code>"
        try:
            item_id = int(args[0])
        except ValueError:
            return "That id is not a number."
        row = self.store.get_item(item_id)
        if not row:
            return f"No item with id {item_id}."
        self.store.set_status(item_id, "open")
        self.store.clear_snooze(item_id)
        return f"↩️ Back on the list: <b>{notify.esc(row['title'])}</b>"


def util_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
