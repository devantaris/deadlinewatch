# DeadlineWatch

A personal deadline radar. It watches your Gmail, pulls out anything you actually
have to **do** (assessments, registrations, internship deadlines, bills, campus
notices), and pings you on **Telegram** — instantly when something is due soon,
and once a morning with everything else.

Built for Windows, runs locally, no cloud server, no third-party Python packages.

```
Gmail (IMAP + App Password)
        │  every 5 min
        ▼
   LLM extraction  ──►  what must I do, by when, link to act
        │
        ▼
    SQLite  ──►  Telegram:  ⚠️ due within 24h   (instant)
        │                 🗓 daily briefing 9:00am
        ▼
   local dashboard (Phase 2)
```

---

## 1. Setup (about 10 minutes)

### a) Gmail App Password
1. Turn on **2-Step Verification** for your Google account.
2. Open <https://myaccount.google.com/apppasswords> → name it "DeadlineWatch" → **Create**.
3. Copy the 16-character password. This is *not* your Google password.

### b) Telegram bot
1. Open Telegram → message **@BotFather** → `/newbot` → name it → copy the token.
2. Message your new bot once (say "hi") so it can see your chat.

### c) LLM key
Any OpenAI-compatible endpoint works. Put the key, base URL and model in `.env`:

| Provider | Base URL | Model |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| OpenRouter | `https://openrouter.ai/api/v1` | `anthropic/claude-3.5-sonnet` |
| Groq (free tier) | `https://api.groq.com/openai/v1` | `llama-3.1-8b-instant` |
| Ollama (local, free) | `http://localhost:11434/v1` | `llama3.1` |

### d) Install
```powershell
cd C:\Users\Devansh\Desktop\Projects\tool
python -m src.main setup          # creates .env
notepad .env                      # paste your three credentials
python -m src.main check          # verifies Gmail + Telegram + LLM all work
python -m src.main test-telegram  # auto-detects your chat id, sends a test
```

### e) Run it in the background
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -StartNow
```
Registers a Windows scheduled task that starts at logon and runs hidden. Log file:
`data\deadlinewatch.log`.

To stop it later: `powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1`

---

## 2. Commands

| Command | What it does |
|---|---|
| `python -m src.main watch --dashboard` | run continuously + serve the local dashboard |
| `python -m src.main watch` | run continuously (scanning + Telegram only) |
| `python -m src.main run` | one scan pass, then exit (good for cron-style scheduling) |
| `python -m src.main bot` | listen for Telegram commands only |
| `python -m src.main dashboard` | serve the local dashboard only |
| `python -m src.main digest` | send the daily briefing immediately |
| `python -m src.main list` | everything currently tracked |
| `python -m src.main done 12` | mark item #12 as done |
| `python -m src.main snooze 12 24` | hide item #12 for 24 hours |
| `python -m src.main dedupe` | fold near-duplicate items into one |
| `python -m src.main stats` | counters |

### Telegram commands
Reply to the bot in Telegram and manage everything without opening anything:

```
/today      due within 24 hours (plus anything overdue)
/week       due within 7 days
/all        everything still open
/briefing   resend the daily briefing
/done 12    mark item #12 as done
/snooze 12 48   hide item #12 for 48 hours
/undo 12    bring item #12 back
/stats      counters
/id         show this chat's id
```

The first message you send the bot teaches it your chat id, which is written
back into `.env` automatically.

### Dashboard
`http://127.0.0.1:8787` — grouped by urgency (overdue → due today → this week →
later), with one-click **Done / +1d / +3d**. It binds to localhost only.

---

## 3. How the extraction works

Each batch of new emails is sent to the LLM with a strict JSON schema. The model
returns, per actionable item:

- `title`, `action` — what you must do
- `deadline_iso` — resolved date; **relative wording is resolved against the
  email's received date** ("due tomorrow" in a 3-day-old mail becomes the correct
  past date, not tomorrow)
- `deadline_confirmed` / `deadline_inferred` — so you can see how solid a date is
- `event_key` — a stable id per event, so "reminder: assessment due" doesn't
  become a second item
- `source_url` — the single most useful link (apply / pay / register)
- `category` — `college`, `internship`, `assessment`, `billing`, `personal`,
  `opportunity`, `event`, `other`

Non-actionable mail (newsletters, promos, "your registration is confirmed" with no
pending action) is discarded, so the briefing stays short.

Email bodies are treated as untrusted data — the prompt tells the model to never
follow instructions found inside an email.

### Tuning
In `.env`:
- `URGENT_WINDOW_HOURS=24` — when to fire an instant alert (use 48 if you tend
  to see things late).
- `FILTER_KEYWORDS=assessment,due,deadline` — optional. If set, only mail whose
  subject/body contains one of these words is even sent to the LLM (cheaper, noisier).
- `DIGEST_HOUR=9` — briefing time.
- `EMAILS_PER_BATCH=4` — emails per LLM request. If the provider says a request
  is too large, the tool halves the batch and retries automatically.
- `SEED_LIMIT=25` — how many recent emails the first run scans.
- `LLM_TPM_LIMIT=7500` — tokens-per-minute ceiling the tool self-throttles to.
  Free tiers (Groq: 8000) will rate-limit you otherwise. Set `0` to disable.
- `LLM_MAX_TOKENS=1200` — response cap; keep it modest on free tiers.

### How it stays reliable
Three failure modes bite LLM pipelines, and all three are handled:

| Failure | What the tool does |
|---|---|
| The model invents an `email_uid` (it does, ~30% of calls) | Items are re-attributed by matching the quoted evidence, then by position. Nothing is dropped — a lost item is a missed deadline. |
| The model returns "nothing here" for a batch that clearly has deadlines | If the batch contains deadline-ish signals, each email is re-asked one at a time. |
| The provider rate-limits or rejects an oversized request | Retries with the provider's own wait hint; oversized batches are split in half; failed batches are **not** marked as processed, so the next cycle picks them up instead of losing them. |

Verified against the live model: 20/20 extraction runs correct on deadline
emails, 8/8 correctly silent on newsletters and job-alert noise.

---

## 4. Privacy

Everything stays on your machine: emails are read via IMAP, extracted, and stored
in `data/deadlinewatch.db`. The only outbound calls are to your LLM provider
(email content is sent there for extraction — use a local Ollama model if you
don't want that) and to Telegram (only the extracted items, never the raw email).
Emails are fetched with `BODY.PEEK[]`, so nothing in your inbox is ever marked read.

---

## 5. Roadmap

- [x] Phase 1 — Gmail → LLM → Telegram alerts + daily briefing
- [x] Phase 2 — Telegram as a full inbox (/done, /snooze, /today) + local dashboard
- [ ] Phase 3 — one-off scrapers for the 1–2 portals email can't reach
      (university ERP, a specific job board) via the browser
- [ ] Phase 4 — calendar export (.ics) so deadlines land in your real calendar
- [ ] Phase 5 — weekly recap + "you let 2 things slip" post-mortem

---

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `IMAP login failed` | You used your Google password instead of an App Password. Regenerate it. |
| `cannot reach imap.gmail.com` | Firewall / VPN / corporate proxy blocking port 993. |
| No Telegram messages | Run `python -m src.main test-telegram` — the chat id is detected only after you message the bot. |
| `chat not found` | `TELEGRAM_CHAT_ID` holds a `t.me/...` link instead of a number. Message the bot once, then rerun `test-telegram`. |
| `the LLM rejected the API key (401)` | Wrong key for that endpoint. A Groq key (`gsk_`) needs `LLM_BASE_URL=https://api.groq.com/openai/v1`. |
| `HTTP 403 ... error code: 1010` | The provider blocks Python's default User-Agent. Upgrade the tool or set a browser-like UA. |
| `request is larger than this provider allows (413)` | Lower `EMAILS_PER_BATCH` and `LLM_MAX_TOKENS`. The tool also splits batches automatically. |
| `rate limited by the provider (429)` | Lower `LLM_TPM_LIMIT` or `EMAILS_PER_BATCH`. Groq's free tier allows ~8000 tokens/min. |
| `model 'x' is not available` | Check `GET {LLM_BASE_URL}/models` for the exact model id. |
| Nothing ever appears | Check `data\deadlinewatch.log`. Run `python -m src.main run` in the foreground. |
| Too many/noisy alerts | Raise `URGENT_WINDOW_HOURS`, or set `FILTER_KEYWORDS`. |
| The first scan seemed to skip emails | It did not — failed batches are retried on the next cycle. Watch the log for `will retry`. |
