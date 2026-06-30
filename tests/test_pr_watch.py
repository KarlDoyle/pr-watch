"""Tests for pr_watch.

No network, no tokens: every HTTP layer is exercised through a monkeypatched
``urlopen``. Time is injected so business-hours, categorisation, digest
ordering and the review-window check are all deterministic. A final test scans
the source for committed secrets.
"""

from __future__ import annotations

import json
import re
import urllib.error
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import pr_watch as pw

ROOT = Path(__file__).resolve().parent.parent
UTC = ZoneInfo("UTC")
LON = ZoneInfo("Europe/London")


def _dt(y, m, d, hh=0, mm=0, tz=LON):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


def _make_pr(repo, number, elapsed, category, requested_at=None):
    pr = pw.PR(
        repo=repo,
        number=number,
        title=f"PR {number}",
        author="alice",
        url=f"https://github.com/{repo}/pull/{number}",
        branch="main",
        requested_at=requested_at or _dt(2025, 10, 13, 9, 0),
        elapsed_hrs=elapsed,
    )
    pr.category = category
    return pr


# ---------------------------------------------------------------------------
# Business-hours engine
# ---------------------------------------------------------------------------
def test_business_hours_same_day_simple():
    # Mon 2025-10-13, 09:00 -> 13:00 = 4.0 business hours.
    start = _dt(2025, 10, 13, 9, 0)
    end = _dt(2025, 10, 13, 13, 0)
    got = pw.business_hours_between(start, end, tz=LON, start_hour=9, end_hour=17)
    assert got == pytest.approx(4.0)


def test_business_hours_clamps_to_window():
    # 06:00 -> 22:00 on a weekday clamps to the 09-17 window = 8h.
    start = _dt(2025, 10, 13, 6, 0)
    end = _dt(2025, 10, 13, 22, 0)
    got = pw.business_hours_between(start, end, tz=LON, start_hour=9, end_hour=17)
    assert got == pytest.approx(8.0)


def test_business_hours_skips_weekend():
    # Fri 16:00 -> Mon 10:00 = 1h (Fri) + 1h (Mon), weekend excluded.
    start = _dt(2025, 10, 17, 16, 0)  # Friday
    end = _dt(2025, 10, 20, 10, 0)  # Monday
    got = pw.business_hours_between(start, end, tz=LON, start_hour=9, end_hour=17)
    assert got == pytest.approx(2.0)


def test_business_hours_multi_day():
    # Mon 15:00 -> Wed 11:00 = 2h (Mon) + 8h (Tue) + 2h (Wed) = 12h.
    start = _dt(2025, 10, 13, 15, 0)
    end = _dt(2025, 10, 15, 11, 0)
    got = pw.business_hours_between(start, end, tz=LON, start_hour=9, end_hour=17)
    assert got == pytest.approx(12.0)


def test_business_hours_excludes_holiday():
    # Mon 13 Oct marked holiday: Mon 09:00 -> Tue 13:00 = only Tue 09-13 = 4h.
    start = _dt(2025, 10, 13, 9, 0)
    end = _dt(2025, 10, 14, 13, 0)
    got = pw.business_hours_between(
        start, end, tz=LON, start_hour=9, end_hour=17, holidays=frozenset({"20251013"})
    )
    assert got == pytest.approx(4.0)


def test_business_hours_end_before_start_is_zero():
    start = _dt(2025, 10, 13, 13, 0)
    end = _dt(2025, 10, 13, 9, 0)
    assert pw.business_hours_between(start, end, tz=LON, start_hour=9, end_hour=17) == 0.0


def test_business_hours_handles_utc_input_in_local_tz():
    # 08:00Z on a winter day == 08:00 London; only 09-17 counts -> from 09:00.
    start = datetime(2025, 1, 6, 8, 0, tzinfo=UTC)  # Monday
    end = datetime(2025, 1, 6, 12, 0, tzinfo=UTC)
    got = pw.business_hours_between(start, end, tz=LON, start_hour=9, end_hour=17)
    assert got == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Categorisation boundaries
# ---------------------------------------------------------------------------
def test_categorise_breached_at_threshold():
    assert pw.categorise(4.0, 4.0, 1.0) == pw.BREACHED
    assert pw.categorise(5.5, 4.0, 1.0) == pw.BREACHED


def test_categorise_approaching_band():
    assert pw.categorise(3.0, 4.0, 1.0) == pw.APPROACHING  # lower edge inclusive
    assert pw.categorise(3.9, 4.0, 1.0) == pw.APPROACHING


def test_categorise_fresh_below_band():
    assert pw.categorise(2.99, 4.0, 1.0) == pw.FRESH
    assert pw.categorise(0.0, 4.0, 1.0) == pw.FRESH


# ---------------------------------------------------------------------------
# State-file dedup
# ---------------------------------------------------------------------------
def test_state_dedup_does_not_realert(tmp_path):
    path = tmp_path / "state.json"
    state = pw.load_state(str(path))
    pr = _make_pr("acme-corp/checkout", 245, 5.2, pw.BREACHED)

    assert pw.needs_alert(state, pr.key, pr.requested_at_iso) is True
    pw.mark_alerted(state, pr, _dt(2025, 10, 13, 14, 0))
    # Same request -> already alerted -> no re-alert.
    assert pw.needs_alert(state, pr.key, pr.requested_at_iso) is False


def test_state_rearms_on_new_request(tmp_path):
    state = pw.load_state(str(tmp_path / "s.json"))
    pr = _make_pr("acme-corp/checkout", 245, 5.2, pw.BREACHED)
    pw.mark_alerted(state, pr, _dt(2025, 10, 13, 14, 0))
    # A fresh review-request (different timestamp) re-arms the alert.
    pr2 = _make_pr(
        "acme-corp/checkout", 245, 1.0, pw.BREACHED, requested_at=_dt(2025, 10, 14, 9, 0)
    )
    assert pw.needs_alert(state, pr2.key, pr2.requested_at_iso) is True


def test_prune_removes_closed_pr():
    state = {"version": 1, "alerted": {"acme-corp/checkout#245": {"requested_at": "x"}}}
    pruned = pw.prune_state(state, open_keys=set())  # PR no longer open
    assert pruned == ["acme-corp/checkout#245"]
    assert state["alerted"] == {}


def test_prune_keeps_open_pr():
    state = {"version": 1, "alerted": {"acme-corp/checkout#245": {"requested_at": "x"}}}
    pruned = pw.prune_state(state, open_keys={"acme-corp/checkout#245"})
    assert pruned == []
    assert "acme-corp/checkout#245" in state["alerted"]


def test_state_save_and_reload_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = pw.load_state(str(path))
    pr = _make_pr("acme-corp/bff", 9, 4.5, pw.BREACHED)
    pw.mark_alerted(state, pr, _dt(2025, 10, 13, 14, 0))
    pw.save_state(str(path), state)
    reloaded = pw.load_state(str(path))
    assert reloaded["alerted"][pr.key]["requested_at"] == pr.requested_at_iso


def test_load_state_tolerates_garbage(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not json", encoding="utf-8")
    state = pw.load_state(str(path))
    assert state == {"version": 1, "alerted": {}}


# ---------------------------------------------------------------------------
# Digest ordering
# ---------------------------------------------------------------------------
def test_digest_ordering():
    prs = [
        _make_pr("r", 1, 5.0, pw.BREACHED),
        _make_pr("r", 2, 7.0, pw.BREACHED),
        _make_pr("r", 3, 3.1, pw.APPROACHING),
        _make_pr("r", 4, 3.8, pw.APPROACHING),
        _make_pr("r", 5, 0.4, pw.FRESH),
        _make_pr("r", 6, 1.5, pw.FRESH),
    ]
    breached, approaching, fresh = pw.order_digest(prs)

    # Breached: oldest (most waiting) first.
    assert [p.number for p in breached] == [2, 1]
    # Approaching: closest to breach (highest elapsed) first.
    assert [p.number for p in approaching] == [4, 3]
    # Fresh: newest (least waiting) first.
    assert [p.number for p in fresh] == [5, 6]


def test_digest_line_format():
    pr = _make_pr("acme-corp/checkout", 245, 5.2, pw.BREACHED)
    pr.title = "Fix checkout bug"
    assert pw._digest_line(pr) == "- acme-corp/checkout #245 · Fix checkout bug · 5.2h waiting"


# ---------------------------------------------------------------------------
# Review-window check
# ---------------------------------------------------------------------------
def test_is_review_window_matches_to_the_minute():
    now = _dt(2025, 10, 13, 9, 15)
    assert pw.is_review_window(now, ["09:15", "15:00"]) is True


def test_is_review_window_no_match():
    now = _dt(2025, 10, 13, 9, 16)
    assert pw.is_review_window(now, ["09:15", "15:00"]) is False


def test_is_review_window_second_slot():
    now = _dt(2025, 10, 13, 15, 0)
    assert pw.is_review_window(now, ["09:15", "15:00"]) is True


# ---------------------------------------------------------------------------
# PagerDuty -> Slack mapping
# ---------------------------------------------------------------------------
def test_pd_to_slack_mention_hit():
    mapping = {"dev@acme-corp.test": "U1234567"}
    assert pw.pd_to_slack_mention("dev@acme-corp.test", mapping, "@oncall") == "<@U1234567>"


def test_pd_to_slack_mention_fallback_unknown_email():
    assert pw.pd_to_slack_mention("nobody@acme-corp.test", {}, "@oncall") == "@oncall"


def test_pd_to_slack_mention_fallback_none_email():
    assert pw.pd_to_slack_mention(None, {"a": "U1"}, "@oncall") == "@oncall"


# ---------------------------------------------------------------------------
# Mocked urlopen helpers
# ---------------------------------------------------------------------------
class _FakeResp:
    """Minimal context-manager stand-in for urlopen's response."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _cfg(tmp_path, **overrides):
    base = dict(
        gh_token="ghtok",
        gh_org="acme-corp",
        team_slug="your-team",
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
        review_windows=["09:15", "15:00"],
        sla_response_hrs=2.0,
        sla_approval_hrs=4.0,
        approaching_buffer_hrs=1.0,
        send_daily_digest=True,
        dry_run=True,
        state_file=str(tmp_path / "state.json"),
    )
    base.update(overrides)
    return pw.Config(**base)


# ---------------------------------------------------------------------------
# GitHub layer (mocked urlopen)
# ---------------------------------------------------------------------------
def test_github_search_prs_single_page(monkeypatch):
    captured = {}

    def fake_urlopen(req, *a, **k):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        return _FakeResp(
            {
                "total_count": 1,
                "items": [{"number": 245, "title": "Fix bug"}],
            }
        )

    monkeypatch.setattr(pw, "urlopen", fake_urlopen)
    items = pw.github_search_prs("ghtok", "acme-corp", "your-team")
    assert len(items) == 1
    assert "team-review-requested" in captured["url"]
    assert captured["auth"] == "Bearer ghtok"


def test_github_latest_team_request_picks_latest(monkeypatch):
    events = [
        {
            "event": "review_requested",
            "requested_team": {"slug": "your-team"},
            "created_at": "2025-10-13T09:00:00Z",
        },
        {
            "event": "review_requested",
            "requested_team": {"slug": "other-team"},
            "created_at": "2025-10-13T11:00:00Z",
        },
        {
            "event": "review_requested",
            "requested_team": {"slug": "your-team"},
            "created_at": "2025-10-13T10:30:00Z",
        },
    ]
    monkeypatch.setattr(pw, "urlopen", lambda req, *a, **k: _FakeResp(events))
    got = pw.github_latest_team_request("t", "acme-corp/checkout", 245, "your-team")
    assert got == pw._parse_iso("2025-10-13T10:30:00Z")


def test_github_latest_team_request_none_when_absent(monkeypatch):
    events = [{"event": "labeled", "created_at": "2025-10-13T09:00:00Z"}]
    monkeypatch.setattr(pw, "urlopen", lambda req, *a, **k: _FakeResp(events))
    assert pw.github_latest_team_request("t", "acme-corp/x", 1, "your-team") is None


def test_build_prs_enriches_and_categorises(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    items = [
        {
            "number": 245,
            "title": "Fix checkout bug",
            "html_url": "https://github.com/acme-corp/checkout/pull/245",
            "user": {"login": "alice"},
            "repository_url": "https://api.github.com/repos/acme-corp/checkout",
            "created_at": "2025-10-13T09:00:00Z",
            "pull_request": {"head": {"ref": "feature"}},
        }
    ]
    # Latest team request at Mon 09:00 London.
    monkeypatch.setattr(
        pw,
        "github_latest_team_request",
        lambda *a, **k: _dt(2025, 10, 13, 9, 0),
    )
    now = _dt(2025, 10, 13, 14, 0)  # 5 business hours later
    prs = pw.build_prs(cfg.gh_token, cfg, now, items)
    assert len(prs) == 1
    pr = prs[0]
    assert pr.repo == "acme-corp/checkout"
    assert pr.branch == "feature"
    assert pr.elapsed_hrs == pytest.approx(5.0)
    assert pr.category == pw.BREACHED


# ---------------------------------------------------------------------------
# Slack layer (mocked urlopen)
# ---------------------------------------------------------------------------
def test_slack_post_message_posts_json(monkeypatch):
    captured = {}

    def fake_urlopen(req, *a, **k):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp({"ok": True})

    monkeypatch.setattr(pw, "urlopen", fake_urlopen)
    data = pw.slack_post_message("xoxb-fake", "C123", "hello", blocks=[{"type": "section"}])
    assert data["ok"] is True
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/chat.postMessage")
    assert captured["body"]["channel"] == "C123"
    assert captured["body"]["text"] == "hello"


# ---------------------------------------------------------------------------
# PagerDuty layer (mocked urlopen) + resolver
# ---------------------------------------------------------------------------
def test_pagerduty_oncall_email_primary(monkeypatch):
    payload = {
        "oncalls": [
            {"escalation_level": 2, "user": {"email": "backup@acme-corp.test"}},
            {"escalation_level": 1, "user": {"email": "primary@acme-corp.test"}},
        ]
    }
    monkeypatch.setattr(pw, "urlopen", lambda req, *a, **k: _FakeResp(payload))
    email = pw.pagerduty_oncall_email("pdtok", "SCHED1", _dt(2025, 10, 13, 9, 0))
    assert email == "primary@acme-corp.test"


def test_resolve_oncall_mention_uses_pagerduty(monkeypatch, tmp_path):
    cfg = _cfg(
        tmp_path,
        pagerduty_token="pd",
        pagerduty_schedule_id="S1",
        pd_to_slack={"primary@acme-corp.test": "U999"},
    )
    monkeypatch.setattr(pw, "pagerduty_oncall_email", lambda *a, **k: "primary@acme-corp.test")
    assert pw.resolve_oncall_mention(cfg, _dt(2025, 10, 13, 9, 0)) == "<@U999>"


def test_resolve_oncall_mention_fallback_when_no_pd(tmp_path):
    cfg = _cfg(tmp_path)  # no PagerDuty configured
    assert pw.resolve_oncall_mention(cfg, _dt(2025, 10, 13, 9, 0)) == "@oncall"


def test_resolve_oncall_mention_fallback_on_api_error(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, pagerduty_token="pd", pagerduty_schedule_id="S1")

    def boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(pw, "pagerduty_oncall_email", boom)
    assert pw.resolve_oncall_mention(cfg, _dt(2025, 10, 13, 9, 0)) == "@oncall"


# ---------------------------------------------------------------------------
# HTTP backoff
# ---------------------------------------------------------------------------
def test_backoff_honours_retry_after_then_exponential():
    assert pw._backoff_seconds("3", 0) == 3.0
    assert pw._backoff_seconds(None, 0) == 1.0
    assert pw._backoff_seconds(None, 2) == 4.0
    assert pw._backoff_seconds("9999", 0) == pw.RATE_LIMIT_MAX_BACKOFF_S
    assert pw._backoff_seconds("not-a-number", 1) == 2.0


def test_http_json_retries_on_429_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(pw, "_sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def fake_urlopen(req, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {"Retry-After": "2"}, None)
        return _FakeResp({"ok": True})

    monkeypatch.setattr(pw, "urlopen", fake_urlopen)
    data = pw.http_json("https://example.test/x", headers={})
    assert data == {"ok": True}
    assert calls["n"] == 2
    assert sleeps == [2.0]


def test_http_json_reraises_after_max_retries(monkeypatch):
    monkeypatch.setattr(pw, "_sleep", lambda s: None)

    def always_429(req, *a, **k):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, None)

    monkeypatch.setattr(pw, "urlopen", always_429)
    with pytest.raises(urllib.error.HTTPError):
        pw.http_json("https://example.test/x", headers={})


# ---------------------------------------------------------------------------
# Full run cycle (everything mocked / dry-run)
# ---------------------------------------------------------------------------
def _search_item(number, repo="acme-corp/checkout"):
    return {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "user": {"login": "alice"},
        "repository_url": f"https://api.github.com/repos/{repo}",
        "created_at": "2025-10-13T09:00:00Z",
        "pull_request": {"head": {"ref": "main"}},
    }


def test_run_cycle_alerts_once_and_dedups(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, dry_run=True, send_daily_digest=False)
    monkeypatch.setattr(pw, "github_search_prs", lambda *a, **k: [_search_item(245)])
    # Requested Mon 09:00; now Mon 15:00 -> 6 business hours -> breached.
    monkeypatch.setattr(pw, "github_latest_team_request", lambda *a, **k: _dt(2025, 10, 13, 9, 0))
    now = _dt(2025, 10, 13, 15, 0)

    first = pw.run_cycle(cfg, now)
    assert first.alerts_sent == ["acme-corp/checkout#245"]

    # Second run, same state file -> no duplicate alert.
    second = pw.run_cycle(cfg, now)
    assert second.alerts_sent == []


def test_run_cycle_prunes_closed_pr(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, dry_run=True, send_daily_digest=False)
    monkeypatch.setattr(pw, "github_search_prs", lambda *a, **k: [_search_item(245)])
    monkeypatch.setattr(pw, "github_latest_team_request", lambda *a, **k: _dt(2025, 10, 13, 9, 0))
    now = _dt(2025, 10, 13, 15, 0)
    pw.run_cycle(cfg, now)  # PR 245 alerted + recorded

    # Now the PR is gone from search results -> pruned from state.
    monkeypatch.setattr(pw, "github_search_prs", lambda *a, **k: [])
    result = pw.run_cycle(cfg, now)
    assert result.pruned == ["acme-corp/checkout#245"]
    state = pw.load_state(cfg.state_file)
    assert state["alerted"] == {}


def test_run_cycle_sends_digest_at_window(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, dry_run=True, send_daily_digest=True)
    monkeypatch.setattr(pw, "github_search_prs", lambda *a, **k: [_search_item(245)])
    monkeypatch.setattr(pw, "github_latest_team_request", lambda *a, **k: _dt(2025, 10, 13, 9, 0))
    # 09:15 is a configured review window.
    now = _dt(2025, 10, 13, 9, 15)
    result = pw.run_cycle(cfg, now)
    assert result.digest_sent is True


def test_run_cycle_skips_digest_off_window(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path, dry_run=True, send_daily_digest=True)
    monkeypatch.setattr(pw, "github_search_prs", lambda *a, **k: [_search_item(245)])
    monkeypatch.setattr(pw, "github_latest_team_request", lambda *a, **k: _dt(2025, 10, 13, 9, 0))
    now = _dt(2025, 10, 13, 11, 7)  # not a window
    result = pw.run_cycle(cfg, now)
    assert result.digest_sent is False


# ---------------------------------------------------------------------------
# Message formatting smoke tests
# ---------------------------------------------------------------------------
def test_format_breach_alert_contains_key_fields():
    pr = _make_pr("acme-corp/checkout", 245, 5.2, pw.BREACHED)
    pr.title = "Fix checkout bug"
    text, blocks = pw.format_breach_alert(pr, "<@U1234567>", 4.0, LON)
    assert "Review needed" in text
    assert "PR #245: Fix checkout bug" in text
    assert "5.2h since request" in text
    assert "<@U1234567> (on-call)" in text
    assert any(b["type"] == "actions" for b in blocks)


def test_format_digest_sections_present():
    breached = [_make_pr("acme-corp/checkout", 245, 5.2, pw.BREACHED)]
    approaching = [_make_pr("acme-corp/bff", 251, 3.1, pw.APPROACHING)]
    fresh = [_make_pr("acme-corp/checkout", 252, 0.4, pw.FRESH)]
    text, _ = pw.format_digest(
        breached, approaching, fresh, "<@U1234567>", _dt(2025, 10, 13, 9, 15), LON
    )
    assert "Daily Review Queue — 09:15" in text
    assert "🚨 Breached SLA" in text
    assert "⏳ Approaching SLA" in text
    assert "🆕 Fresh PRs" in text


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------
def test_load_config_missing_required_secrets(monkeypatch):
    for var in ("GH_TOKEN", "GH_ORG", "SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(pw.ConfigError) as exc:
        pw.load_config()
    msg = str(exc.value)
    assert "GH_TOKEN" in msg and "SLACK_CHANNEL_ID" in msg


def test_load_config_neutral_team_default(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("GH_ORG", "acme-corp")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C1")
    monkeypatch.delenv("TEAM_SLUG", raising=False)
    monkeypatch.delenv("TZ_NAME", raising=False)
    cfg = pw.load_config()
    assert cfg.team_slug == "your-team"
    assert cfg.oncall_fallback == "@oncall"
    assert cfg.tz_name == "Europe/London"


# ---------------------------------------------------------------------------
# Committed-secret scan
# ---------------------------------------------------------------------------
def test_no_committed_secrets():
    src = (ROOT / "pr_watch.py").read_text(encoding="utf-8")
    # No real Slack tokens, GitHub PATs, or generic keys baked into the source.
    assert not re.search(r"xox[baprs]-[A-Za-z0-9-]{10,}", src)
    assert not re.search(r"gh[pousr]_[A-Za-z0-9]{20,}", src)
    assert not re.search(r"sk-[A-Za-z0-9]{20,}", src)
    # Secret constants must remain unset (sourced from constant/env).
    for line in ("GH_TOKEN: str | None = None", "SLACK_BOT_TOKEN: str | None = None"):
        assert line in src
    # No employer-specific placeholders carried through from the spec.
    assert "pass-team" not in src
    assert "pass-oncall" not in src
    assert "Europe/Dublin" not in src
