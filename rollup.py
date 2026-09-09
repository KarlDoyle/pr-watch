#!/usr/bin/env python3
"""rollup.py — cross-team, director-level PR landscape digest.

PR Watch's core (`pr_watch.py`) is deliberately single-team: one org, one
team slug, one Slack channel, framed for the team doing the reviewing
("you have a breach, go look"). That framing gets defensive fast when a
manager sees it pointed at their own team.

This module is the other lens: aggregate the *same* underlying data across
MULTIPLE teams into one digest for someone who oversees several of them
(a director, a Head of Engineering, an HR/People partner) — informational,
descriptive, "here's the landscape, worth being aware of", never a
per-team performance ranking or a "who's slow" callout.

Design:
  - Reuses pr_watch.py's fetch/categorisation/business-hours engine
    unchanged (`github_search_prs`, `build_prs`, `categorise`,
    `business_hours_between`) — no duplicated GitHub logic.
  - Adds one layer on top: multiple `TeamSource`s -> `TeamSnapshot`s ->
    one `LandscapeSummary` -> one Slack digest, in aggregate-first language.
  - Standard library only, same as pr_watch.py. No new runtime deps.
  - Additive: does not change pr_watch.py's single-team CLI/polling mode.
    Run standalone: `python3 rollup.py`.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import pr_watch as pw

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Team sources: a JSON array of {"label", "org", "team_slug", "repos"?}.
# Set PR_WATCH_TEAMS_JSON to either an inline JSON array or a path to a
# file containing one. Each source is a distinct org/team pair PR Watch's
# core engine already knows how to query.
PR_WATCH_TEAMS_JSON: str | None = None

# A single shared GitHub PAT reads across every source (needs `read:org`
# on each org in scope). Same var name as pr_watch.py so one token works
# for both if you want it to; kept independent so they can differ.
GH_TOKEN: str | None = None

# Director-level Slack destination — deliberately separate from any
# per-team SLACK_CHANNEL_ID so this never lands in a team's own channel.
SLACK_BOT_TOKEN: str | None = None
LANDSCAPE_SLACK_CHANNEL_ID: str | None = None

TZ_NAME: str | None = None
BUSINESS_START_HOUR: int | None = None
BUSINESS_END_HOUR: int | None = None
HOLIDAYS_YYYYMMDD: str | None = None
SLA_APPROVAL_HRS: float | None = None
APPROACHING_BUFFER_HRS: float | None = None
REVIEW_WINDOWS: str | None = None

DRY_RUN: bool | None = None


class RollupConfigError(Exception):
    """Raised when rollup configuration is missing or invalid."""


@dataclass
class TeamSource:
    label: str  # human-facing name shown in the digest, e.g. "Payments (EU)"
    org: str
    team_slug: str
    repos: list[str] = field(default_factory=list)


@dataclass
class RollupConfig:
    gh_token: str
    sources: list[TeamSource]
    slack_token: str
    slack_channel: str
    tz: ZoneInfo
    business_start_hour: int
    business_end_hour: int
    holidays: frozenset[str]
    sla_approval_hrs: float
    approaching_buffer_hrs: float
    review_windows: list[str]
    dry_run: bool


def _load_sources(raw: str | None) -> list[TeamSource]:
    if not raw:
        raise RollupConfigError(
            "No team sources configured. Set PR_WATCH_TEAMS_JSON to a JSON array "
            'of {"label", "org", "team_slug", "repos"?} objects, or a path to '
            "a file containing one."
        )
    text = raw.strip()
    if not text.startswith("["):
        try:
            with open(text, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise RollupConfigError(
                f"Could not read PR_WATCH_TEAMS_JSON file '{raw}': {exc}"
            ) from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RollupConfigError(f"PR_WATCH_TEAMS_JSON is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list) or not parsed:
        raise RollupConfigError("PR_WATCH_TEAMS_JSON must be a non-empty JSON array.")

    sources: list[TeamSource] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise RollupConfigError(f"Team source #{i} is not an object.")
        missing = [k for k in ("label", "org", "team_slug") if not item.get(k)]
        if missing:
            raise RollupConfigError(f"Team source #{i} missing: {', '.join(missing)}.")
        sources.append(
            TeamSource(
                label=str(item["label"]),
                org=str(item["org"]),
                team_slug=str(item["team_slug"]),
                repos=[str(r) for r in item.get("repos", [])],
            )
        )
    return sources


def load_rollup_config() -> RollupConfig:
    gh_token = pw._resolve(GH_TOKEN, "GH_TOKEN")
    slack_token = pw._resolve(SLACK_BOT_TOKEN, "SLACK_BOT_TOKEN")
    slack_channel = pw._resolve(LANDSCAPE_SLACK_CHANNEL_ID, "LANDSCAPE_SLACK_CHANNEL_ID")

    missing = []
    if not gh_token:
        missing.append("GH_TOKEN")
    if not slack_token:
        missing.append("SLACK_BOT_TOKEN")
    if not slack_channel:
        missing.append("LANDSCAPE_SLACK_CHANNEL_ID")
    if missing:
        raise RollupConfigError("Missing required configuration: " + ", ".join(missing))

    sources = _load_sources(pw._resolve(PR_WATCH_TEAMS_JSON, "PR_WATCH_TEAMS_JSON"))

    tz_name = pw._resolve(TZ_NAME, "TZ_NAME", "Europe/London") or "Europe/London"
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # noqa: BLE001
        raise RollupConfigError(f"Unknown TZ_NAME '{tz_name}': {exc}") from exc

    return RollupConfig(
        gh_token=gh_token,
        sources=sources,
        slack_token=slack_token,
        slack_channel=slack_channel,
        tz=tz,
        business_start_hour=int(pw._resolve(BUSINESS_START_HOUR, "BUSINESS_START_HOUR", "9")),
        business_end_hour=int(pw._resolve(BUSINESS_END_HOUR, "BUSINESS_END_HOUR", "17")),
        holidays=frozenset(pw._csv(pw._resolve(HOLIDAYS_YYYYMMDD, "HOLIDAYS_YYYYMMDD"))),
        sla_approval_hrs=float(pw._resolve(SLA_APPROVAL_HRS, "SLA_APPROVAL_HRS", "4.0")),
        approaching_buffer_hrs=float(
            pw._resolve(APPROACHING_BUFFER_HRS, "APPROACHING_BUFFER_HRS", "1.0")
        ),
        review_windows=pw._csv(pw._resolve(REVIEW_WINDOWS, "REVIEW_WINDOWS", "09:15")),
        dry_run=pw._as_bool(pw._resolve(DRY_RUN, "DRY_RUN"), False),
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
@dataclass
class TeamSnapshot:
    label: str
    prs: list[pw.PR]

    @property
    def breached(self) -> list[pw.PR]:
        return [p for p in self.prs if p.category == pw.BREACHED]

    @property
    def approaching(self) -> list[pw.PR]:
        return [p for p in self.prs if p.category == pw.APPROACHING]

    @property
    def fresh(self) -> list[pw.PR]:
        return [p for p in self.prs if p.category == pw.FRESH]

    @property
    def oldest_breach_hrs(self) -> float:
        breached = self.breached
        return max((p.elapsed_hrs for p in breached), default=0.0)


FetchFn = Callable[[str, TeamSource, "RollupConfig", datetime], list[pw.PR]]


def fetch_team_prs(
    gh_token: str, source: TeamSource, cfg: RollupConfig, now: datetime
) -> list[pw.PR]:
    """Default fetch: reuse pr_watch's own Search API + enrichment, unchanged."""
    items = pw.github_search_prs(gh_token, source.org, source.team_slug, source.repos)
    team_cfg = pw.Config(
        gh_token=gh_token,
        gh_org=source.org,
        team_slug=source.team_slug,
        gh_repos=source.repos,
        slack_token="",
        slack_channel="",
        pagerduty_token=None,
        pagerduty_schedule_id=None,
        pd_to_slack={},
        oncall_fallback="",
        tz=cfg.tz,
        tz_name=cfg.tz.key,
        business_start_hour=cfg.business_start_hour,
        business_end_hour=cfg.business_end_hour,
        holidays=cfg.holidays,
        review_windows=cfg.review_windows,
        sla_response_hrs=0.0,
        sla_approval_hrs=cfg.sla_approval_hrs,
        approaching_buffer_hrs=cfg.approaching_buffer_hrs,
        send_daily_digest=False,
        dry_run=True,
        state_file="",
    )
    return pw.build_prs(gh_token, team_cfg, now, items)


def aggregate_landscape(
    cfg: RollupConfig, now: datetime, fetch_fn: FetchFn = fetch_team_prs
) -> list[TeamSnapshot]:
    """Fetch every configured team source and return one snapshot each.

    A single source failing to fetch does not take down the whole rollup —
    it is reported with zero PRs and a note is left for the caller via
    normal exception propagation only when *every* source fails.
    """
    snapshots: list[TeamSnapshot] = []
    errors: list[str] = []
    for source in cfg.sources:
        try:
            prs = fetch_fn(cfg.gh_token, source, cfg, now)
        except Exception as exc:  # noqa: BLE001 - one bad source must not sink the rollup
            print(f"[warn] rollup: could not fetch '{source.label}': {exc}")
            errors.append(source.label)
            prs = []
        snapshots.append(TeamSnapshot(label=source.label, prs=prs))
    if errors and len(errors) == len(cfg.sources):
        raise RollupConfigError(f"Every team source failed to fetch: {', '.join(errors)}")
    return snapshots


@dataclass
class LandscapeSummary:
    snapshots: list[TeamSnapshot]

    @property
    def teams_observed(self) -> int:
        return len(self.snapshots)

    @property
    def teams_with_breaches(self) -> list[TeamSnapshot]:
        return [s for s in self.snapshots if s.breached]

    @property
    def total_breached(self) -> int:
        return sum(len(s.breached) for s in self.snapshots)

    @property
    def total_approaching(self) -> int:
        return sum(len(s.approaching) for s in self.snapshots)

    @property
    def total_fresh(self) -> int:
        return sum(len(s.fresh) for s in self.snapshots)


def summarise(snapshots: list[TeamSnapshot]) -> LandscapeSummary:
    return LandscapeSummary(snapshots=snapshots)


def summary_to_dict(summary: LandscapeSummary, now: datetime) -> dict:
    """JSON-serialisable snapshot of the landscape, for consumers that are
    not Slack — e.g. the Atlassian Forge dashboard gadget in `forge_app/`,
    which has no direct access to this process and instead fetches this
    payload over HTTPS from wherever you choose to publish it (see
    `forge_app/README.md`). Counts only — no PR titles/authors/URLs, since
    the framing here is aggregate awareness, not a per-item list.
    """
    return {
        "generated_at": now.isoformat(),
        "teams_observed": summary.teams_observed,
        "teams_with_breaches": len(summary.teams_with_breaches),
        "total_breached": summary.total_breached,
        "total_approaching": summary.total_approaching,
        "total_fresh": summary.total_fresh,
        "teams": [
            {
                "label": snap.label,
                "breached": len(snap.breached),
                "approaching": len(snap.approaching),
                "fresh": len(snap.fresh),
                "oldest_breach_hrs": round(snap.oldest_breach_hrs, 2),
            }
            for snap in summary.snapshots
        ],
    }


# ---------------------------------------------------------------------------
# Director-level framing
# ---------------------------------------------------------------------------
# Deliberately awareness language, not enforcement language: no "slow",
# "underperforming", "blame", or named-individual call-outs. Aggregate
# counts and a plain landscape view, framed as something worth noticing —
# not a scoreboard.
FRAMING_NOTE = (
    "This is an awareness digest, not a performance ranking — flow varies with "
    "team size, review load and time zone. Useful for spotting where support "
    "might help, not for judging who's behind."
)


def _escape_mrkdwn(value: str) -> str:
    """Escape a string that will be interpolated into Slack mrkdwn.

    Team labels come from `PR_WATCH_TEAMS_JSON` config, which this process
    does not otherwise treat as trusted input — without this, a label like
    `<!channel>` would live-inject a Slack mention/formatting control
    sequence into the posted message. Order matters: `&` must be escaped
    first, or escaping `<`/`>` afterwards would double-escape the `&` just
    introduced.
    """
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_landscape_digest(
    summary: LandscapeSummary, now: datetime, tz: ZoneInfo
) -> tuple[str, list[dict]]:
    """Build the cross-team landscape digest (text + Block Kit)."""
    stamp = now.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    with_breaches = summary.teams_with_breaches

    lines = [
        f"🗺️ Cross-Team Review Landscape — {stamp}",
        f"Teams observed: {summary.teams_observed} · "
        f"Teams with items past SLA: {len(with_breaches)}",
        "",
        FRAMING_NOTE,
        "",
    ]

    if with_breaches:
        lines.append("Past SLA (worth being aware of)")
        for snap in sorted(with_breaches, key=lambda s: s.oldest_breach_hrs, reverse=True):
            lines.append(
                f"- {_escape_mrkdwn(snap.label)}: {len(snap.breached)} item(s) waiting, "
                f"oldest {snap.oldest_breach_hrs:.1f}h"
            )
        lines.append("")

    others = [s for s in summary.snapshots if s not in with_breaches]
    if others:
        lines.append("Everything else")
        for snap in sorted(others, key=lambda s: s.label):
            lines.append(
                f"- {_escape_mrkdwn(snap.label)}: 0 past SLA, {len(snap.approaching)} approaching, "
                f"{len(snap.fresh)} fresh"
            )
        lines.append("")

    text = "\n".join(lines).rstrip()

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Cross-Team Review Landscape — {stamp}"},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Teams observed: {summary.teams_observed} · "
                        f"Teams with items past SLA: {len(with_breaches)}"
                    ),
                }
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"_{FRAMING_NOTE}_"}},
    ]
    if with_breaches:
        body = "\n".join(
            f"• *{_escape_mrkdwn(snap.label)}* — {len(snap.breached)} item(s) waiting, "
            f"oldest {snap.oldest_breach_hrs:.1f}h"
            for snap in sorted(with_breaches, key=lambda s: s.oldest_breach_hrs, reverse=True)
        )
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Past SLA*\n{body}"}}
        )
    if others:
        body = "\n".join(
            f"• {_escape_mrkdwn(snap.label)} — 0 past SLA, {len(snap.approaching)} approaching, "
            f"{len(snap.fresh)} fresh"
            for snap in sorted(others, key=lambda s: s.label)
        )
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Everything else*\n{body}"}}
        )
    return text, blocks


# ---------------------------------------------------------------------------
# Run cycle
# ---------------------------------------------------------------------------
def run_landscape_cycle(cfg: RollupConfig, now: datetime, fetch_fn: FetchFn = fetch_team_prs):
    """Fetch every team source, aggregate, and post the landscape digest."""
    snapshots = aggregate_landscape(cfg, now, fetch_fn)
    summary = summarise(snapshots)
    text, blocks = format_landscape_digest(summary, now, cfg.tz)
    if cfg.dry_run:
        print(f"--- [DRY_RUN] landscape digest ---\n{text}\n")
    else:
        pw.slack_post_message(cfg.slack_token, cfg.slack_channel, text, blocks)

    json_out = os.getenv("PR_WATCH_LANDSCAPE_JSON_OUT")
    if json_out:
        with open(json_out, "w", encoding="utf-8") as handle:
            json.dump(summary_to_dict(summary, now), handle, indent=2, sort_keys=True)

    return summary


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = load_rollup_config()
    except RollupConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    now = datetime.now(cfg.tz)
    try:
        summary = run_landscape_cycle(cfg, now)
    except RollupConfigError as exc:
        print(f"Rollup error: {exc}", file=sys.stderr)
        return 1
    print(
        f"PR Watch rollup: {summary.teams_observed} team(s) observed, "
        f"{summary.total_breached} past SLA, {summary.total_approaching} approaching, "
        f"{summary.total_fresh} fresh."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
