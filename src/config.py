"""Configuration loading. Everything lives in .env (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "deadlinewatch.db"
LOG_PATH = DATA_DIR / "deadlinewatch.log"


def load_env(path: Path = ENV_PATH) -> None:
    """Load .env into os.environ. Real env vars always win."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _get_int(key: str, default: int) -> int:
    try:
        return int(_get(key, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    gmail_user: str
    gmail_app_password: str
    imap_host: str
    imap_port: int
    imap_folders: list[str]
    telegram_bot_token: str
    telegram_chat_id: str
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_tpm_limit: int
    poll_interval_minutes: int
    digest_hour: int
    digest_minute: int
    urgent_window_hours: int
    emails_per_batch: int
    llm_max_tokens: int
    seed_limit: int
    filter_keywords: list[str]
    timezone_label: str
    dashboard_host: str
    dashboard_port: int
    bot_poll_seconds: int
    db_path: Path
    log_path: Path

    @property
    def urgent_window_seconds(self) -> int:
        return self.urgent_window_hours * 3600


def load_settings() -> Settings:
    load_env()
    keywords = [k.strip().lower() for k in _get("FILTER_KEYWORDS", "").split(",") if k.strip()]
    return Settings(
        gmail_user=_get("GMAIL_USER"),
        gmail_app_password=_get("GMAIL_APP_PASSWORD").replace(" ", ""),
        imap_host=_get("IMAP_HOST", "imap.gmail.com"),
        imap_port=_get_int("IMAP_PORT", 993),
        imap_folders=[
            f.strip()
            for f in _get("IMAP_FOLDERS", "INBOX").split(",")
            if f.strip()
        ] or ["INBOX"],
        telegram_bot_token=_get("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_get("TELEGRAM_CHAT_ID"),
        llm_api_key=_get("LLM_API_KEY"),
        llm_base_url=_get("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_model=_get("LLM_MODEL", "gpt-4o-mini"),
        llm_tpm_limit=_get_int("LLM_TPM_LIMIT", 6000),
        poll_interval_minutes=max(1, _get_int("POLL_INTERVAL_MINUTES", 5)),
        digest_hour=_get_int("DIGEST_HOUR", 9),
        digest_minute=_get_int("DIGEST_MINUTE", 0),
        urgent_window_hours=max(1, _get_int("URGENT_WINDOW_HOURS", 24)),
        emails_per_batch=max(1, _get_int("EMAILS_PER_BATCH", 4)),
        llm_max_tokens=_get_int("LLM_MAX_TOKENS", 1200),
        seed_limit=max(1, _get_int("SEED_LIMIT", 25)),
        filter_keywords=keywords,
        timezone_label=_get("TIMEZONE", "local"),
        dashboard_host=_get("DASHBOARD_HOST", "127.0.0.1"),
        dashboard_port=_get_int("DASHBOARD_PORT", 8787),
        bot_poll_seconds=max(2, _get_int("BOT_POLL_SECONDS", 5)),
        db_path=Path(_get("DB_PATH", str(DB_PATH))),
        log_path=Path(_get("LOG_PATH", str(LOG_PATH))),
    )


def persist_chat_id(chat_id: str) -> bool:
    """Write a discovered Telegram chat id back into .env."""
    path = ENV_PATH
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        updated = []
        for line in lines:
            if line.strip().startswith("TELEGRAM_CHAT_ID="):
                updated.append(f"TELEGRAM_CHAT_ID={chat_id}")
            else:
                updated.append(line)
        path.write_text("\n".join(updated) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def is_valid_chat_id(value: str) -> bool:
    """Telegram chat ids are numeric. A t.me link is not a chat id."""
    value = (value or "").strip()
    return value.lstrip("-").isdigit() and len(value) >= 5


REQUIRED = (
    ("GMAIL_USER", "your Gmail address"),
    ("GMAIL_APP_PASSWORD", "a Google App Password"),
    ("LLM_API_KEY", "an LLM API key"),
)

OPTIONAL = (
    ("TELEGRAM_BOT_TOKEN", "the bot token from @BotFather"),
    ("TELEGRAM_CHAT_ID", "your Telegram chat id"),
)


def missing_settings(settings: Settings) -> list[str]:
    """Human readable list of what still needs to be configured."""
    checks = {
        "GMAIL_USER": settings.gmail_user,
        "GMAIL_APP_PASSWORD": settings.gmail_app_password,
        "LLM_API_KEY": settings.llm_api_key,
    }
    return [f"{key} ({hint})" for key, hint in REQUIRED if not checks[key]]


def missing_optional(settings: Settings) -> list[str]:
    """Things that only affect notifications, not the scanning pipeline."""
    checks = {
        "TELEGRAM_BOT_TOKEN": settings.telegram_bot_token,
        "TELEGRAM_CHAT_ID": settings.telegram_chat_id,
    }
    return [f"{key} ({hint})" for key, hint in OPTIONAL if not checks[key]]
