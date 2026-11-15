"""Offline smoke test - no network, no credentials.

    python -m tests.smoke_test

Exercises the store, deadline parsing and message formatting.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import util  # noqa: E402
from src.notify import format_digest, format_item  # noqa: E402
from src.store import Store  # noqa: E402


def main() -> int:
    util.enable_utf8()
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  ok   {name}")
        else:
            print(f"  FAIL {name} {detail}")
            failures.append(name)

    print("util")
    ts, date_only = util.parse_deadline("2026-11-20")
    check("date-only parses", ts is not None and date_only, f"got {ts}")
    check("date-only lands on 23:59", util.days_until(ts) > 0)
    ts2, _ = util.parse_deadline("2026-11-20T23:59:00")
    check("datetime parses", ts2 is not None)
    check("null deadline -> None", util.parse_deadline(None)[0] is None)
    check("garbage deadline -> None", util.parse_deadline("sometime soon")[0] is None)
    check("slug normalises", util.slug("Paytm Online Assessment!") == "paytm online assessment")

    print("store")
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "test.db")
        item = {
            "email_uid": 42,
            "event_key": "paytm_online_assessment",
            "dedupe_key": "paytm_online_assessment|2026-11-20",
            "title": "Paytm online assessment",
            "category": "assessment",
            "kind": "deadline",
            "action": "Complete the 2 hour assessment",
            "deadline_ts": util.now_ts() + 3600,
            "deadline_text": "closes Nov 20, 11:59 PM",
            "deadline_confirmed": True,
            "deadline_inferred": False,
            "urgency": "high",
            "importance": 5,
            "source_label": "HireMee",
            "source_url": "https://example.com/assess",
            "evidence": "Your assessment link is ready",
        }
        item_id, created = store.upsert_item(item)
        check("insert creates item", created and item_id > 0)
        _, created_again = store.upsert_item(item)
        check("same dedupe key does not duplicate", not created_again)

        due = store.due_soon(24)
        check("due-soon query finds it", len(due) == 1)
        check("unalerted query finds it", len(store.unalerted_urgent(24)) == 1)
        store.mark_urgent_alerted(item_id)
        check("alert flag clears", len(store.unalerted_urgent(24)) == 0)

        reminder = dict(item, title="Reminder: Paytm assessment", deadline_text="tomorrow")
        store.upsert_item(reminder)
        check("reminder merges into same item", len(store.all_items()) == 1)

        # The same event worded differently should not become a second item.
        ban = {
            "email_uid": 43,
            "event_key": "leetcode_contest_ban_ends",
            "dedupe_key": "leetcode_contest_ban_ends|2026-12-07",
            "title": "LeetCode contest ban ends",
            "category": "other",
            "kind": "event",
            "action": "Contest access returns",
            "deadline_ts": util.now_ts() + 20 * 86400,
            "deadline_text": "Dec 07",
            "deadline_confirmed": True,
            "deadline_inferred": False,
            "urgency": "low",
            "importance": 2,
            "source_label": "LeetCode",
            "source_url": None,
            "evidence": "Your ban ends on Dec 7",
        }
        _, created_ban = store.upsert_item(ban)
        check("new event is created", created_ban)
        ban_wording = dict(
            ban,
            event_key="leetcode_contest_ban_lift",
            dedupe_key="leetcode_contest_ban_lift|2026-12-07",
            title="LeetCode contest ban lift",
        )
        _, created_again_ban = store.upsert_item(ban_wording)
        check("same event worded differently merges", not created_again_ban)
        check("no duplicate rows for the same event", len(store.all_items()) == 2)

        unrelated = {
            "email_uid": 44,
            "event_key": "semester_results",
            "dedupe_key": "semester_results|2026-12-01",
            "title": "Semester results published",
            "category": "college",
            "kind": "info",
            "action": "Check your marks",
            "deadline_ts": util.now_ts() + 15 * 86400,
            "deadline_text": "Dec 01",
            "deadline_confirmed": True,
            "deadline_inferred": False,
            "urgency": "low",
            "importance": 2,
            "source_label": "University",
            "source_url": None,
            "evidence": "Results are out",
        }
        _, created_unrelated = store.upsert_item(unrelated)
        check("a genuinely different item is kept", created_unrelated)

        store.set_status(item_id, "done")
        check("status change works", len(store.open_items()) == 2)
        stats = store.stats()
        check("stats count done items", stats["done"] == 1, str(stats))
        store.close()

    print("formatting")
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "test.db")
        store.upsert_item(
            {
                "email_uid": 1,
                "event_key": "fee_payment",
                "dedupe_key": "fee_payment|2026-11-15",
                "title": "Semester fee payment",
                "category": "billing",
                "kind": "payment",
                "action": "Pay via the portal",
                "deadline_ts": util.now_ts() + 7200,
                "deadline_text": "15 Nov",
                "deadline_confirmed": True,
                "deadline_inferred": False,
                "urgency": "high",
                "importance": 4,
                "source_label": "Accounts Dept",
                "source_url": "https://portal.example.com/pay",
                "evidence": "Fee due",
            }
        )
        rows = store.open_items()
        rendered = format_item(rows[0])
        check("item renders a title", "Semester fee payment" in rendered)
        check("item renders the link", "portal.example.com" in rendered)

        html_row = dict(rows[0])
        html_row["title"] = "<script>alert(1)</script>"
        check("html is escaped", "&lt;script&gt;" in format_item(html_row))
        check("raw html never leaks", "<script>" not in format_item(html_row))

        digest = format_digest(
            {"due_soon": rows, "week": [], "later": [], "overdue": []}, store.stats(), "Today"
        )
        check("digest renders", "Semester fee payment" in digest)
        check("digest has a section", "Due within 24 hours" in digest)
        store.close()

    print("classifier")
    from src.classifier import build_batch_prompt, classify_batch  # noqa: E402
    from src.llm import LLMClient  # noqa: E402

    canned = {
        "items": [
            {
                "email_uid": 77,
                "event_key": "paytm_online_assessment",
                "title": "Paytm online assessment",
                "category": "assessment",
                "kind": "deadline",
                "action": "Complete the 2 hour assessment",
                "deadline_iso": "2026-11-20T23:59:00",
                "deadline_text": "closes Nov 20, 11:59 PM",
                "deadline_confirmed": True,
                "deadline_inferred": False,
                "urgency": "high",
                "importance": 5,
                "source_label": "HireMee",
                "source_url": "https://example.com/assess",
                "evidence": "Your assessment link is ready",
            }
        ]
    }

    class FakeLLM(LLMClient):
        def __init__(self, payload):
            super().__init__("key", "http://localhost", "fake-model")
            self.payload = payload
            self.last_system = ""
            self.last_user = ""

        def _post(self, payload, slot=-1):
            self.last_system = payload["messages"][0]["content"]
            self.last_user = payload["messages"][1]["content"]
            return {"choices": [{"message": {"content": self.payload}}]}

    email = {
        "uid": 77,
        "received_ts": util.now_ts(),
        "sender": "HireMee",
        "sender_email": "no-reply@hiremee.in",
        "subject": "Your assessment link is ready",
        "body": "Your assessment link is ready. It closes on Nov 20, 11:59 PM.",
        "links": ["https://example.com/assess"],
    }

    fake = FakeLLM(json.dumps(canned))
    items = classify_batch(fake, [email])
    check("classifier returns one valid item", len(items) == 1, str(items))
    if items:
        item = items[0]
        check("item keeps the title", item["title"] == "Paytm online assessment")
        check("item parses the deadline", item["deadline_ts"] is not None)
        check("item keeps the link", item["source_url"] == "https://example.com/assess")
        check("item has a dedupe key", item["dedupe_key"].startswith("paytm_online_assessment"))
    check("prompt carries the received date", "received:" in fake.last_user)
    check("prompt includes the email body", "assessment link is ready" in fake.last_user)
    check("system prompt guards against injection", "untrusted" in fake.last_system.lower())

    fenced = FakeLLM("```json\n" + json.dumps(canned) + "\n```")
    check("fenced json parses", len(classify_batch(fenced, [email])) == 1)

    prose = FakeLLM('Sure! Here you go:\n' + json.dumps(canned))
    check("json inside prose parses", len(classify_batch(prose, [email])) == 1)

    empty = FakeLLM(json.dumps({"items": []}))
    check("no items -> empty list", classify_batch(empty, [email]) == [])

    print("classifier robustness")
    # The model sometimes hallucinates email_uid. Dropping those items would
    # mean missing a deadline, so they must be recovered instead.
    wrong_uid = {
        "items": [
            {
                "email_uid": 0,
                "event_key": "paytm_online_assessment",
                "title": "Paytm online assessment",
                "kind": "deadline",
                "deadline_iso": "2026-11-20T23:59:00",
                "evidence": "Your assessment link is ready",
            }
        ]
    }
    recovered = classify_batch(FakeLLM(json.dumps(wrong_uid)), [email])
    check("hallucinated uid is recovered", len(recovered) == 1, str(recovered))
    if recovered:
        check("recovered item keeps its uid", recovered[0]["email_uid"] == email["uid"])

    positional = {
        "items": [
            {"email_uid": 0, "title": "First", "kind": "deadline", "deadline_iso": "2026-11-20"},
            {"email_uid": 0, "title": "Second", "kind": "deadline", "deadline_iso": "2026-11-21"},
        ]
    }
    two = [dict(email, uid=11), dict(email, uid=12)]
    mapped = classify_batch(FakeLLM(json.dumps(positional)), two)
    check("positional fallback maps both items", len(mapped) == 2, str(mapped))
    if len(mapped) == 2:
        check("positional fallback keeps order", mapped[0]["email_uid"] == 11 and mapped[1]["email_uid"] == 12)

    # A batch that fails must report the failure so the caller can retry it.
    class BrokenLLM(LLMClient):
        def __init__(self):
            super().__init__("key", "http://localhost", "fake-model")

        def chat_json(self, system, user, retries=4):
            from src.llm import LLMError

            raise LLMError("HTTP 429 rate limited")

    errors: list[str] = []
    result = classify_batch(BrokenLLM(), [email], errors=errors)
    check("failed batch returns no items", result == [])
    check("failed batch reports the error", len(errors) == 1, str(errors))

    # An empty result on an obviously actionable email triggers a per-email retry.
    calls: list[int] = []

    class RetryLLM(LLMClient):
        def __init__(self):
            super().__init__("key", "http://localhost", "fake-model")
            self.n = 0

        def chat_json(self, system, user, retries=4):
            self.n += 1
            calls.append(self.n)
            if self.n == 1:
                return {"items": []}
            return {"items": [{"email_uid": 77, "title": "Retried", "kind": "deadline", "deadline_iso": "2026-11-20"}]}

    retried = classify_batch(RetryLLM(), [email])
    check("empty result on a deadline-ish email is retried", len(retried) == 1, str(retried))

    from src.classifier import looks_actionable  # noqa: E402

    check("deadline email looks actionable", looks_actionable([email]))
    check(
        "newsletter does not look actionable",
        not looks_actionable(
            [
                {
                    "uid": 1,
                    "subject": "What's new",
                    "body": "A summary of what we shipped this week.",
                    "links": [],
                }
            ]
        ),
    )

    print("email parsing")
    from email.message import EmailMessage  # noqa: E402
    from src.gmail_client import extract_content, html_to_text, strip_quotes  # noqa: E402

    raw = EmailMessage()
    raw["Subject"] = "Assessment window closes tonight"
    raw["From"] = "HireMee <no-reply@hiremee.in>"
    raw["Date"] = "Thu, 13 Nov 2026 09:12:00 +0530"
    raw["Message-ID"] = "<abc123@mail.hiremee.in>"
    raw.set_content("Hi,\nYour assessment link is ready.\n\nRegards")
    raw.add_alternative(
        "<html><body><p>Your assessment link is <b>ready</b>.</p>"
        '<p><a href="https://example.com/assess">Start assessment</a></p>'
        "<script>alert('x')</script></body></html>",
        subtype="html",
    )
    body, links = extract_content(raw)
    check("plain text body preferred", "assessment link is ready" in body)
    check("links extracted", "https://example.com/assess" in links, str(links))
    check("html flattened without scripts", "alert(" not in html_to_text("<script>alert(1)</script><p>hi</p>"))
    quoted = "Real content here\n\nOn Thu, 13 Nov 2026, someone wrote:\nold stuff from 2020"
    check("quoted replies stripped", "old stuff" not in strip_quotes(quoted))

    from src.gmail_client import GmailClient  # noqa: E402

    check(
        "uid parsed from fetch metadata",
        GmailClient._uid_from_meta(b"1234 (UID 98765 BODY[] {1234}") == 98765,
    )
    check(
        "uid parse tolerates no uid",
        GmailClient._uid_from_meta(b"1234 (BODY[] {12}") == 0,
    )
    check(
        "uid list parsed from search",
        GmailClient._parse_uids([b"1 2 3", b"4 5"]) == [1, 2, 3, 4, 5],
    )

    print("telegram bot")
    from src.bot import Bot  # noqa: E402
    from src.notify import Telegram  # noqa: E402

    class FakeTelegram(Telegram):
        def __init__(self):
            super().__init__("token", "")
            self.sent: list[str] = []
            self.updates: list[dict] = []

        def get_updates(self, offset: int = 0) -> list[dict]:
            updates, self.updates = self.updates, []
            return updates

        def send(self, text: str, notify: bool = True) -> bool:
            self.sent.append(text)
            return True

    class Settings:
        urgent_window_hours = 24
        telegram_bot_token = "token"
        telegram_chat_id = ""

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "test.db")
        store.upsert_item(
            {
                "email_uid": 5,
                "event_key": "semester_fee",
                "dedupe_key": "semester_fee|2026-11-20",
                "title": "Semester fee",
                "category": "billing",
                "kind": "payment",
                "action": "Pay via portal",
                "deadline_ts": util.now_ts() + 3600,
                "deadline_text": "20 Nov",
                "deadline_confirmed": True,
                "deadline_inferred": False,
                "urgency": "high",
                "importance": 5,
                "source_label": "Accounts",
                "source_url": "https://portal.example.com",
                "evidence": "fee due",
            }
        )
        fake_tg = FakeTelegram()
        bot = Bot(Settings(), store, fake_tg, log=lambda m: None, persist_chat_id=False)
        item_id = store.all_items()[0]["id"]

        reply = bot.handle_command("999", "/done 99999")
        check("done with unknown id is handled", "No item" in reply)

        reply = bot.handle_command("999", f"/done {item_id}")
        check("done marks the item", "Done" in reply and store.get_item(item_id)["status"] == "done")

        reply = bot.handle_command("999", f"/undo {item_id}")
        check("undo reopens the item", store.get_item(item_id)["status"] == "open")

        reply = bot.handle_command("999", "/today")
        check("today renders items", "Semester fee" in reply, reply[:120])

        reply = bot.handle_command("999", f"/snooze {item_id} 48")
        check("snooze sets a timer", store.get_item(item_id)["snooze_until"] is not None)
        reply = bot.handle_command("999", "/today")
        check("snoozed items leave the view", "Semester fee" not in reply)

        reply = bot.handle_command("999", f"/undo {item_id}")
        check("undo clears the snooze", store.get_item(item_id)["snooze_until"] is None)
        check("undo reopens the item", store.get_item(item_id)["status"] == "open")

        reply = bot.handle_command("999", "/help")
        check("help lists commands", "/snooze" in reply)

        reply = bot.handle_command("999", "/nonsense")
        check("unknown command suggests help", "Unknown command" in reply)

        # the bot should learn the chat id from the first message it receives
        fake_tg.updates = [{"update_id": 7, "message": {"chat": {"id": 555}, "text": "/today"}}]
        bot.poll_once()
        check("bot learns its chat id", fake_tg.chat_id == "555")
        check("bot replies to messages", len(fake_tg.sent) >= 1)
        check("offset advances", store.get_meta("telegram_offset") == "8")
        store.close()

    print("dashboard")
    import json as json_mod
    import threading
    import urllib.request

    from src.dashboard import build_state, serve  # noqa: E402

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "test.db")
        store.upsert_item(
            {
                "email_uid": 6,
                "event_key": "oa_round2",
                "dedupe_key": "oa_round2|2026-11-19",
                "title": "OA round 2",
                "category": "assessment",
                "kind": "deadline",
                "action": "Take the test",
                "deadline_ts": util.now_ts() + 7200,
                "deadline_text": "19 Nov",
                "deadline_confirmed": True,
                "deadline_inferred": False,
                "urgency": "high",
                "importance": 4,
                "source_label": "HireMee",
                "source_url": "https://example.com/oa",
                "evidence": "link ready",
            }
        )
        state = build_state(store)
        check("state has groups", "overdue" in state["groups"])
        check("state lists the item", len(state["groups"]["due_soon"]) == 1)

        httpd = serve(store, "127.0.0.1", 0)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as res:
                payload = json_mod.loads(res.read().decode("utf-8"))
            check("api returns state", payload["stats"]["open"] == 1)

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as res:
                html = res.read().decode("utf-8")
            check("index page is served", "DeadlineWatch" in html)

            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/items/{store.all_items()[0]['id']}",
                data=json_mod.dumps({"action": "done"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as res:
                res.read()
            check("api marks an item done", store.all_items()[0]["status"] == "done")
        finally:
            httpd.shutdown()
            httpd.server_close()
        store.close()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
