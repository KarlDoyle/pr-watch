"""Tests for github_app/webhook.py — signature verification, event parsing,
event-populated state, and the still-periodic breach/digest sweep.

Webhook payloads below follow GitHub's documented `pull_request` event
shape (https://docs.github.com/en/webhooks/webhook-events-and-payloads#pull_request),
trimmed to the fields PR Watch actually reads — but the field names, event
names and nesting match the real payload GitHub sends, so `parse_pull_request_event`
is exercised against a realistic shape even though there is no live,
registered App to send a real delivery.

No network: signature verification uses real `hmac` against a fixed test
secret (no mocking needed — it's pure stdlib crypto), and `sweep_and_alert`
reuses pr_watch's own dry-run / monkeypatched-Slack test pattern.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import pr_watch as pw
from github_app import webhook as wh

LON = ZoneInfo("Europe/London")
SECRET = "test-webhook-secret"


def _dt(y, m, d, hh=0, mm=0, tz=LON):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


def _sign(payload: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ---------------------------------------------------------------------------
# Realistic pull_request webhook payload fixtures
# ---------------------------------------------------------------------------
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
            "created_at": "2025-10-13T08:55:00Z",
            "updated_at": "2025-10-13T09:00:00Z",
            "head": {"ref": "fix/checkout-timeout", "sha": "abc123"},
            "base": {"ref": "main", "sha": "def456"},
            "requested_reviewers": [],
            "requested_teams": [{"id": 42, "slug": team_slug, "name": "Platform Reviewers"}],
        },
        "requested_team": {"id": 42, "slug": team_slug, "name": "Platform Reviewers"},
        "repository": {
            "id": 987654,
            "full_name": "acme-corp/checkout",
            "name": "checkout",
            "owner": {"login": "acme-corp"},
        },
        "sender": {"login": "octocat", "id": 1},
    }


def _review_request_removed_payload(*, team_slug="platform-reviewers", number=482):
    payload = _review_requested_payload(team_slug=team_slug, number=number)
    payload["action"] = "review_request_removed"
    return payload


def _closed_payload(*, number=482):
    return {
        "action": "closed",
        "number": number,
        "pull_request": {
            "id": 1234567890,
            "number": number,
            "state": "closed",
            "title": "Fix checkout timeout",
            "user": {"login": "octocat"},
            "html_url": f"https://github.com/acme-corp/checkout/pull/{number}",
            "merged": True,
            "updated_at": "2025-10-13T12:00:00Z",
            "head": {"ref": "fix/checkout-timeout"},
        },
        "repository": {"full_name": "acme-corp/checkout"},
        "sender": {"login": "octocat"},
    }


def _opened_payload(number=999):
    """An action PR Watch does not act on at all (not a review request)."""
    return {
        "action": "opened",
        "number": number,
        "pull_request": {
            "number": number,
            "title": "WIP",
            "user": {"login": "octocat"},
            "html_url": f"https://github.com/acme-corp/checkout/pull/{number}",
            "updated_at": "2025-10-13T09:00:00Z",
            "head": {"ref": "wip"},
        },
        "repository": {"full_name": "acme-corp/checkout"},
    }


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------
def test_verify_signature_accepts_valid():
    body = json.dumps({"a": 1}).encode("utf-8")
    header = _sign(body)
    wh.verify_signature(body, header, SECRET)  # should not raise


def test_verify_signature_rejects_wrong_secret():
    body = json.dumps({"a": 1}).encode("utf-8")
    header = _sign(body, secret="wrong-secret")
    with pytest.raises(wh.SignatureError):
        wh.verify_signature(body, header, SECRET)


def test_verify_signature_rejects_tampered_payload():
    body = json.dumps({"a": 1}).encode("utf-8")
    header = _sign(body)
    tampered = json.dumps({"a": 2}).encode("utf-8")
    with pytest.raises(wh.SignatureError):
        wh.verify_signature(tampered, header, SECRET)


def test_verify_signature_rejects_missing_header():
    body = b"{}"
    with pytest.raises(wh.SignatureError):
        wh.verify_signature(body, None, SECRET)


def test_verify_signature_rejects_malformed_header():
    body = b"{}"
    with pytest.raises(wh.SignatureError):
        wh.verify_signature(body, "not-a-real-signature", SECRET)


def test_verify_signature_rejects_missing_secret():
    body = b"{}"
    header = _sign(body)
    with pytest.raises(wh.SignatureError):
        wh.verify_signature(body, header, "")


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------
def test_parse_review_requested_for_matching_team():
    payload = _review_requested_payload(team_slug="platform-reviewers")
    event = wh.parse_pull_request_event(payload, "platform-reviewers")
    assert event.kind == wh.REVIEW_REQUESTED
    assert event.key == "acme-corp/checkout#482"
    assert event.pr.repo == "acme-corp/checkout"
    assert event.pr.number == 482
    assert event.pr.title == "Fix checkout timeout"
    assert event.pr.branch == "fix/checkout-timeout"
    assert event.pr.requested_at == pw._parse_iso("2025-10-13T09:00:00Z")


def test_parse_review_requested_ignores_other_team():
    payload = _review_requested_payload(team_slug="other-team")
    event = wh.parse_pull_request_event(payload, "platform-reviewers")
    assert event.kind == wh.IGNORED


def test_parse_review_requested_ignores_individual_reviewer_request():
    payload = _review_requested_payload(team_slug="platform-reviewers")
    del payload["requested_team"]
    payload["requested_reviewer"] = {"login": "someone"}
    event = wh.parse_pull_request_event(payload, "platform-reviewers")
    assert event.kind == wh.IGNORED


def test_parse_review_request_removed_matching_team():
    payload = _review_request_removed_payload(team_slug="platform-reviewers")
    event = wh.parse_pull_request_event(payload, "platform-reviewers")
    assert event.kind == wh.REVIEW_REQUEST_REMOVED
    assert event.key == "acme-corp/checkout#482"


def test_parse_review_request_removed_other_team_ignored():
    payload = _review_request_removed_payload(team_slug="other-team")
    event = wh.parse_pull_request_event(payload, "platform-reviewers")
    assert event.kind == wh.IGNORED


def test_parse_closed_event():
    payload = _closed_payload()
    event = wh.parse_pull_request_event(payload, "platform-reviewers")
    assert event.kind == wh.CLOSED
    assert event.key == "acme-corp/checkout#482"


def test_parse_unrelated_action_ignored():
    payload = _opened_payload()
    event = wh.parse_pull_request_event(payload, "platform-reviewers")
    assert event.kind == wh.IGNORED


def test_parse_review_requested_missing_updated_at_is_ignored_not_crashed():
    """Regression for bug #1: a `review_requested` delivery (or any payload
    shape) missing `pull_request.updated_at` must be skipped as IGNORED, not
    raise an uncaught KeyError from raw dict indexing."""
    payload = _review_requested_payload(team_slug="platform-reviewers")
    del payload["pull_request"]["updated_at"]
    event = wh.parse_pull_request_event(payload, "platform-reviewers")
    assert event.kind == wh.IGNORED


def test_parse_review_requested_missing_pull_request_key_is_ignored_not_crashed():
    """Same malformed-payload class as above, one level up: no `pull_request`
    object at all."""
    payload = _review_requested_payload(team_slug="platform-reviewers")
    del payload["pull_request"]
    event = wh.parse_pull_request_event(payload, "platform-reviewers")
    assert event.kind == wh.IGNORED


def test_parse_review_requested_unparseable_updated_at_is_ignored_not_crashed():
    payload = _review_requested_payload(team_slug="platform-reviewers")
    payload["pull_request"]["updated_at"] = "not-a-timestamp"
    event = wh.parse_pull_request_event(payload, "platform-reviewers")
    assert event.kind == wh.IGNORED


# ---------------------------------------------------------------------------
# Event-populated state
# ---------------------------------------------------------------------------
def test_apply_event_review_requested_populates_open_state():
    state = {"version": 1, "alerted": {}, "open": {}}
    event = wh.parse_pull_request_event(
        _review_requested_payload(team_slug="platform-reviewers"), "platform-reviewers"
    )
    wh.apply_event(state, event)
    assert "acme-corp/checkout#482" in state["open"]
    record = state["open"]["acme-corp/checkout#482"]
    assert record["repo"] == "acme-corp/checkout"
    assert record["title"] == "Fix checkout timeout"


def test_apply_event_removal_clears_open_and_alerted_state():
    state = {
        "version": 1,
        "alerted": {"acme-corp/checkout#482": {"requested_at": "x"}},
        "open": {"acme-corp/checkout#482": {"repo": "acme-corp/checkout", "number": 482}},
    }
    event = wh.parse_pull_request_event(_closed_payload(), "platform-reviewers")
    wh.apply_event(state, event)
    assert "acme-corp/checkout#482" not in state["open"]
    assert "acme-corp/checkout#482" not in state["alerted"]


def test_tracked_prs_computes_elapsed_from_state(tmp_path):
    cfg = _make_cfg(tmp_path)
    state = wh.load_webhook_state(str(tmp_path / "state.json"))
    event = wh.parse_pull_request_event(
        _review_requested_payload(team_slug="platform-reviewers"), "platform-reviewers"
    )
    wh.apply_event(state, event)

    # Requested 09:00 UTC == 10:00 London (BST); now 15:00 London ->
    # 5 business hours -> breached (SLA 4h).
    now = _dt(2025, 10, 13, 15, 0)
    prs = wh.tracked_prs(state, cfg, now)
    assert len(prs) == 1
    assert prs[0].elapsed_hrs == pytest.approx(5.0)
    assert prs[0].category == pw.BREACHED


def test_load_webhook_state_adds_open_key_to_existing_pw_state(tmp_path):
    path = tmp_path / "state.json"
    pw.save_state(str(path), {"version": 1, "alerted": {}})
    state = wh.load_webhook_state(str(path))
    assert state["open"] == {}


# ---------------------------------------------------------------------------
# handle_delivery (signature -> parse -> state, end to end)
# ---------------------------------------------------------------------------
def test_handle_delivery_review_requested_persists_state(tmp_path):
    state_path = str(tmp_path / "state.json")
    payload = json.dumps(_review_requested_payload(team_slug="platform-reviewers")).encode("utf-8")
    header = _sign(payload)

    event = wh.handle_delivery(
        event_name="pull_request",
        payload=payload,
        signature_header=header,
        team_slug="platform-reviewers",
        state_path=state_path,
        secret=SECRET,
    )
    assert event.kind == wh.REVIEW_REQUESTED

    state = wh.load_webhook_state(state_path)
    assert "acme-corp/checkout#482" in state["open"]


def test_handle_delivery_rejects_bad_signature(tmp_path):
    state_path = str(tmp_path / "state.json")
    payload = json.dumps(_review_requested_payload()).encode("utf-8")
    with pytest.raises(wh.SignatureError):
        wh.handle_delivery(
            event_name="pull_request",
            payload=payload,
            signature_header="sha256=deadbeef",
            team_slug="platform-reviewers",
            state_path=state_path,
            secret=SECRET,
        )


def test_handle_delivery_ignores_non_pull_request_event(tmp_path):
    state_path = str(tmp_path / "state.json")
    payload = json.dumps({"zen": "Keep it logically awesome."}).encode("utf-8")
    header = _sign(payload)
    event = wh.handle_delivery(
        event_name="ping",
        payload=payload,
        signature_header=header,
        team_slug="platform-reviewers",
        state_path=state_path,
        secret=SECRET,
    )
    assert event.kind == wh.IGNORED


# ---------------------------------------------------------------------------
# sweep_and_alert — the still-periodic half, driven entirely from state
# ---------------------------------------------------------------------------
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
        send_daily_digest=True,
        dry_run=True,
        state_file=str(tmp_path / "state.json"),
    )
    base.update(overrides)
    return pw.Config(**base)


def test_sweep_and_alert_fires_breach_from_webhook_state(tmp_path, capsys):
    cfg = _make_cfg(tmp_path, send_daily_digest=False)
    state = wh.load_webhook_state(cfg.state_file)
    event = wh.parse_pull_request_event(
        _review_requested_payload(team_slug="platform-reviewers"), "platform-reviewers"
    )
    wh.apply_event(state, event)
    pw.save_state(cfg.state_file, state)

    now = _dt(2025, 10, 13, 15, 0)  # 6 business hours later -> breached
    result = wh.sweep_and_alert(cfg, now)
    assert result.alerts_sent == ["acme-corp/checkout#482"]

    # No Search-API call happened anywhere in this path — everything came
    # from the webhook-populated state file.
    second = wh.sweep_and_alert(cfg, now)
    assert second.alerts_sent == []  # deduped, same as pr_watch.run_cycle


def test_sweep_and_alert_prunes_after_removal(tmp_path):
    cfg = _make_cfg(tmp_path, send_daily_digest=False)
    state = wh.load_webhook_state(cfg.state_file)
    wh.apply_event(
        state,
        wh.parse_pull_request_event(
            _review_requested_payload(team_slug="platform-reviewers"), "platform-reviewers"
        ),
    )
    pw.save_state(cfg.state_file, state)
    wh.sweep_and_alert(cfg, _dt(2025, 10, 13, 15, 0))

    # Team's review request is removed (or PR merged) -> webhook clears it.
    state = wh.load_webhook_state(cfg.state_file)
    wh.apply_event(
        state,
        wh.parse_pull_request_event(_closed_payload(), "platform-reviewers"),
    )
    pw.save_state(cfg.state_file, state)

    result = wh.sweep_and_alert(cfg, _dt(2025, 10, 13, 16, 0))
    assert result.prs == []
