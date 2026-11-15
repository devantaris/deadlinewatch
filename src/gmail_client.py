"""Gmail over IMAP. Uses an App Password, never touches the real password."""

from __future__ import annotations

import email
import imaplib
import re
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from . import util

BODY_LIMIT = 2500

_QUOTE_MARKERS = (
    "\n-----original message-----",
    "\n________________________________",
    "\non ",
)


class _HTMLText(HTMLParser):
    """Flatten HTML into readable plain text."""

    SKIP = {"script", "style", "head", "title"}
    BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag in self.BLOCKS and not self.skip_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif tag in self.BLOCKS and not self.skip_depth:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        joined = "".join(self.parts)
        joined = re.sub(r"[ \t]+", " ", joined)
        joined = re.sub(r"\n\s*\n\s*\n+", "\n\n", joined)
        return joined.strip()


class _HTMLLinks(HTMLParser):
    """Collect (href, anchor text) pairs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            self._href = href
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = None
            self._text = []


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return parser.text()


def html_links(html: str) -> list[tuple[str, str]]:
    parser = _HTMLLinks()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return parser.links


def strip_quotes(text: str) -> str:
    """Drop quoted reply chains - they carry stale dates."""
    lowered = text.lower()
    cut = len(text)
    for marker in _QUOTE_MARKERS:
        index = lowered.find(marker)
        if index != -1 and index < cut:
            cut = index
    return text[:cut].strip()


class GmailError(RuntimeError):
    pass


class GmailClient:
    def __init__(
        self,
        user: str,
        app_password: str,
        host: str = "imap.gmail.com",
        port: int = 993,
        log=print,
    ):
        if not user or not app_password:
            raise GmailError("GMAIL_USER and GMAIL_APP_PASSWORD must be set in .env")
        self.user = user
        self.password = app_password
        self.host = host
        self.port = port
        self.log = log
        self.conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "GmailClient":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def connect(self) -> None:
        try:
            self.conn = imaplib.IMAP4_SSL(self.host, self.port)
            self.conn.login(self.user, self.password)
            self.conn.select("INBOX")
        except imaplib.IMAP4.error as exc:
            raise GmailError(
                "IMAP login failed. Check GMAIL_USER and that you are using an "
                "App Password (not your normal Google password)."
            ) from exc
        except OSError as exc:
            raise GmailError(f"cannot reach {self.host}:{self.port} - {exc}") from exc

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    # -- fetching --------------------------------------------------------
    def list_folders(self) -> list[str]:
        """Every mailbox/label this account can see."""
        try:
            typ, data = self.conn.list()
        except Exception:
            return []
        folders: list[str] = []
        for line in data or []:
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            # format: (\HasNoChildren) "/" "Name"
            if "/" not in line:
                continue
            name = line.rsplit('"/"', 1)[-1].strip().strip('"')
            if name and name not in folders:
                folders.append(name)
        return folders

    def fetch_new(
        self,
        since_uid: int,
        max_emails: int = 100,
        seed_limit: int = 25,
        folder: str = "INBOX",
    ) -> list[dict]:
        """Fetch messages newer than since_uid from one folder.

        Never marks mail as read. The very first run (since_uid == 0) only
        looks at the newest `seed_limit` messages so we do not bill you for
        your whole inbox.
        """
        try:
            return self._fetch_new(since_uid, max_emails, seed_limit, folder)
        except (OSError, imaplib.IMAP4.abort) as exc:
            # Gmail closes idle connections. Drop the dead socket, reconnect
            # and try once more instead of failing the whole cycle.
            self.log(f"IMAP connection dropped ({exc.__class__.__name__}) - reconnecting")
            self.close()
            self.connect()
            return self._fetch_new(since_uid, max_emails, seed_limit, folder)

    def _fetch_new(
        self,
        since_uid: int,
        max_emails: int = 100,
        seed_limit: int = 25,
        folder: str = "INBOX",
    ) -> list[dict]:
        if self.conn is None:
            self.connect()
        assert self.conn is not None

        try:
            self.conn.select(folder)
        except imaplib.IMAP4.abort:
            raise  # a dropped socket - the caller reconnects
        except imaplib.IMAP4.error as exc:
            raise GmailError(f"cannot open mailbox '{folder}': {exc}") from exc

        if since_uid <= 0:
            typ, data = self.conn.uid("SEARCH", None, "ALL")
            uids = self._parse_uids(data)
            uids = uids[-seed_limit:]
        else:
            typ, data = self.conn.uid("SEARCH", None, f"UID {since_uid + 1}:*")
            uids = [uid for uid in self._parse_uids(data) if uid > since_uid]
            # oldest first, and no more than max_emails per pass - the rest is
            # picked up on the next pass so nothing is ever skipped.
            uids = uids[:max_emails]

        if typ != "OK" or not uids:
            return []

        messages: list[dict] = []
        for start in range(0, len(uids), 20):
            group = uids[start : start + 20]
            typ, payload = self.conn.uid(
                "FETCH", ",".join(str(u) for u in group), "(BODY.PEEK[])"
            )
            if typ != "OK":
                raise GmailError(f"IMAP fetch failed: {typ}")
            for part in payload or []:
                if not isinstance(part, tuple) or len(part) < 2:
                    continue
                raw = part[1]
                if not isinstance(raw, bytes):
                    continue
                uid = self._uid_from_meta(part[0])
                messages.append(self._parse(raw, uid))
        messages.sort(key=lambda m: m["uid"])
        return messages

    @staticmethod
    def _parse_uids(data) -> list[int]:
        uids: list[int] = []
        for chunk in data or []:
            if isinstance(chunk, bytes):
                chunk = chunk.decode("ascii", "replace")
            for token in str(chunk).split():
                try:
                    uids.append(int(token))
                except ValueError:
                    continue
        return uids

    @staticmethod
    def _uid_from_meta(meta) -> int:
        """The UID lives in the FETCH response metadata, e.g. b'12 (UID 345 BODY[] {99}'."""
        if isinstance(meta, bytes):
            meta = meta.decode("ascii", "replace")
        match = re.search(r"UID\s+(\d+)", str(meta))
        return int(match.group(1)) if match else 0

    def _parse(self, raw: bytes, uid: int = 0) -> dict:
        msg = email.message_from_bytes(raw, policy=policy.default)
        subject = str(msg.get("Subject") or "").strip()
        sender_raw = str(msg.get("From") or "").strip()
        sender_name, sender_email = parseaddr(sender_raw)

        received_ts = util.now_ts()
        try:
            received = parsedate_to_datetime(msg.get("Date"))
            if received is not None:
                received_ts = int(received.timestamp())
        except (TypeError, ValueError):
            pass

        body, links = extract_content(msg)
        body = strip_quotes(body)

        return {
            "uid": uid,
            "message_id": str(msg.get("Message-ID") or ""),
            "subject": subject,
            "sender": sender_name or sender_email or sender_raw,
            "sender_email": sender_email.lower(),
            "received_ts": received_ts,
            "snippet": util.truncate(body.replace("\n", " "), 180),
            "body": body[:BODY_LIMIT],
            "links": links,
        }


def extract_content(msg) -> tuple[str, list[str]]:
    """Return (plain text body, list of links). Prefers text/plain."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    links: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition.lower():
                continue
            content_type = part.get_content_type()
            if content_type not in ("text/plain", "text/html"):
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, "replace")
            except Exception:
                continue
            if content_type == "text/plain":
                plain_parts.append(text)
            else:
                html_parts.append(text)
    else:
        content_type = msg.get_content_type()
        try:
            payload = msg.get_payload(decode=True)
            text = payload.decode(msg.get_content_charset() or "utf-8", "replace") if payload else ""
        except Exception:
            text = ""
        if content_type == "text/html":
            html_parts.append(text)
        else:
            plain_parts.append(text)

    for html in html_parts:
        for href, _anchor in html_links(html):
            if href and href.lower().startswith("http") and href not in links:
                links.append(href)

    if plain_parts:
        body = "\n".join(plain_parts)
    elif html_parts:
        body = html_to_text("\n".join(html_parts))
    else:
        body = ""

    return body.strip(), links[:25]
