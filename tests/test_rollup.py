"""Tests for rollup.py — the multi-team, director-level landscape digest.

No network: `aggregate_landscape` takes an injected `fetch_fn`, same pattern
as pr_watch's monkeypatched `urlopen` tests. Covers aggregation across
sources, one-source-failure resilience, and the awareness-framing language
(no "slow"/"blame"/individual naming) in the rendered digest.
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import pr_watch as pw
import rollup as ru

LON = ZoneInfo("Europe/London")


def _dt(y, m, d, hh=0, mm=0, tz=LON):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


def _pr(repo, number, elapsed, category):
    pr = pw.PR(
        repo=repo,
        number=number,
        title=f"PR {number}",
        author="alice",
        url=f"https://github.com/{repo}/pull/{number}",
        branch="main",
        requested_at=_dt(2025, 10, 13, 9, 0),
        elapsed_hrs=elapsed,
    )
    pr.category = category
    return pr


def _rollup_cfg(sources, dry_run=True):
    return ru.RollupConfig(
        gh_token="ghtok",
        sources=sources,
        slack_token="xoxb-fake",
        slack_channel="C_DIRECTOR",
        tz=LON,
        business_start_hour=9,
        business_end_hour=17,
        holidays=frozenset(),
        sla_approval_hrs=4.0,
        approaching_buffer_hrs=1.0,
        review_windows=["09:15"],
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def test_load_sources_from_inline_json():
    raw = (
        '[{"label": "Payments", "org": "acme", "team_slug": "payments"},'
        '{"label": "Checkout", "org": "acme", "team_slug": "checkout", "repos": ["co"]}]'
    )
    sources = ru._load_sources(raw)
    assert len(sources) == 2
    assert sources[0].label == "Payments"
    assert sources[1].repos == ["co"]


def test_load_sources_from_file(tmp_path):
    path = tmp_path / "teams.json"
    path.write_text('[{"label": "A", "org": "acme", "team_slug": "a"}]', encoding="utf-8")
    sources = ru._load_sources(str(path))
    assert sources[0].label == "A"


def test_load_sources_rejects_empty():
    with pytest.raises(ru.RollupConfigError):
        ru._load_sources("[]")


def test_load_sources_rejects_missing_fields():
    with pytest.raises(ru.RollupConfigError):
        ru._load_sources('[{"label": "A"}]')


def test_load_sources_requires_config():
    with pytest.raises(ru.RollupConfigError):
        ru._load_sources(None)


def test_load_rollup_config_missing_required(monkeypatch):
    for var in ("GH_TOKEN", "SLACK_BOT_TOKEN", "LANDSCAPE_SLACK_CHANNEL_ID", "PR_WATCH_TEAMS_JSON"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ru.RollupConfigError) as exc:
        ru.load_rollup_config()
    assert "GH_TOKEN" in str(exc.value)


def test_load_rollup_config_success(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setenv("LANDSCAPE_SLACK_CHANNEL_ID", "C1")
    monkeypatch.setenv("PR_WATCH_TEAMS_JSON", '[{"label": "A", "org": "acme", "team_slug": "a"}]')
    cfg = ru.load_rollup_config()
    assert cfg.sources[0].label == "A"
    assert cfg.slack_channel == "C1"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def test_aggregate_landscape_calls_each_source():
    sources = [
        ru.TeamSource(label="Payments", org="acme", team_slug="payments"),
        ru.TeamSource(label="Checkout", org="acme", team_slug="checkout"),
    ]
    cfg = _rollup_cfg(sources)
    calls = []

    def fake_fetch(token, source, cfg_, now):
        calls.append(source.label)
        if source.label == "Payments":
            return [_pr("acme/pay", 1, 5.0, pw.BREACHED)]
        return [_pr("acme/co", 2, 0.5, pw.FRESH)]

    snapshots = ru.aggregate_landscape(cfg, _dt(2025, 10, 13, 15, 0), fetch_fn=fake_fetch)
    assert calls == ["Payments", "Checkout"]
    assert len(snapshots) == 2
    assert snapshots[0].label == "Payments"
    assert len(snapshots[0].breached) == 1
    assert len(snapshots[1].fresh) == 1


def test_aggregate_landscape_tolerates_one_source_failing():
    sources = [
        ru.TeamSource(label="Payments", org="acme", team_slug="payments"),
        ru.TeamSource(label="Checkout", org="acme", team_slug="checkout"),
    ]
    cfg = _rollup_cfg(sources)

    def flaky_fetch(token, source, cfg_, now):
        if source.label == "Payments":
            raise RuntimeError("GitHub is down")
        return [_pr("acme/co", 2, 0.5, pw.FRESH)]

    snapshots = ru.aggregate_landscape(cfg, _dt(2025, 10, 13, 15, 0), fetch_fn=flaky_fetch)
    assert len(snapshots) == 2
    assert snapshots[0].prs == []  # failed source reported empty, not raised
    assert len(snapshots[1].fresh) == 1


def test_aggregate_landscape_raises_when_every_source_fails():
    sources = [ru.TeamSource(label="Payments", org="acme", team_slug="payments")]
    cfg = _rollup_cfg(sources)

    def always_fails(token, source, cfg_, now):
        raise RuntimeError("down")

    with pytest.raises(ru.RollupConfigError):
        ru.aggregate_landscape(cfg, _dt(2025, 10, 13, 15, 0), fetch_fn=always_fails)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def test_summarise_totals():
    snapshots = [
        ru.TeamSnapshot(
            label="A",
            prs=[_pr("a/r", 1, 5.0, pw.BREACHED), _pr("a/r", 2, 3.2, pw.APPROACHING)],
        ),
        ru.TeamSnapshot(label="B", prs=[_pr("b/r", 3, 0.5, pw.FRESH)]),
    ]
    summary = ru.summarise(snapshots)
    assert summary.teams_observed == 2
    assert summary.total_breached == 1
    assert summary.total_approaching == 1
    assert summary.total_fresh == 1
    assert [s.label for s in summary.teams_with_breaches] == ["A"]


def test_oldest_breach_hrs():
    snap = ru.TeamSnapshot(
        label="A", prs=[_pr("a/r", 1, 5.0, pw.BREACHED), _pr("a/r", 2, 9.5, pw.BREACHED)]
    )
    assert snap.oldest_breach_hrs == pytest.approx(9.5)


def test_oldest_breach_hrs_zero_when_no_breaches():
    snap = ru.TeamSnapshot(label="A", prs=[_pr("a/r", 1, 0.5, pw.FRESH)])
    assert snap.oldest_breach_hrs == 0.0


# ---------------------------------------------------------------------------
# Director-level framing (the reframing requirement)
# ---------------------------------------------------------------------------
def test_digest_contains_awareness_framing_not_blame_language():
    snapshots = [
        ru.TeamSnapshot(label="Payments (EU)", prs=[_pr("a/r", 1, 9.2, pw.BREACHED)]),
        ru.TeamSnapshot(label="Checkout", prs=[_pr("b/r", 2, 0.5, pw.FRESH)]),
    ]
    summary = ru.summarise(snapshots)
    text, blocks = ru.format_landscape_digest(summary, _dt(2025, 10, 13, 9, 15), LON)

    # Present: aggregate/awareness language.
    assert "awareness" in text.lower()
    assert "not a performance ranking" in text.lower()
    assert "Cross-Team Review Landscape" in text

    # Absent: evaluative/blame language and any individual person mention.
    banned = ["slow", "underperform", "blame", "worst", "best team", "@"]
    lowered = text.lower()
    for term in banned:
        assert term not in lowered, f"blame/individual-framing term leaked into digest: {term!r}"

    # Both teams appear, but breached one is not singled out as "bad" —
    # only descriptive counts.
    assert "Payments (EU)" in text
    assert "Checkout" in text


def test_digest_groups_breached_teams_separately_from_others():
    snapshots = [
        ru.TeamSnapshot(label="Payments", prs=[_pr("a/r", 1, 9.2, pw.BREACHED)]),
        ru.TeamSnapshot(label="Checkout", prs=[_pr("b/r", 2, 0.5, pw.FRESH)]),
        ru.TeamSnapshot(label="Growth", prs=[]),
    ]
    summary = ru.summarise(snapshots)
    text, _ = ru.format_landscape_digest(summary, _dt(2025, 10, 13, 9, 15), LON)
    assert "Past SLA" in text
    assert "Everything else" in text
    past_sla_idx = text.index("Past SLA")
    everything_idx = text.index("Everything else")
    payments_idx = text.index("Payments")
    checkout_idx = text.index("Checkout")
    # Payments (breached) appears in the "Past SLA" section, before "Everything else".
    assert past_sla_idx < payments_idx < everything_idx
    # Checkout (not breached) appears after "Everything else".
    assert checkout_idx > everything_idx


def test_digest_blocks_include_header_and_context():
    snapshots = [ru.TeamSnapshot(label="A", prs=[_pr("a/r", 1, 0.2, pw.FRESH)])]
    summary = ru.summarise(snapshots)
    _, blocks = ru.format_landscape_digest(summary, _dt(2025, 10, 13, 9, 15), LON)
    assert blocks[0]["type"] == "header"
    assert any(b["type"] == "context" for b in blocks)


def test_escape_mrkdwn_escapes_ampersand_lt_gt_in_order():
    # Order matters: '&' must be escaped first, or escaping '<'/'>' after
    # would double-escape the '&' those introduce.
    assert ru._escape_mrkdwn("a & b") == "a &amp; b"
    assert ru._escape_mrkdwn("<!channel>") == "&lt;!channel&gt;"
    assert ru._escape_mrkdwn("<b>bold & bad</b>") == "&lt;b&gt;bold &amp; bad&lt;/b&gt;"


def test_digest_escapes_hostile_team_label_not_live_injected():
    """Regression for bug #5: a team label containing Slack mrkdwn control
    sequences (e.g. `<!channel>`) must be escaped before interpolation, not
    passed through verbatim into the posted message."""
    hostile_label = "<!channel> Payments"
    snapshots = [
        ru.TeamSnapshot(label=hostile_label, prs=[_pr("a/r", 1, 9.2, pw.BREACHED)]),
        ru.TeamSnapshot(label="Checkout", prs=[_pr("b/r", 2, 0.5, pw.FRESH)]),
    ]
    summary = ru.summarise(snapshots)
    text, blocks = ru.format_landscape_digest(summary, _dt(2025, 10, 13, 9, 15), LON)

    # The raw, unescaped control sequence must never appear anywhere in the
    # rendered text or Block Kit payload.
    assert "<!channel>" not in text
    for block in blocks:
        assert "<!channel>" not in json.dumps(block)

    # The escaped form is present instead, so the label is still visible.
    assert "&lt;!channel&gt; Payments" in text


def test_summary_to_dict_shape():
    snapshots = [ru.TeamSnapshot(label="A", prs=[_pr("a/r", 1, 5.0, pw.BREACHED)])]
    summary = ru.summarise(snapshots)
    payload = ru.summary_to_dict(summary, _dt(2025, 10, 13, 9, 15))
    assert payload["teams_observed"] == 1
    assert payload["total_breached"] == 1
    assert payload["teams"][0]["label"] == "A"
    assert payload["teams"][0]["breached"] == 1


# ---------------------------------------------------------------------------
# Run cycle
# ---------------------------------------------------------------------------
def test_run_landscape_cycle_dry_run_prints(capsys):
    sources = [ru.TeamSource(label="A", org="acme", team_slug="a")]
    cfg = _rollup_cfg(sources, dry_run=True)

    def fake_fetch(token, source, cfg_, now):
        return [_pr("a/r", 1, 5.0, pw.BREACHED)]

    summary = ru.run_landscape_cycle(cfg, _dt(2025, 10, 13, 9, 15), fetch_fn=fake_fetch)
    out = capsys.readouterr().out
    assert "[DRY_RUN] landscape digest" in out
    assert summary.total_breached == 1


def test_run_landscape_cycle_writes_json_when_configured(monkeypatch, tmp_path):
    out_path = tmp_path / "landscape.json"
    monkeypatch.setenv("PR_WATCH_LANDSCAPE_JSON_OUT", str(out_path))
    sources = [ru.TeamSource(label="A", org="acme", team_slug="a")]
    cfg = _rollup_cfg(sources, dry_run=True)

    def fake_fetch(token, source, cfg_, now):
        return [_pr("a/r", 1, 5.0, pw.BREACHED)]

    ru.run_landscape_cycle(cfg, _dt(2025, 10, 13, 9, 15), fetch_fn=fake_fetch)
    assert out_path.exists()

    import json

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["total_breached"] == 1


def test_run_landscape_cycle_posts_to_slack_when_not_dry_run(monkeypatch):
    sources = [ru.TeamSource(label="A", org="acme", team_slug="a")]
    cfg = _rollup_cfg(sources, dry_run=False)
    captured = {}

    def fake_post(token, channel, text, blocks=None):
        captured["token"] = token
        captured["channel"] = channel
        return {"ok": True}

    monkeypatch.setattr(pw, "slack_post_message", fake_post)

    def fake_fetch(token, source, cfg_, now):
        return [_pr("a/r", 1, 0.2, pw.FRESH)]

    ru.run_landscape_cycle(cfg, _dt(2025, 10, 13, 9, 15), fetch_fn=fake_fetch)
    assert captured["channel"] == "C_DIRECTOR"
