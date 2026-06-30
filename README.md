# PR Watch

Keep cross-team pull requests moving. **PR Watch** watches open GitHub PRs where
your team is explicitly requested to review, measures how long each has been
waiting in **business hours** since the latest team review-request, and:

- 🚨 posts **real-time Slack SLA-breach alerts** (deduped via a local JSON state file),
- 📋 posts **daily digests** at fixed review windows (Breached / Approaching / Fresh),
- 👤 pings the **current on-call** by resolving PagerDuty → Slack ID (with a static fallback).

It is **read-only** against GitHub — it never merges or approves anything.

## Why it is built this way

- **Standard library only.** `urllib` for HTTP (GitHub Search + Issue Events,
  Slack `chat.postMessage`, PagerDuty On-Calls), `zoneinfo` for timezones, `json`
  for state. No `requests` / `pytz` / `dateutil` — zero third-party deps, so it is
  trivial to run anywhere and fully testable without network or tokens.
- **Business-hours aware.** Evenings, weekends and configured holidays don't count,
  so PRs raised out-of-hours aren't unfairly penalised.
- **Low noise.** Digests at set windows; real-time pings only for breaches; a state
  file prevents repeat alerts.

## Requirements

- Python 3.10+
- No dependencies to install for runtime. (`ruff` + `pytest` for development only.)

## Install

With [uv](https://docs.astral.sh/uv/):

```bash
uv sync            # creates .venv and installs the dev tools
```

Or with stock Python:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install "ruff>=0.11" "pytest>=7.4"   # dev tools only; runtime needs nothing
```

## Configuration

Set values either as **top-of-file constants** in `pr_watch.py` or as
**environment variables**. Precedence: constant → environment variable → default;
**required secrets** error-and-exit if unset. Tokens are never logged.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GH_TOKEN` | ✅ | — | GitHub PAT (`repo`, `read:org`) |
| `GH_ORG` | ✅ | — | Org login, e.g. `acme-corp` |
| `TEAM_SLUG` | — | `your-team` | Team slug being review-requested |
| `GH_REPOS` | — | _(all)_ | CSV of repo names to limit scope |
| `SLACK_BOT_TOKEN` | ✅ | — | Slack bot token (`xoxb-…`) |
| `SLACK_CHANNEL_ID` | ✅ | — | Channel **ID** (e.g. `C0123456789`), not `#name` |
| `PAGERDUTY_API_TOKEN` | — | — | PagerDuty API token (on-call lookup) |
| `PAGERDUTY_SCHEDULE_ID` | — | — | Schedule to resolve on-call from |
| `PD_TO_SLACK_JSON` | — | — | Path to / inline JSON: PD email → Slack ID |
| `ONCALL_FALLBACK_MENTION` | — | `@oncall` | Static mention when PD lookup fails |
| `TZ_NAME` | — | `Europe/London` | IANA timezone for business hours |
| `BUSINESS_START_HOUR` | — | `9` | Business day start hour |
| `BUSINESS_END_HOUR` | — | `17` | Business day end hour |
| `HOLIDAYS_YYYYMMDD` | — | — | CSV of holidays, e.g. `20250101,20250317` |
| `REVIEW_WINDOWS` | — | `09:15,15:00` | CSV of `HH:MM` digest times (local) |
| `SLA_RESPONSE_HRS` | — | `2.0` | First-response SLA (context) |
| `SLA_APPROVAL_HRS` | — | `4.0` | Approval SLA — drives categorisation |
| `APPROACHING_BUFFER_HRS` | — | `1.0` | "Approaching" band width below the SLA |
| `SEND_DAILY_DIGEST` | — | `true` | Post digests at review windows |
| `DRY_RUN` | — | `false` | Print messages instead of posting |
| `PR_WATCH_STATE_FILE` | — | `pr_watch_state.json` | Dedup state file path |

### Categorisation

For each PR, with `elapsed` = business hours since the latest team review-request:

- **Breached** — `elapsed ≥ SLA_APPROVAL_HRS`
- **Approaching** — `SLA_APPROVAL_HRS − APPROACHING_BUFFER_HRS ≤ elapsed < SLA_APPROVAL_HRS`
- **Fresh** — everything else

## Usage

Dry-run locally first (prints, posts nothing):

```bash
DRY_RUN=true \
GH_TOKEN=… GH_ORG=acme-corp TEAM_SLUG=your-team \
SLACK_BOT_TOKEN=xoxb-… SLACK_CHANNEL_ID=C0123456789 \
python3 pr_watch.py
```

Then drop `DRY_RUN` to post for real. Run it every 15–30 minutes so breaches are
caught promptly and digests fire on the minute they are due.

### Cron (every 15 minutes)

```cron
*/15 * * * * cd /path/to/pr-watch && /path/to/pr-watch/.venv/bin/python pr_watch.py >> pr_watch.log 2>&1
```

### GitHub Actions

```yaml
name: pr-watch
on:
  schedule:
    - cron: "*/15 * * * *"   # every 15 minutes (UTC)
  workflow_dispatch:
jobs:
  watch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python pr_watch.py
        env:
          GH_TOKEN: ${{ secrets.PR_WATCH_GH_TOKEN }}
          GH_ORG: acme-corp
          TEAM_SLUG: your-team
          SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}
          SLACK_CHANNEL_ID: ${{ secrets.SLACK_CHANNEL_ID }}
          PAGERDUTY_API_TOKEN: ${{ secrets.PAGERDUTY_API_TOKEN }}
          PAGERDUTY_SCHEDULE_ID: ${{ secrets.PAGERDUTY_SCHEDULE_ID }}
```

> Note: GitHub Actions `schedule` is UTC and the state file is ephemeral between
> runs unless you persist it (cache/artifact) — dedup is best-effort there. A
> long-lived host with cron is the most reliable place to run this.

## Slack message examples

**Real-time breach alert**

```
🚨 Review needed  ·  acme-corp/checkout → main
PR #245: Fix checkout bug  •  by alice  •  5.2h since request (business hours)
Requested: 2025-10-13 09:30 • SLA 4h
<@U1234567> (on-call) please review now
[Open PR] [Files] [Approve]
```

**Daily digest (09:15)**

```
📋 Daily Review Queue — 09:15
On-Call: <@U1234567>

🚨 Breached SLA
- acme-corp/checkout #245 · Fix checkout bug · 5.2h waiting
- acme-corp/payments #246 · Add card validation · 4.8h waiting

⏳ Approaching SLA
- acme-corp/bff #251 · Update API contract · 3.1h waiting

🆕 Fresh PRs
- acme-corp/checkout #252 · Refactor totals · 0.4h waiting
```

## State file

`pr_watch_state.json` remembers which PRs have already been alerted for a breach,
so each breach pings once. A **new** review-request (newer timestamp) re-arms the
alert. Closed/merged PRs are pruned from the state automatically each run.

```json
{
  "version": 1,
  "alerted": {
    "acme-corp/checkout#245": {
      "requested_at": "2025-10-13T09:30:00+01:00",
      "elapsed_h": 5.2,
      "alerted_at": "2025-10-13T14:42:00+01:00"
    }
  }
}
```

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

The test suite uses **no network and no tokens**: the GitHub, Slack and PagerDuty
layers are driven through a monkeypatched `urlopen`, and time is injected so the
business-hours, categorisation, digest-ordering and review-window logic are
deterministic. A committed-secret scan guards against tokens leaking into source.

## Error handling

- **Missing required config** → exits with a clear list of missing variables.
- **GitHub rate limits (403/429)** → respects `Retry-After`, retries with backoff.
- **PagerDuty/Slack errors** → logged and skipped; on-call falls back to the static mention.
- **Network hiccups** → small retry loop with jitter, capped, to keep runs fast.
