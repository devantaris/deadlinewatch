"""Command line entry point.

    python -m src.main setup          create .env and print the setup steps
    python -m src.main check          verify every credential and connection
    python -m src.main run            one scan pass (for Task Scheduler)
    python -m src.main watch          stay resident and poll forever
    python -m src.main digest         send the daily briefing now
    python -m src.main list           show tracked items
    python -m src.main done <id>      mark an item as done
    python -m src.main snooze <id> 5  hide an item for 5 hours
    python -m src.main stats          show counters
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
import threading
import time
from pathlib import Path

from . import bot as bot_mod
from . import config, dashboard, notify, util
from .classifier import classify_batch
from .gmail_client import GmailClient, GmailError
from .llm import LLMClient, LLMError
from .notify import Telegram, TelegramError
from .pipeline import Pipeline
from .store import Store

ROOT = config.ROOT


def log(message: str) -> None:
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    line = f"{stamp} {message}"
    try:
        if sys.stdout is not None:  # sys.stdout is None under pythonw.exe
            print(line, flush=True)
    except Exception:
        pass
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_setup(_args) -> int:
    env_path = ROOT / ".env"
    example = ROOT / ".env.example"
    if not env_path.exists():
        shutil.copyfile(example, env_path)
        log(f"created {env_path}")
    else:
        log(f"{env_path} already exists, leaving it alone")

    print(
        """
Next steps
----------
1. Gmail   : turn on 2-Step Verification, then create an App Password at
             https://myaccount.google.com/apppasswords  -> paste into GMAIL_APP_PASSWORD
2. Telegram: message @BotFather -> /newbot -> paste the token into TELEGRAM_BOT_TOKEN
             then send any message to your bot and run:  python -m src.main test-telegram
3. LLM     : paste your API key into LLM_API_KEY (any OpenAI-compatible endpoint works)
4. Verify  : python -m src.main check
"""
    )
    return 0


def cmd_check(_args) -> int:
    settings = config.load_settings()
    results: list[tuple[str, str]] = []  # (status, detail)

    # -- Gmail ----------------------------------------------------------
    if not settings.gmail_user or not settings.gmail_app_password:
        results.append(("SKIP", "Gmail - GMAIL_USER / GMAIL_APP_PASSWORD not set"))
    else:
        try:
            with GmailClient(settings.gmail_user, settings.gmail_app_password, settings.imap_host, settings.imap_port) as gmail:
                gmail.conn.select("INBOX")
                typ, data = gmail.conn.uid("SEARCH", None, "ALL")
                total = len((data[0] or b"").split()) if typ == "OK" else 0
                typ, unseen = gmail.conn.uid("SEARCH", None, "UNSEEN")
                unread = len((unseen[0] or b"").split()) if typ == "OK" else 0
            results.append(("OK", f"Gmail - {settings.gmail_user}, {total} messages, {unread} unread"))
        except GmailError as exc:
            results.append(("FAIL", f"Gmail - {exc}"))

    # -- LLM ------------------------------------------------------------
    if not settings.llm_api_key:
        results.append(("SKIP", "LLM - LLM_API_KEY not set"))
    else:
        try:
            llm = LLMClient(
                settings.llm_api_key,
                settings.llm_base_url,
                settings.llm_model,
                tpm_limit=settings.llm_tpm_limit,
                max_tokens=settings.llm_max_tokens,
            )
            probe = classify_batch(
                llm,
                [
                    {
                        "uid": 1,
                        "received_ts": util.now_ts(),
                        "sender": "Test",
                        "sender_email": "test@example.com",
                        "subject": "Assignment due",
                        "body": "Your assignment is due on 20 November 2026 at 11:59 PM.",
                        "links": [],
                    }
                ],
            )
            if probe:
                deadline = util.fmt_dt(probe[0]["deadline_ts"])
                results.append(
                    ("OK", f"LLM - {settings.llm_model} extracted '{probe[0]['title']}' (due {deadline})")
                )
            else:
                results.append(("WARN", "LLM - model replied but extracted no items"))
        except LLMError as exc:
            results.append(("FAIL", f"LLM - {exc}"))

    # -- Telegram -------------------------------------------------------
    if not settings.telegram_bot_token:
        results.append(("SKIP", "Telegram - TELEGRAM_BOT_TOKEN not set"))
    elif not config.is_valid_chat_id(settings.telegram_chat_id):
        results.append(
            (
                "PENDING",
                "Telegram - bot token is valid, but no chat id yet.\n"
                "         Message your bot once in Telegram, then run: python -m src.main test-telegram",
            )
        )
    else:
        try:
            telegram = Telegram(settings.telegram_bot_token, settings.telegram_chat_id)
            telegram.send("✅ DeadlineWatch is wired up correctly.")
            results.append(("OK", f"Telegram - test message sent to chat {settings.telegram_chat_id}"))
        except TelegramError as exc:
            results.append(
                ("FAIL", f"Telegram - {exc}\n         If it says 'chat not found', message your bot once and run: python -m src.main test-telegram")
            )

    # -- report ---------------------------------------------------------
    width = 78
    print("=" * width)
    failed = 0
    for status, detail in results:
        print(f"  [{status:<7}] {detail}")
        if status == "FAIL":
            failed += 1
    print("=" * width)

    if failed:
        print("\nSome checks failed - see the details above.")
        return 1
    if any(status == "PENDING" for status, _ in results):
        print("\nEverything is ready except the Telegram chat id (one message to your bot).")
        return 0
    print("\nAll good. Run:  python -m src.main watch --dashboard")
    return 0


def cmd_test_telegram(_args) -> int:
    settings = config.load_settings()
    if not settings.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is not set in .env")
        return 1

    telegram = Telegram(settings.telegram_bot_token, settings.telegram_chat_id)

    # 1. If a usable chat id is configured, try it first.
    if config.is_valid_chat_id(settings.telegram_chat_id):
        try:
            telegram.send("✅ DeadlineWatch test message. You're all set.")
            print(f"Sent a test message to chat {settings.telegram_chat_id}")
            return 0
        except TelegramError as exc:
            print(f"Configured chat id failed: {exc}")

    # 2. Otherwise discover it from the messages sent to the bot.
    print("Looking for a chat id in the bot's messages...")
    try:
        chat_ids = telegram.known_chat_ids()
    except TelegramError as exc:
        print(f"FAILED: {exc}")
        return 1

    if not chat_ids:
        print(
            "No chat id found yet.\n"
            "  1. Open Telegram and message your bot (e.g. @DevanshAutomationBot) - anything works.\n"
            "  2. Run this command again - it will detect the id and save it to .env."
        )
        return 1

    chat_id = chat_ids[0]
    telegram.chat_id = chat_id
    try:
        telegram.send("✅ DeadlineWatch is connected. Use /help to see what I can do.")
    except TelegramError as exc:
        print(f"FAILED to send to {chat_id}: {exc}")
        return 1

    config.persist_chat_id(chat_id)
    print(f"Sent a test message to chat {chat_id} and saved it to .env")
    return 0


def _build(settings):
    store = Store(settings.db_path)
    gmail = GmailClient(settings.gmail_user, settings.gmail_app_password, settings.imap_host, settings.imap_port)
    llm = LLMClient(
        settings.llm_api_key,
        settings.llm_base_url,
        settings.llm_model,
        tpm_limit=settings.llm_tpm_limit,
        max_tokens=settings.llm_max_tokens,
    )
    telegram = Telegram(settings.telegram_bot_token, settings.telegram_chat_id)
    pipeline = Pipeline(settings, store, gmail, llm, telegram, log=log)
    return store, gmail, pipeline


def cmd_run(_args) -> int:
    settings = config.load_settings()
    missing = config.missing_settings(settings)
    if missing:
        print("Missing configuration: " + ", ".join(missing))
        return 1
    store, gmail, pipeline = _build(settings)
    try:
        with gmail:
            pipeline.run_once()
        pipeline.maybe_send_digest()
    except GmailError as exc:
        log(f"[error] {exc}")
        return 1
    except LLMError as exc:
        log(f"[error] {exc}")
        return 1
    except TelegramError as exc:
        log(f"[error] telegram not ready: {exc}")
    finally:
        store.close()
    return 0


def cmd_watch(args) -> int:
    settings = config.load_settings()
    missing = config.missing_settings(settings)
    if missing:
        print("Missing configuration: " + ", ".join(missing))
        return 1
    log(f"[boot] DeadlineWatch v1 - polling every {settings.poll_interval_minutes} min")
    store, gmail, pipeline = _build(settings)

    optional = config.missing_optional(settings)
    if optional:
        log("[boot] not ready: " + ", ".join(optional) + " - scanning works, alerts will not")
    if settings.telegram_bot_token:
        listener = bot_mod.Bot(settings, store, pipeline.telegram, log=log)
        threading.Thread(target=listener.run_forever, daemon=True).start()
    else:
        log("[boot] no Telegram token - command listener disabled")

    if getattr(args, "dashboard", False):
        dashboard.serve_in_background(
            store, settings.dashboard_host, settings.dashboard_port, log=log
        )

    try:
        with gmail:
            while True:
                try:
                    pipeline.run_once()
                    pipeline.maybe_send_digest()
                except (GmailError, LLMError, TelegramError) as exc:
                    log(f"[error] {exc} (will retry next cycle)")
                except Exception as exc:  # never let one bad cycle kill the daemon
                    log(f"[error] unexpected: {exc!r}")
                # sleep in short slices so the digest fires close to its time
                remaining = settings.poll_interval_minutes * 60
                while remaining > 0:
                    time.sleep(min(30, remaining))
                    remaining -= 30
    except KeyboardInterrupt:
        log("[boot] stopped")
    finally:
        store.close()
    return 0


def cmd_bot(_args) -> int:
    """Run only the Telegram command listener (in another terminal)."""
    settings = config.load_settings()
    if not settings.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is not set in .env")
        return 1
    store = Store(settings.db_path)
    telegram = Telegram(settings.telegram_bot_token, settings.telegram_chat_id)
    listener = bot_mod.Bot(settings, store, telegram, log=log)
    log("[bot] listening for commands - /help for help")
    try:
        while True:
            listener.poll_once()
            time.sleep(settings.bot_poll_seconds)
    except KeyboardInterrupt:
        log("[bot] stopped")
    finally:
        store.close()
    return 0


def cmd_dashboard(args) -> int:
    settings = config.load_settings()
    store = Store(settings.db_path)
    host = getattr(args, "host", None) or settings.dashboard_host
    port = getattr(args, "port", None) or settings.dashboard_port
    httpd = dashboard.serve(store, host, port)
    print(f"DeadlineWatch dashboard -> http://{host}:{port}")
    print("Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
        store.close()
    return 0


def cmd_digest(_args) -> int:
    settings = config.load_settings()
    store, _gmail, pipeline = _build(settings)
    try:
        pipeline.send_digest(force=True)
    finally:
        store.close()
    return 0


def cmd_list(_args) -> int:
    settings = config.load_settings()
    store = Store(settings.db_path)
    try:
        rows = store.open_items(limit=100)
        if not rows:
            print("Nothing tracked yet.")
            return 0
        for row in rows:
            marker = " " if row["status"] == "open" else "x"
            deadline = util.fmt_dt(row["deadline_ts"]) if row["deadline_ts"] else "no date"
            print(f"[{row['id']:>3}] {marker} {row['title']}  |  {deadline}  |  {row['source_label'] or ''}")
    finally:
        store.close()
    return 0


def cmd_done(_args) -> int:
    settings = config.load_settings()
    store = Store(settings.db_path)
    try:
        ok = store.set_status(args.id, "done")
        print("marked as done" if ok else f"no item with id {args.id}")
    finally:
        store.close()
    return 0


def cmd_snooze(_args) -> int:
    settings = config.load_settings()
    store = Store(settings.db_path)
    try:
        until = util.now_ts() + int(args.hours * 3600)
        ok = store.snooze(args.id, until)
        print(f"snoozed until {util.fmt_dt(until)}" if ok else f"no item with id {args.id}")
    finally:
        store.close()
    return 0


def cmd_folders(_args) -> int:
    """List the mailboxes/labels the tool can scan."""
    settings = config.load_settings()
    try:
        with GmailClient(
            settings.gmail_user, settings.gmail_app_password, settings.imap_host, settings.imap_port
        ) as gmail:
            folders = gmail.list_folders()
    except GmailError as exc:
        print(f"FAILED: {exc}")
        return 1
    print("Mailboxes and labels you can scan (put them in IMAP_FOLDERS):\n")
    for name in folders:
        marker = " <- currently scanned" if name in settings.imap_folders else ""
        print(f"  {name}{marker}")
    return 0


def cmd_dedupe(_args) -> int:
    """Fold near-duplicate items into one."""
    settings = config.load_settings()
    store = Store(settings.db_path)
    try:
        merged = store.merge_similar_items()
        print(f"merged {merged} duplicate item(s)")
        for row in store.open_items(limit=50):
            deadline = util.fmt_dt(row["deadline_ts"]) if row["deadline_ts"] else "no date"
            print(f"[{row['id']:>3}] {row['title']}  |  {deadline}  |  {row['source_label'] or ''}")
    finally:
        store.close()
    return 0


def cmd_stats(_args) -> int:
    settings = config.load_settings()
    store = Store(settings.db_path)
    try:
        stats = store.stats()
        print(f"emails scanned : {stats['emails_seen']}")
        print(f"items tracked  : {stats['total']} ({stats['open']} open, {stats['done']} done)")
    finally:
        store.close()
    return 0


def main(argv=None) -> int:
    util.enable_utf8()
    parser = argparse.ArgumentParser(prog="deadlinewatch", description="Never miss a deadline again.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("setup", help="create .env and show setup steps").set_defaults(func=cmd_setup)
    sub.add_parser("check", help="verify credentials and connections").set_defaults(func=cmd_check)
    sub.add_parser("test-telegram", help="send a test message / detect chat id").set_defaults(func=cmd_test_telegram)
    sub.add_parser("run", help="one scan pass").set_defaults(func=cmd_run)
    watch = sub.add_parser("watch", help="poll continuously")
    watch.add_argument("--dashboard", action="store_true", help="also serve the local dashboard")
    watch.set_defaults(func=cmd_watch)
    sub.add_parser("bot", help="listen for Telegram commands only").set_defaults(func=cmd_bot)
    dash = sub.add_parser("dashboard", help="serve the local dashboard only")
    dash.add_argument("--host", default=None)
    dash.add_argument("--port", type=int, default=None)
    dash.set_defaults(func=cmd_dashboard)
    sub.add_parser("digest", help="send the daily briefing now").set_defaults(func=cmd_digest)
    sub.add_parser("list", help="show tracked items").set_defaults(func=cmd_list)
    sub.add_parser("stats", help="show counters").set_defaults(func=cmd_stats)
    sub.add_parser("dedupe", help="merge near-duplicate items").set_defaults(func=cmd_dedupe)
    sub.add_parser("folders", help="list scannable mailboxes/labels").set_defaults(func=cmd_folders)
    done = sub.add_parser("done", help="mark an item as done")
    done.add_argument("id", type=int)
    done.set_defaults(func=cmd_done)
    snooze = sub.add_parser("snooze", help="hide an item for N hours")
    snooze.add_argument("id", type=int)
    snooze.add_argument("hours", type=float)
    snooze.set_defaults(func=cmd_snooze)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
