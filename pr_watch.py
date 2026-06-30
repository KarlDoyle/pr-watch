#!/usr/bin/env python3
"""PR Watch — keep cross-team pull requests moving.

Watches open GitHub PRs where a team is explicitly requested to review,
computes how long each has been waiting in *business hours* since the latest
team review-request, and:

  1. Posts real-time Slack SLA-breach alerts (deduped via a local JSON state).
  2. Posts daily digests at configured review windows
     (Breached / Approaching / Fresh sections).
  3. Resolves the current on-call from PagerDuty and maps to a Slack ID,
     falling back to a static group mention.

Read-only against GitHub — it never merges or approves anything.

Standard library only (Python 3.10+): urllib for HTTP, zoneinfo for
timezones, json for the state file. No third-party dependencies.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Configuration
#
# Precedence for every setting:
#   top-of-file constant (if truthy) -> matching environment variable
#   -> default (for non-secret settings) -> error-and-exit (required secrets).
#
# Leave the secret constants as None to source them from the environment.
# NEVER commit real tokens here.
# ---------------------------------------------------------------------------

# GitHub --------------------------------------------------------------------
GH_TOKEN: str | None = None  # PAT with `repo`, `read:org`
GH_ORG: str | None = None  # organisation login, e.g. "acme-corp"
TEAM_SLUG: str | None = None  # team slug, e.g. "your-team"
GH_REPOS: str | None = None  # optional CSV of repo names to limit scope

# Slack ---------------------------------------------------------------------
SLACK_BOT_TOKEN: str | None = None  # "xoxb-..."
SLACK_CHANNEL_ID: str | None = None  # channel ID, e.g. "C0123456789"

# PagerDuty (optional — falls back to a static mention) ---------------------
PAGERDUTY_API_TOKEN: str | None = None
PAGERDUTY_SCHEDULE_ID: str | None = None
PD_TO_SLACK_JSON: str | None = None  # path to JSON file, or inline JSON object
ONCALL_FALLBACK_MENTION: str | None = None  # default "@oncall"

# Business hours & windows --------------------------------------------------
TZ_NAME: str | None = None  # default "Europe/London"
BUSINESS_START_HOUR: int | None = None  # default 9
BUSINESS_END_HOUR: int | None = None  # default 17
HOLIDAYS_YYYYMMDD: str | None = None  # CSV like "20250101,20250317"
REVIEW_WINDOWS: str | None = None  # CSV like "09:15,15:00"

# SLA targets (hours) -------------------------------------------------------
SLA_RESPONSE_HRS: float | None = None  # default 2.0
SLA_APPROVAL_HRS: float | None = None  # default 4.0
APPROACHING_BUFFER_HRS: float | None = None  # default 1.0

# Behaviour flags -----------------------------------------------------------
SEND_DAILY_DIGEST: bool | None = None  # default True
DRY_RUN: bool | None = None  # default False

# State ---------------------------------------------------------------------
STATE_FILE = os.getenv("PR_WATCH_STATE_FILE", "pr_watch_state.json")
STATE_VERSION = 1

# HTTP / retry --------------------------------------------------------------
RATE_LIMIT_MAX_RETRIES = 4
RATE_LIMIT_MAX_BACKOFF_S = 30.0
HTTP_TIMEOUT_S = 20


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------
def _const(value: object) -> str | None:
    """Treat empty strings / None constants as 'unset'."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve(constant: object, env_name: str, default: str | None = None) -> str | None:
    """constant -> env -> default. Returns a string or None."""
    chosen = _const(constant)
    if chosen is not None:
        return chosen
    env_val = os.getenv(env_name)
    if env_val is not None and env_val.strip() != "":
        return env_val.strip()
    return default


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Config:
    gh_token: str
    gh_org: str
    team_slug: str
    gh_repos: list[str]
    slack_token: str
    slack_channel: str
    pagerduty_token: str | None
    pagerduty_schedule_id: str | None
    pd_to_slack: dict[str, str]
    oncall_fallback: str
    tz: ZoneInfo
    tz_name: str
    business_start_hour: int
    business_end_hour: int
    holidays: frozenset[str]
    review_windows: list[str]
    sla_response_hrs: float
    sla_approval_hrs: float
    approaching_buffer_hrs: float
    send_daily_digest: bool
    dry_run: bool
    state_file: str


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def _load_pd_mapping(raw: str | None) -> dict[str, str]:
    """Parse PD_TO_SLACK_JSON: either inline JSON or a path to a JSON file."""
    if not raw:
        return {}
    text = raw.strip()
    if not text.startswith("{"):
        # Treat as a filesystem path.
        try:
            with open(text, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise ConfigError(f"Could not read PD_TO_SLACK_JSON file '{raw}': {exc}") from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"PD_TO_SLACK_JSON is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError("PD_TO_SLACK_JSON must be a JSON object of email -> Slack ID.")
    return {str(k): str(v) for k, v in parsed.items()}


def load_config() -> Config:
    """Resolve every setting and validate required secrets. Exits on failure."""
    gh_token = _resolve(GH_TOKEN, "GH_TOKEN")
    gh_org = _resolve(GH_ORG, "GH_ORG")
    team_slug = _resolve(TEAM_SLUG, "TEAM_SLUG", "your-team")
    slack_token = _resolve(SLACK_BOT_TOKEN, "SLACK_BOT_TOKEN")
    slack_channel = _resolve(SLACK_CHANNEL_ID, "SLACK_CHANNEL_ID")

    missing: list[str] = []
    if not gh_token:
        missing.append("GH_TOKEN")
    if not gh_org:
        missing.append("GH_ORG")
    if not slack_token:
        missing.append("SLACK_BOT_TOKEN")
    if not slack_channel:
        missing.append("SLACK_CHANNEL_ID")
    if missing:
        raise ConfigError(
            "Missing required configuration: "
            + ", ".join(missing)
            + ". Set each as a top-of-file constant or environment variable."
        )

    tz_name = _resolve(TZ_NAME, "TZ_NAME", "Europe/London") or "Europe/London"
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # noqa: BLE001 - surface a clean message for any tz error
        raise ConfigError(f"Unknown TZ_NAME '{tz_name}': {exc}") from exc

    start_hour = int(_resolve(BUSINESS_START_HOUR, "BUSINESS_START_HOUR", "9"))
    end_hour = int(_resolve(BUSINESS_END_HOUR, "BUSINESS_END_HOUR", "17"))
    if not 0 <= start_hour < end_hour <= 24:
        raise ConfigError(
            f"Invalid business hours: start={start_hour}, end={end_hour} "
            "(need 0 <= start < end <= 24)."
        )

    pagerduty_token = _resolve(PAGERDUTY_API_TOKEN, "PAGERDUTY_API_TOKEN")
    pagerduty_schedule = _resolve(PAGERDUTY_SCHEDULE_ID, "PAGERDUTY_SCHEDULE_ID")
    pd_mapping = _load_pd_mapping(_resolve(PD_TO_SLACK_JSON, "PD_TO_SLACK_JSON"))

    return Config(
        gh_token=gh_token,
        gh_org=gh_org,
        team_slug=team_slug,
        gh_repos=_csv(_resolve(GH_REPOS, "GH_REPOS")),
        slack_token=slack_token,
        slack_channel=slack_channel,
        pagerduty_token=pagerduty_token,
        pagerduty_schedule_id=pagerduty_schedule,
        pd_to_slack=pd_mapping,
        oncall_fallback=_resolve(ONCALL_FALLBACK_MENTION, "ONCALL_FALLBACK_MENTION", "@oncall")
        or "@oncall",
        tz=tz,
        tz_name=tz_name,
        business_start_hour=start_hour,
        business_end_hour=end_hour,
        holidays=frozenset(_csv(_resolve(HOLIDAYS_YYYYMMDD, "HOLIDAYS_YYYYMMDD"))),
        review_windows=_csv(_resolve(REVIEW_WINDOWS, "REVIEW_WINDOWS", "09:15,15:00")),
        sla_response_hrs=float(_resolve(SLA_RESPONSE_HRS, "SLA_RESPONSE_HRS", "2.0")),
        sla_approval_hrs=float(_resolve(SLA_APPROVAL_HRS, "SLA_APPROVAL_HRS", "4.0")),
        approaching_buffer_hrs=float(
            _resolve(APPROACHING_BUFFER_HRS, "APPROACHING_BUFFER_HRS", "1.0")
        ),
        send_daily_digest=_as_bool(_resolve(SEND_DAILY_DIGEST, "SEND_DAILY_DIGEST"), True),
        dry_run=_as_bool(_resolve(DRY_RUN, "DRY_RUN"), False),
        state_file=STATE_FILE,
    )


# ---------------------------------------------------------------------------
# Business-hours engine (pure functions)
# ---------------------------------------------------------------------------
def _parse_iso(ts: str) -> datetime:
    """Parse a GitHub/PagerDuty ISO-8601 timestamp into an aware datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def business_hours_between(
    start: datetime,
    end: datetime,
    *,
    tz: ZoneInfo,
    start_hour: int,
    end_hour: int,
    holidays: frozenset[str] = frozenset(),
) -> float:
    """Business hours elapsed between two aware datetimes.

    Counts only ``start_hour``-``end_hour`` on Mon-Fri, excluding any date in
    ``holidays`` (``YYYYMMDD`` strings). Uses a precise per-day overlap so a
    request mid-window and a "now" mid-window are both handled exactly.
    """
    if end <= start:
        return 0.0

    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)

    total_seconds = 0.0
    day = local_start.date()
    last_day = local_end.date()
    while day <= last_day:
        is_weekday = day.weekday() < 5
        is_holiday = day.strftime("%Y%m%d") in holidays
        if is_weekday and not is_holiday:
            window_open = datetime(day.year, day.month, day.day, start_hour, tzinfo=tz)
            window_close = datetime(day.year, day.month, day.day, end_hour, tzinfo=tz)
            segment_start = max(local_start, window_open)
            segment_end = min(local_end, window_close)
            if segment_end > segment_start:
                total_seconds += (segment_end - segment_start).total_seconds()
        day += timedelta(days=1)

    return total_seconds / 3600.0


# ---------------------------------------------------------------------------
# Categorisation
# ---------------------------------------------------------------------------
BREACHED = "breached"
APPROACHING = "approaching"
FRESH = "fresh"


def categorise(elapsed_hrs: float, sla_approval_hrs: float, buffer_hrs: float = 1.0) -> str:
    """Bucket a PR by business-hours elapsed.

    Breached:    elapsed >= sla_approval_hrs
    Approaching: sla_approval_hrs - buffer <= elapsed < sla_approval_hrs
    Fresh:       everything else
    """
    if elapsed_hrs >= sla_approval_hrs:
        return BREACHED
    if elapsed_hrs >= sla_approval_hrs - buffer_hrs:
        return APPROACHING
    return FRESH


# ---------------------------------------------------------------------------
# PR model
# ---------------------------------------------------------------------------
@dataclass
class PR:
    repo: str  # "acme-corp/checkout"
    number: int
    title: str
    author: str
    url: str
    branch: str
    requested_at: datetime
    elapsed_hrs: float = 0.0
    category: str = FRESH

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def requested_at_iso(self) -> str:
        return self.requested_at.isoformat()


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------
def load_state(path: str) -> dict:
    """Load the dedup state, returning a fresh structure on any problem."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"version": STATE_VERSION, "alerted": {}}
    if not isinstance(data, dict) or "alerted" not in data:
        return {"version": STATE_VERSION, "alerted": {}}
    data.setdefault("version", STATE_VERSION)
    if not isinstance(data["alerted"], dict):
        data["alerted"] = {}
    return data


def save_state(path: str, state: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)


def needs_alert(state: dict, key: str, requested_at_iso: str) -> bool:
    """True when this PR has not yet been alerted for its current request.

    A *new* review-request (different ``requested_at``) re-arms the alert.
    """
    record = state["alerted"].get(key)
    if record is None:
        return True
    return record.get("requested_at") != requested_at_iso


def mark_alerted(state: dict, pr: PR, alerted_at: datetime) -> None:
    state["alerted"][pr.key] = {
        "requested_at": pr.requested_at_iso,
        "elapsed_h": round(pr.elapsed_hrs, 2),
        "alerted_at": alerted_at.isoformat(),
    }


def prune_state(state: dict, open_keys: set[str]) -> list[str]:
    """Drop alert records for PRs that are no longer open (closed/merged)."""
    stale = [key for key in state["alerted"] if key not in open_keys]
    for key in stale:
        del state["alerted"][key]
    return stale


# ---------------------------------------------------------------------------
# Digest ordering
# ---------------------------------------------------------------------------
def order_digest(prs: list[PR]) -> tuple[list[PR], list[PR], list[PR]]:
    """Split and order PRs for the digest.

    - Breached:    oldest first  (longest-waiting -> highest elapsed first)
    - Approaching: closest to breach first (highest elapsed first)
    - Fresh:       newest first  (least-waiting -> lowest elapsed first)
    """
    breached = sorted(
        (p for p in prs if p.category == BREACHED), key=lambda p: p.elapsed_hrs, reverse=True
    )
    approaching = sorted(
        (p for p in prs if p.category == APPROACHING), key=lambda p: p.elapsed_hrs, reverse=True
    )
    fresh = sorted((p for p in prs if p.category == FRESH), key=lambda p: p.elapsed_hrs)
    return breached, approaching, fresh


# ---------------------------------------------------------------------------
# Review-window check
# ---------------------------------------------------------------------------
def is_review_window(now: datetime, windows: list[str]) -> bool:
    """True when ``now`` (local) matches a configured HH:MM window, to the minute."""
    return now.strftime("%H:%M") in set(windows)


# ---------------------------------------------------------------------------
# HTTP helpers (urllib + backoff)
# ---------------------------------------------------------------------------
def _sleep(seconds: float) -> None:
    """Indirection point so tests can patch out real sleeping."""
    time.sleep(seconds)


def _backoff_seconds(retry_after: str | None, attempt: int) -> float:
    """Retry-After header wins (capped); otherwise exponential 2**attempt (capped)."""
    if retry_after:
        try:
            return min(float(retry_after), RATE_LIMIT_MAX_BACKOFF_S)
        except ValueError:
            pass
    return min(float(2**attempt), RATE_LIMIT_MAX_BACKOFF_S)


def _is_rate_limited(exc: HTTPError) -> bool:
    if exc.code == 429:
        return True
    if exc.code == 403:
        remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
        return remaining == "0"
    return False


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict | None = None,
) -> dict:
    """Perform an HTTP request and return parsed JSON, retrying on rate limits.

    Tokens live only in headers and are never logged.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req_headers = dict(headers or {})
    if data is not None:
        req_headers.setdefault("Content-Type", "application/json")

    attempt = 0
    while True:
        request = Request(url, data=data, headers=req_headers, method=method)
        try:
            with urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
                raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as exc:
            if _is_rate_limited(exc) and attempt < RATE_LIMIT_MAX_RETRIES:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                _sleep(_backoff_seconds(retry_after, attempt))
                attempt += 1
                continue
            raise
        except URLError:
            if attempt < RATE_LIMIT_MAX_RETRIES:
                _sleep(_backoff_seconds(None, attempt) + random.uniform(0, 0.3))
                attempt += 1
                continue
            raise


# ---------------------------------------------------------------------------
# GitHub layer
# ---------------------------------------------------------------------------
GITHUB_API = "https://api.github.com"


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pr-watch",
    }


def github_search_prs(
    token: str, org: str, team_slug: str, repos: list[str] | None = None
) -> list[dict]:
    """Search open PRs where the team is requested to review.

    Uses the Search API qualifier ``team-review-requested:ORG/TEAM``.
    Returns the raw ``items`` from every page.
    """
    qualifiers = [
        "type:pr",
        "state:open",
        f"team-review-requested:{org}/{team_slug}",
    ]
    for repo in repos or []:
        qualifiers.append(f"repo:{org}/{repo}")
    query = " ".join(qualifiers)

    items: list[dict] = []
    page = 1
    while True:
        params = urlencode({"q": query, "per_page": 100, "page": page})
        data = http_json(f"{GITHUB_API}/search/issues?{params}", headers=_gh_headers(token))
        batch = data.get("items", [])
        items.extend(batch)
        if len(batch) < 100 or len(items) >= int(data.get("total_count", len(items))):
            break
        page += 1
    return items


def _repo_full_name_from_issue(item: dict) -> str:
    """Derive 'owner/repo' from a search result's repository_url."""
    repo_url = item.get("repository_url", "")
    # https://api.github.com/repos/{owner}/{repo}
    return repo_url.split("/repos/", 1)[-1] if "/repos/" in repo_url else ""


def github_latest_team_request(
    token: str, repo_full_name: str, number: int, team_slug: str
) -> datetime | None:
    """Find the latest ``review_requested`` event aimed at the team.

    Reads the issue events timeline and returns the most recent
    ``created_at`` where the requested reviewer is the team.
    """
    latest: datetime | None = None
    page = 1
    while True:
        params = urlencode({"per_page": 100, "page": page})
        url = f"{GITHUB_API}/repos/{repo_full_name}/issues/{number}/events?{params}"
        events = http_json(url, headers=_gh_headers(token))
        if not isinstance(events, list):
            break
        for event in events:
            if event.get("event") != "review_requested":
                continue
            team = event.get("requested_team") or {}
            if team.get("slug") != team_slug:
                continue
            created = event.get("created_at")
            if not created:
                continue
            when = _parse_iso(created)
            if latest is None or when > latest:
                latest = when
        if len(events) < 100:
            break
        page += 1
    return latest


def build_prs(token: str, cfg: Config, now: datetime, items: list[dict]) -> list[PR]:
    """Turn raw search items into enriched, categorised PR objects."""
    prs: list[PR] = []
    for item in items:
        repo_full = _repo_full_name_from_issue(item)
        number = int(item.get("number", 0))
        if not repo_full or not number:
            continue
        requested_at = github_latest_team_request(token, repo_full, number, cfg.team_slug)
        if requested_at is None:
            # No team request event found (e.g. requested via UI only) — fall
            # back to the PR's created time so it is still tracked.
            requested_at = _parse_iso(item["created_at"])
        elapsed = business_hours_between(
            requested_at,
            now,
            tz=cfg.tz,
            start_hour=cfg.business_start_hour,
            end_hour=cfg.business_end_hour,
            holidays=cfg.holidays,
        )
        pr = PR(
            repo=repo_full,
            number=number,
            title=item.get("title", ""),
            author=(item.get("user") or {}).get("login", "unknown"),
            url=item.get("html_url", ""),
            branch=((item.get("pull_request") or {}).get("head") or {}).get("ref", ""),
            requested_at=requested_at,
            elapsed_hrs=elapsed,
        )
        pr.category = categorise(elapsed, cfg.sla_approval_hrs, cfg.approaching_buffer_hrs)
        prs.append(pr)
    return prs


# ---------------------------------------------------------------------------
# PagerDuty layer + on-call resolution
# ---------------------------------------------------------------------------
PAGERDUTY_API = "https://api.pagerduty.com"


def pagerduty_oncall_email(token: str, schedule_id: str, now: datetime) -> str | None:
    """Return the email of the user currently on call for a schedule."""
    params = urlencode(
        {
            "schedule_ids[]": schedule_id,
            "since": now.isoformat(),
            "until": now.isoformat(),
            "earliest": "true",
        }
    )
    headers = {
        "Authorization": f"Token token={token}",
        "Accept": "application/vnd.pagerduty+json;version=2",
        "User-Agent": "pr-watch",
    }
    data = http_json(f"{PAGERDUTY_API}/oncalls?{params}", headers=headers)
    oncalls = data.get("oncalls", [])
    if not oncalls:
        return None
    # Lowest escalation level == primary on-call.
    primary = min(oncalls, key=lambda o: o.get("escalation_level", 99))
    return (primary.get("user") or {}).get("email")


def pd_to_slack_mention(email: str | None, mapping: dict[str, str], fallback: str) -> str:
    """Map a PagerDuty email to a Slack mention, else the static fallback."""
    if email and email in mapping:
        return f"<@{mapping[email]}>"
    return fallback


def resolve_oncall_mention(cfg: Config, now: datetime) -> str:
    """Resolve the on-call Slack mention, tolerating any PagerDuty failure."""
    if not cfg.pagerduty_token or not cfg.pagerduty_schedule_id:
        return cfg.oncall_fallback
    try:
        email = pagerduty_oncall_email(cfg.pagerduty_token, cfg.pagerduty_schedule_id, now)
    except (HTTPError, URLError, ValueError) as exc:
        print(f"[warn] PagerDuty lookup failed ({type(exc).__name__}); using fallback mention.")
        return cfg.oncall_fallback
    return pd_to_slack_mention(email, cfg.pd_to_slack, cfg.oncall_fallback)


# ---------------------------------------------------------------------------
# Slack layer
# ---------------------------------------------------------------------------
SLACK_API = "https://slack.com/api"


def slack_post_message(
    token: str, channel: str, text: str, blocks: list[dict] | None = None
) -> dict:
    """Post a message via chat.postMessage."""
    body: dict = {"channel": channel, "text": text}
    if blocks:
        body["blocks"] = blocks
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "pr-watch",
    }
    data = http_json(f"{SLACK_API}/chat.postMessage", method="POST", headers=headers, body=body)
    if not data.get("ok", False):
        print(f"[warn] Slack chat.postMessage error: {data.get('error', 'unknown')}")
    return data


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------
def _fmt_hours(hrs: float) -> str:
    return f"{hrs:.1f}h"


def format_breach_alert(
    pr: PR, oncall_mention: str, sla_hrs: float, tz: ZoneInfo
) -> tuple[str, list[dict]]:
    """Build the real-time SLA-breach alert (text + Block Kit)."""
    requested_local = pr.requested_at.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    text = (
        f"🚨 Review needed  ·  {pr.repo} → {pr.branch or 'main'}\n"
        f"PR #{pr.number}: {pr.title}  •  by {pr.author}  •  "
        f"{_fmt_hours(pr.elapsed_hrs)} since request (business hours)\n"
        f"Requested: {requested_local} • SLA {sla_hrs:g}h\n"
        f"{oncall_mention} (on-call) please review now"
    )
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🚨 *Review needed*  ·  `{pr.repo}` → `{pr.branch or 'main'}`\n"
                    f"*<{pr.url}|PR #{pr.number}: {pr.title}>*  •  by {pr.author}  •  "
                    f"*{_fmt_hours(pr.elapsed_hrs)}* since request (business hours)"
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Requested: {requested_local} • SLA {sla_hrs:g}h • "
                    f"{oncall_mention} (on-call) please review now",
                }
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open PR"},
                    "url": pr.url,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Files"},
                    "url": f"{pr.url}/files",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "url": f"{pr.url}/files#submit-review",
                },
            ],
        },
    ]
    return text, blocks


def _digest_line(pr: PR) -> str:
    return f"- {pr.repo} #{pr.number} · {pr.title} · {_fmt_hours(pr.elapsed_hrs)} waiting"


def format_digest(
    breached: list[PR],
    approaching: list[PR],
    fresh: list[PR],
    oncall_mention: str,
    now: datetime,
    tz: ZoneInfo,
) -> tuple[str, list[dict]]:
    """Build the daily review digest (text + Block Kit)."""
    stamp = now.astimezone(tz).strftime("%H:%M")
    lines = [f"📋 Daily Review Queue — {stamp}", f"On-Call: {oncall_mention}", ""]

    def section(title: str, prs: list[PR]) -> None:
        lines.append(title)
        if prs:
            lines.extend(_digest_line(pr) for pr in prs)
        else:
            lines.append("- (none)")
        lines.append("")

    section("🚨 Breached SLA", breached)
    section("⏳ Approaching SLA", approaching)
    section("🆕 Fresh PRs", fresh)
    text = "\n".join(lines).rstrip()

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Daily Review Queue — {stamp}"},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"On-Call: {oncall_mention}"}],
        },
    ]
    for title, prs in (
        ("🚨 *Breached SLA*", breached),
        ("⏳ *Approaching SLA*", approaching),
        ("🆕 *Fresh PRs*", fresh),
    ):
        body = (
            "\n".join(
                f"• <{p.url}|{p.repo} #{p.number}> · {p.title} "
                f"· {_fmt_hours(p.elapsed_hrs)} waiting"
                for p in prs
            )
            or "_(none)_"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{title}\n{body}"}})
    return text, blocks


# ---------------------------------------------------------------------------
# Run cycle
# ---------------------------------------------------------------------------
@dataclass
class RunResult:
    prs: list[PR] = field(default_factory=list)
    alerts_sent: list[str] = field(default_factory=list)
    digest_sent: bool = False
    pruned: list[str] = field(default_factory=list)


def _emit(cfg: Config, text: str, blocks: list[dict], label: str) -> None:
    """Post to Slack, or print when DRY_RUN."""
    if cfg.dry_run:
        print(f"--- [DRY_RUN] {label} ---\n{text}\n")
        return
    slack_post_message(cfg.slack_token, cfg.slack_channel, text, blocks)


def run_cycle(cfg: Config, now: datetime) -> RunResult:
    """Execute one watch cycle. ``now`` is injected for determinism/testing."""
    result = RunResult()

    items = github_search_prs(cfg.gh_token, cfg.gh_org, cfg.team_slug, cfg.gh_repos)
    prs = build_prs(cfg.gh_token, cfg, now, items)
    result.prs = prs

    state = load_state(cfg.state_file)
    open_keys = {pr.key for pr in prs}
    result.pruned = prune_state(state, open_keys)

    oncall = resolve_oncall_mention(cfg, now)

    # Real-time breach alerts (deduped).
    for pr in prs:
        if pr.category != BREACHED:
            continue
        if needs_alert(state, pr.key, pr.requested_at_iso):
            text, blocks = format_breach_alert(pr, oncall, cfg.sla_approval_hrs, cfg.tz)
            _emit(cfg, text, blocks, f"breach alert {pr.key}")
            mark_alerted(state, pr, now)
            result.alerts_sent.append(pr.key)

    save_state(cfg.state_file, state)

    # Daily digest at review windows.
    if cfg.send_daily_digest and is_review_window(now, cfg.review_windows):
        breached, approaching, fresh = order_digest(prs)
        text, blocks = format_digest(breached, approaching, fresh, oncall, now, cfg.tz)
        _emit(cfg, text, blocks, "daily digest")
        result.digest_sent = True

    return result


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    now = datetime.now(cfg.tz)
    result = run_cycle(cfg, now)
    print(
        f"PR Watch: {len(result.prs)} PR(s) tracked, "
        f"{len(result.alerts_sent)} breach alert(s), "
        f"digest={'sent' if result.digest_sent else 'skipped'}, "
        f"{len(result.pruned)} stale state entr(ies) pruned."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
