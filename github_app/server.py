#!/usr/bin/env python3
"""Stdlib HTTP server for the PR Watch GitHub App webhook.

Deliberately `http.server` rather than a framework (Flask/FastAPI/etc.) —
`pr_watch.py`'s "standard library only" rule (see AGENTS.md) carries over
to the App's request-handling path. The one place a third-party dependency
is unavoidable is RS256 JWT signing in `github_app/auth.py`, which stays
optional and is not needed just to *receive* webhooks (only to mint
installation tokens for calling back into the GitHub API).

Run:

    GH_WEBHOOK_SECRET=... TEAM_SLUG=... \\
    GH_TOKEN=... GH_ORG=... SLACK_BOT_TOKEN=... SLACK_CHANNEL_ID=... \\
    python3 -m github_app.server

Exposes:
    POST /github/webhook   — GitHub App webhook delivery endpoint
    GET  /healthz          — liveness probe

This is a scaffold for local/self-hosted use (a small VM, a container, a
box behind a reverse proxy with TLS) — it is not a production-grade async
server. For real traffic, put it behind something that terminates TLS and
consider running `sweep_and_alert` from a scheduler (cron / GitHub Actions
`schedule`) instead of the background thread below, which is here for
convenience in a single-process deployment.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pr_watch as pw
from github_app.webhook import SignatureError, handle_delivery, sweep_and_alert

SWEEP_INTERVAL_S = int(os.getenv("PR_WATCH_SWEEP_INTERVAL_S", "900"))  # 15 min, same as cron

# GitHub webhook payloads are never huge (the largest realistic pull_request
# delivery is a few hundred KB); cap well above that so a single oversized
# request can't exhaust memory via ThreadingHTTPServer's one-thread-per-
# request model.
MAX_BODY_BYTES = int(os.getenv("PR_WATCH_MAX_BODY_BYTES", str(5 * 1024 * 1024)))  # 5 MB


class Handler(BaseHTTPRequestHandler):
    server_version = "PRWatchGitHubApp/0.1"

    def _cfg(self) -> pw.Config:
        return pw.load_config()

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        # Never let the default logger echo headers (may include signatures).
        print(f"[github_app] {self.address_string()} - {fmt % args}")

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == "/healthz":
            self._respond(200, b"ok")
            return
        self._respond(404, b"not found")

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path != "/github/webhook":
            self._respond(404, b"not found")
            return

        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            self._respond(400, b"bad request: malformed Content-Length header")
            return
        if length < 0:
            self._respond(400, b"bad request: malformed Content-Length header")
            return
        if length > MAX_BODY_BYTES:
            self._respond(413, b"payload too large")
            return
        payload = self.rfile.read(length) if length else b""
        event_name = self.headers.get("X-GitHub-Event", "")
        signature = self.headers.get("X-Hub-Signature-256")
        delivery_id = self.headers.get("X-GitHub-Delivery", "-")

        try:
            cfg = self._cfg()
        except pw.ConfigError as exc:
            print(f"[github_app] delivery {delivery_id}: config error: {exc}")
            self._respond(500, b"server misconfigured")
            return

        try:
            event = handle_delivery(
                event_name=event_name,
                payload=payload,
                signature_header=signature,
                team_slug=cfg.team_slug,
                state_path=cfg.state_file,
            )
        except SignatureError as exc:
            print(f"[github_app] delivery {delivery_id}: signature rejected: {exc}")
            self._respond(401, b"invalid signature")
            return
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            print(f"[github_app] delivery {delivery_id}: malformed payload: {exc}")
            self._respond(400, b"bad request: malformed payload")
            return

        print(f"[github_app] delivery {delivery_id}: {event_name} -> {event.kind}")
        self._respond(202, b"accepted")

    def _respond(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _sweep_loop(stop_event: threading.Event) -> None:
    """Background thread: periodically evaluate SLA breaches/digests from
    whatever the webhook has recorded so far. See module docstring — event
    delivery and time-elapsed evaluation are two different concerns."""
    while not stop_event.is_set():
        try:
            cfg = pw.load_config()
            sweep_and_alert(cfg, datetime.now(cfg.tz))
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            print(f"[github_app] sweep error: {exc}")
        stop_event.wait(SWEEP_INTERVAL_S)


def serve(host: str = "0.0.0.0", port: int | None = None) -> None:  # noqa: S104 - bind-all is expected for a webhook receiver behind a proxy
    port = port if port is not None else int(os.getenv("PORT", "8080"))
    stop_event = threading.Event()
    sweep_thread = threading.Thread(target=_sweep_loop, args=(stop_event,), daemon=True)
    sweep_thread.start()

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[github_app] listening on {host}:{port} (POST /github/webhook, GET /healthz)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        httpd.server_close()


if __name__ == "__main__":
    serve()
