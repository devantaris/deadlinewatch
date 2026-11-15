"""Local dashboard: a tiny read/write web UI over the SQLite database.

Binds to 127.0.0.1 only - it is not reachable from your network.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import util

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def item_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "action": row["action"],
        "category": row["category"],
        "kind": row["kind"],
        "deadline_ts": row["deadline_ts"],
        "deadline_text": row["deadline_text"],
        "deadline_confirmed": bool(row["deadline_confirmed"]),
        "deadline_inferred": bool(row["deadline_inferred"]),
        "urgency": row["urgency"],
        "importance": row["importance"],
        "source_label": row["source_label"],
        "source_url": row["source_url"],
        "status": row["status"],
        "relative": util.relative(row["deadline_ts"]),
        "deadline_human": util.fmt_dt(row["deadline_ts"]) if row["deadline_ts"] else None,
    }


def build_state(store) -> dict:
    groups: dict[str, list] = {"overdue": [], "due_soon": [], "week": [], "later": [], "done": []}
    for row in store.open_items(limit=300):
        deadline = row["deadline_ts"]
        if deadline is None:
            groups["later"].append(row)
            continue
        days = util.days_until(deadline)
        if days < -7:
            continue
        if days < 0:
            groups["overdue"].append(row)
        elif days <= 1:
            groups["due_soon"].append(row)
        elif days <= 7:
            groups["week"].append(row)
        else:
            groups["later"].append(row)
    groups["later"] = groups["later"][:12]

    for row in store.all_items(limit=300):
        if row["status"] == "done":
            groups["done"].append(row)

    return {
        "generated_at": util.now_ts(),
        "stats": store.stats(),
        "groups": {key: [item_to_dict(r) for r in rows] for key, rows in groups.items()},
    }


class DashboardHandler(BaseHTTPRequestHandler):
    store = None
    server_version = "DeadlineWatch"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # -- helpers ---------------------------------------------------------
    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:
        route = self.path.split("?")[0].rstrip("/") or "/"
        if route == "/":
            self._send_file(DASHBOARD_DIR / "index.html")
        elif route == "/app.js":
            self._send_file(DASHBOARD_DIR / "app.js")
        elif route == "/style.css":
            self._send_file(DASHBOARD_DIR / "style.css")
        elif route == "/api/state":
            self._send_json(build_state(self.store))
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        route = self.path.split("?")[0].rstrip("/")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json({"error": "bad json"}, 400)
            return

        parts = [p for p in route.split("/") if p]
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "items":
            try:
                item_id = int(parts[2])
            except ValueError:
                self._send_json({"error": "bad id"}, 400)
                return
            action = payload.get("action")
            if action == "done":
                self.store.set_status(item_id, "done")
            elif action == "open":
                self.store.set_status(item_id, "open")
                self.store.clear_snooze(item_id)
            elif action == "snooze":
                hours = float(payload.get("hours") or 24)
                self.store.snooze(item_id, util.now_ts() + int(hours * 3600))
            else:
                self._send_json({"error": "unknown action"}, 400)
                return
            self._send_json({"ok": True, "id": item_id, "action": action})
            return

        self._send_json({"error": "not found"}, 404)


def serve(store, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (DashboardHandler,), {"store": store})
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd


def serve_in_background(store, host: str = "127.0.0.1", port: int = 8787, log=print) -> ThreadingHTTPServer:
    httpd = serve(store, host, port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    log(f"[dashboard] serving on http://{host}:{port}")
    return httpd
