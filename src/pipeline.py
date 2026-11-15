"""The engine: fetch mail -> classify -> store -> notify."""

from __future__ import annotations

import datetime as dt

from . import classifier, notify, util


class Pipeline:
    def __init__(self, settings, store, gmail, llm, telegram, log=print):
        self.settings = settings
        self.store = store
        self.gmail = gmail
        self.llm = llm
        self.telegram = telegram
        self.log = log

    # ------------------------------------------------------------------
    # Main pass
    # ------------------------------------------------------------------
    def run_once(self) -> dict:
        total_new = 0
        total_items = 0
        total_updated = 0
        total_alerts = 0
        total_failed = 0

        for folder in self.settings.imap_folders:
            result = self._scan_folder(folder)
            total_new += result["new_emails"]
            total_items += result["new_items"]
            total_updated += result["updated_items"]
            total_alerts += result["alerts"]
            total_failed += result["failed"]

        return {
            "new_emails": total_new,
            "new_items": total_items,
            "updated_items": total_updated,
            "alerts": total_alerts,
            "failed": total_failed,
        }

    def _scan_folder(self, folder: str) -> dict:
        last_uid = self.store.last_uid(folder)
        self.log(f"[scan] checking '{folder}' for mail newer than uid {last_uid}")

        try:
            mail = self.gmail.fetch_new(last_uid, seed_limit=self.settings.seed_limit, folder=folder)
        except Exception as exc:
            self._note_failure(folder, str(exc))
            raise

        self._clear_failure()

        if not mail:
            self.log(f"[scan] '{folder}': no new mail")
            return {"new_emails": 0, "new_items": 0, "updated_items": 0, "alerts": 0, "failed": 0}

        newest = max(m["uid"] for m in mail)
        self.log(f"[scan] '{folder}': {len(mail)} new email(s) (uid {last_uid + 1}-{newest})")

        for item in mail:
            self.store.save_email(item)

        candidates = classifier.keyword_filter(mail, self.settings.filter_keywords)
        if len(candidates) != len(mail):
            self.log(f"[scan] {len(mail) - len(candidates)} email(s) filtered out by keywords")

        new_items = 0
        updated_items = 0
        failed_uids: set[int] = set()
        batch_size = self.settings.emails_per_batch
        for start in range(0, len(candidates), batch_size):
            batch = candidates[start : start + batch_size]
            self.log(f"[scan] classifying {len(batch)} email(s)...")
            errors: list[str] = []
            for item in classifier.classify_batch(self.llm, batch, errors=errors):
                _, created = self.store.upsert_item(item)
                if created:
                    new_items += 1
                    self.log(f"   + {item['title']}" + (f"  (due {item['deadline_text']})" if item["deadline_text"] else ""))
                else:
                    updated_items += 1
            if errors:
                failed_uids.update(m["uid"] for m in batch)
                self.log(f"[scan] !! {len(batch)} email(s) failed to classify - will retry")

        # Only move the watermark past mail we actually processed. If a batch
        # failed (rate limit, provider hiccup) we stop there so the next cycle
        # picks those emails up again instead of losing them forever.
        new_last = last_uid
        for message in sorted(mail, key=lambda m: m["uid"]):
            if message["uid"] in failed_uids:
                break
            new_last = message["uid"]
        if new_last != last_uid:
            self.store.set_last_uid(new_last, folder)

        alerts = self.send_urgent_alerts()
        self.log(f"[scan] '{folder}' done: {new_items} new, {updated_items} updated, {alerts} alert(s)")
        return {
            "new_emails": len(mail),
            "new_items": new_items,
            "updated_items": updated_items,
            "alerts": alerts,
            "failed": len(failed_uids),
        }

    # ------------------------------------------------------------------
    # Self-monitoring: tell the user when the tool itself is broken
    # ------------------------------------------------------------------
    def _note_failure(self, folder: str, error: str) -> None:
        count = int(self.store.get_meta("consecutive_failures", "0") or 0) + 1
        self.store.set_meta("consecutive_failures", str(count))
        self.log(f"[scan] !! {folder} scan failed ({count} in a row): {error}")
        if count < 3:
            return
        last_alert = int(self.store.get_meta("last_failure_alert", "0") or 0)
        if util.now_ts() - last_alert < 6 * 3600:
            return
        self.store.set_meta("last_failure_alert", str(util.now_ts()))
        try:
            self.telegram.send(
                "⚠️ <b>DeadlineWatch cannot reach your inbox</b>\n"
                f"<code>{notify.esc(error[:200])}</code>\n\n"
                "Your app password may have been revoked. Fix it and run:\n"
                "<code>python -m src.main check</code>"
            )
        except Exception:
            pass

    def _clear_failure(self) -> None:
        if self.store.get_meta("consecutive_failures"):
            self.store.set_meta("consecutive_failures", "0")

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------
    def send_urgent_alerts(self) -> int:
        """Ping Telegram about anything due inside the urgent window.

        Deliberately strict - only genuine deadlines (assessment, form, payment,
        event) that are high urgency or high importance. Opportunities and
        low-priority chatter stay in the daily briefing.
        """
        rows = self.store.unalerted_urgent(self.settings.urgent_window_hours)
        rows = [row for row in rows if self._is_real_deadline(row)]
        if not rows:
            return 0

        sent = 0
        for row in rows[:5]:
            try:
                self.telegram.send(notify.format_urgent(row))
            except Exception as exc:  # network hiccup should not kill the loop
                self.log(f"[alert] failed to send: {exc}")
                continue
            self.store.mark_urgent_alerted(row["id"])
            sent += 1
            self.log(f"[alert] sent: {row['title']}")
        return sent

    @staticmethod
    def _is_real_deadline(row) -> bool:
        """Only interrupt for things you genuinely must do, by a real date."""
        if row["kind"] not in ("deadline", "payment", "event"):
            return False
        if not row["deadline_ts"]:
            return False
        if row["urgency"] == "low" and (row["importance"] or 0) < 4:
            return False
        # a date we had to guess is worth an interruption only if it looks important
        if row["deadline_inferred"] and not row["deadline_confirmed"]:
            return (row["importance"] or 0) >= 4
        return True

    # ------------------------------------------------------------------
    # Digest
    # ------------------------------------------------------------------
    def collect_digest_items(self) -> dict[str, list]:
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
                continue  # stale, more than a week past
            if days < 0:
                groups["overdue"].append(row)
            elif days <= self.settings.urgent_window_hours / 24:
                groups["due_soon"].append(row)
            elif days <= 7:
                groups["week"].append(row)
            else:
                groups["later"].append(row)
        groups["later"] = groups["later"][:8]
        return groups

    def send_digest(self, force: bool = False) -> bool:
        today = dt.datetime.now().astimezone().strftime("%Y-%m-%d")
        if not force and self.store.get_meta("last_digest_date") == today:
            return False

        groups = self.collect_digest_items()
        if not any(groups.values()):
            self.log("[digest] nothing to report")
            self.store.set_meta("last_digest_date", today)
            return False

        heading = dt.datetime.now().astimezone().strftime("%A %d %B")
        message = notify.format_digest(groups, self.store.stats(), heading)
        self.telegram.send(message, notify=False)
        self.store.set_meta("last_digest_date", today)
        self.log(f"[digest] sent ({sum(len(v) for v in groups.values())} items)")
        return True

    def maybe_send_digest(self) -> bool:
        now = dt.datetime.now().astimezone()
        target = now.replace(hour=self.settings.digest_hour, minute=self.settings.digest_minute, second=0, microsecond=0)
        if now >= target and self.store.get_meta("last_digest_date") != now.strftime("%Y-%m-%d"):
            return self.send_digest()
        return False
