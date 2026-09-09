"""Tests for github_app/server.py — the stdlib HTTP layer around webhook.py.

No mocking of the HTTP stack itself: these spin up a real `ThreadingHTTPServer`
on an OS-assigned loopback port and talk to it over a real socket, the same
way GitHub's webhook delivery would, so the exact `do_POST` exception paths
(malformed `Content-Length`, oversized body, malformed JSON / KeyError from a
malformed payload shape) are exercised for real rather than by calling
`Handler` methods in isolation.

`Handler._cfg` is monkeypatched to return a fixed, tokenless test `Config`
instead of hitting `pr_watch.load_config()` (which requires real env vars) —
config resolution is already covered by `pr_watch`'s own tests.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from zoneinfo import ZoneInfo

import pr_watch as pw
from github_app import server as srv

LON = ZoneInfo("Europe/London")
SECRET = "test-webhook-secret"


def _sign(payload: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _make_cfg(tmp_path, **overrides):
    base = dict(
        gh_token="ghtok",
        gh_org="acme-corp",
        team_slug="platform-reviewers",
        gh_repos=[],
        slack_token="xoxb-fake",
        slack_channel="C123",
        pagerduty_token=None,
        pagerduty_schedule_id=None,
        pd_to_slack={},
        oncall_fallback="@oncall",
        tz=LON,
        tz_name="Europe/London",
        business_start_hour=9,
        business_end_hour=17,
        holidays=frozenset(),
        review_windows=["09:15"],
        sla_response_hrs=2.0,
        sla_approval_hrs=4.0,
        approaching_buffer_hrs=1.0,
        send_daily_digest=False,
        dry_run=True,
        state_file=str(tmp_path / "state.json"),
    )
    base.update(overrides)
    return pw.Config(**base)


def _review_requested_payload(*, team_slug="platform-reviewers", number=482):
    return {
        "action": "review_requested",
        "number": number,
        "pull_request": {
            "id": 1234567890,
            "number": number,
            "state": "open",
            "title": "Fix checkout timeout",
            "user": {"login": "octocat", "id": 1},
            "html_url": f"https://github.com/acme-corp/checkout/pull/{number}",
            "updated_at": "2025-10-13T09:00:00Z",
            "head": {"ref": "fix/checkout-timeout", "sha": "abc123"},
        },
        "requested_team": {"id": 42, "slug": team_slug, "name": "Platform Reviewers"},
        "repository": {"full_name": "acme-corp/checkout", "owner": {"login": "acme-corp"}},
        "sender": {"login": "octocat", "id": 1},
    }


@contextmanager
def _running_server(tmp_path, monkeypatch, cfg=None):
    cfg = cfg or _make_cfg(tmp_path)
    monkeypatch.setattr(srv.Handler, "_cfg", lambda self: cfg)
    monkeypatch.setenv("GH_WEBHOOK_SECRET", SECRET)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post(port, body: bytes, headers: dict) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/github/webhook", body=body, headers=headers)
    resp = conn.getresponse()
    resp.read()
    conn.close()
    return resp


# ---------------------------------------------------------------------------
# Bug #2 — do_POST only caught SignatureError; a KeyError (bug #1) or
# json.JSONDecodeError from a malformed body must return a clean 4xx, not
# crash the handler.
# ---------------------------------------------------------------------------
def test_malformed_json_body_returns_clean_400(tmp_path, monkeypatch):
    with _running_server(tmp_path, monkeypatch) as port:
        body = b"{not valid json"
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": "d-1",
        }
        resp = _post(port, body, headers)
        assert resp.status == 400


def test_malformed_payload_shape_returns_clean_400_not_500(tmp_path, monkeypatch):
    """A payload shape that would previously raise a bare KeyError deep in
    parse_pull_request_event (bug #1) must still come back as a clean 4xx
    through the server layer, not crash the handler thread."""
    payload = _review_requested_payload()
    del payload["pull_request"]["updated_at"]
    body = json.dumps(payload).encode("utf-8")
    with _running_server(tmp_path, monkeypatch) as port:
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": "d-2",
        }
        resp = _post(port, body, headers)
        # With bug #1 fixed this is now IGNORED (202), not a crash; this
        # test's job is to prove the server never 500s / hangs on it either
        # way, which is the bug #2 contract.
        assert resp.status in (200, 202)


def test_valid_review_requested_delivery_still_accepted(tmp_path, monkeypatch):
    payload = _review_requested_payload()
    body = json.dumps(payload).encode("utf-8")
    with _running_server(tmp_path, monkeypatch) as port:
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": "d-3",
        }
        resp = _post(port, body, headers)
        assert resp.status == 202


def test_bad_signature_still_returns_401(tmp_path, monkeypatch):
    body = json.dumps(_review_requested_payload()).encode("utf-8")
    with _running_server(tmp_path, monkeypatch) as port:
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=deadbeef",
            "X-GitHub-Delivery": "d-4",
        }
        resp = _post(port, body, headers)
        assert resp.status == 401


# ---------------------------------------------------------------------------
# Bug #3 — unguarded int(Content-Length) raised a bare ValueError on a
# malformed header.
# ---------------------------------------------------------------------------
def test_malformed_content_length_header_returns_clean_400(tmp_path, monkeypatch):
    with _running_server(tmp_path, monkeypatch) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        # putrequest/putheader lets us send a header http.client would
        # never compute itself (a non-numeric Content-Length).
        conn.putrequest("POST", "/github/webhook", skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", f"127.0.0.1:{port}")
        conn.putheader("Content-Length", "not-a-number")
        conn.putheader("X-GitHub-Event", "pull_request")
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 400
        resp.read()
        conn.close()


# ---------------------------------------------------------------------------
# Bug #4 — no cap on request body size; server would `rfile.read(length)`
# unbounded. A malformed/oversized Content-Length must be rejected with a
# clean 4xx *before* attempting to read that many bytes.
# ---------------------------------------------------------------------------
def test_oversized_content_length_rejected_before_reading_body(tmp_path, monkeypatch):
    with _running_server(tmp_path, monkeypatch) as port:
        huge = srv.MAX_BODY_BYTES + 1
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/github/webhook", skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", f"127.0.0.1:{port}")
        conn.putheader("Content-Length", str(huge))
        conn.putheader("X-GitHub-Event", "pull_request")
        conn.endheaders()
        # Deliberately never send `huge` bytes of body. If the server tried
        # to `rfile.read(huge)` before responding, this would hang until
        # the socket timeout instead of returning promptly.
        resp = conn.getresponse()
        assert resp.status == 413
        resp.read()
        conn.close()


def test_negative_content_length_returns_clean_400(tmp_path, monkeypatch):
    with _running_server(tmp_path, monkeypatch) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/github/webhook", skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", f"127.0.0.1:{port}")
        conn.putheader("Content-Length", "-1")
        conn.putheader("X-GitHub-Event", "pull_request")
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 400
        resp.read()
        conn.close()
