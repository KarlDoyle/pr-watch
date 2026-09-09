# PR Watch (`pr-watch`)

Keep cross-team pull requests moving. **PR Watch** watches open GitHub PRs where
your team is explicitly requested to review, measures how long each has been
waiting in **business hours** since the latest team review-request, and:

![tests](https://img.shields.io/badge/tests-93-slategray?style=flat-square)
![deps](https://img.shields.io/badge/runtime--deps-zero-slategray?style=flat-square)
![python](https://img.shields.io/badge/python-3.10%2B-slategray?style=flat-square)
![licence](https://img.shields.io/badge/licence-UNLICENSED-slategray?style=flat-square)

- 🚨 posts **real-time Slack SLA-breach alerts** (deduped via a local JSON state file),
- 📋 posts **daily digests** at fixed review windows (Breached / Approaching / Fresh),
- 👤 pings the **current on-call** by resolving PagerDuty → Slack ID (with a static fallback).

It is **read-only** against GitHub — it never merges or approves anything.

## Three modes, one engine

| Mode | Entry point | Audience | Status |
|---|---|---|---|
| **CLI / polling** (original) | `pr_watch.py` | One team, their own Slack channel | Working, unchanged |
| **GitHub App / webhook** | `github_app/` | Same team-level alerting, event-driven instead of polled | Code complete, **needs manual registration** (below) |
| **Multi-team rollup** | `rollup.py` | Director/HR-level, several teams at once | Working, standalone |

The webhook and rollup modes are **additive** — they import and reuse
`pr_watch.py`'s business-hours engine, categorisation, state-file dedup and
Slack formatting rather than duplicating it. Nothing about the original
CLI/cron/GitHub-Actions usage below changes.

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

## Multi-team landscape rollup (director / HR-level)

`rollup.py` is a **separate, standalone script** — same engine, different
audience and framing. It aggregates PR Watch's data across **multiple**
team/org sources into one cross-team digest for someone who oversees
several teams, not the team doing the reviewing.

**Why this exists, and why the framing is different:** pointing the
single-team digest at one team's own channel reads as "you're slow" and
gets defensive — a team-level manager sees their team singled out.
Aggregated across many teams for someone a level up, the same underlying
numbers read as useful context ("here's the landscape, worth being aware
of") rather than a performance callout. Senior leadership consistently
found this framing useful; individual team leads did not, when it named
just them. So the rollup:

- names **no individual**, ever (no PagerDuty on-call ping, no `@mentions`);
- groups by **team, in aggregate counts only** — no per-PR titles/authors in
  the digest itself;
- opens every digest with an explicit framing line (`FRAMING_NOTE` in
  `rollup.py`) stating it is not a ranking;
- is a **separate Slack channel** (`LANDSCAPE_SLACK_CHANNEL_ID`) from any
  team's own alert channel, by design — this is additive to team-level
  alerting, not a replacement for it.

### Configuration

| Variable | Required | Purpose |
|---|---|---|
| `GH_TOKEN` | ✅ | GitHub PAT with `read:org` on every org in scope |
| `PR_WATCH_TEAMS_JSON` | ✅ | Inline JSON array, or path to one — see below |
| `SLACK_BOT_TOKEN` | ✅ | Slack bot token |
| `LANDSCAPE_SLACK_CHANNEL_ID` | ✅ | The **director-level** channel — keep it separate from team channels |
| `TZ_NAME`, `BUSINESS_START_HOUR`, `BUSINESS_END_HOUR`, `HOLIDAYS_YYYYMMDD`, `SLA_APPROVAL_HRS`, `APPROACHING_BUFFER_HRS`, `REVIEW_WINDOWS`, `DRY_RUN` | — | Same meaning as the CLI's config, shared across every team source |
| `PR_WATCH_LANDSCAPE_JSON_OUT` | — | If set, also writes the digest as JSON to this path (consumed by the Forge app — see below) |

`PR_WATCH_TEAMS_JSON` example:

```json
[
  {"label": "Payments (EU)", "org": "acme-corp", "team_slug": "payments"},
  {"label": "Checkout", "org": "acme-corp", "team_slug": "checkout"},
  {"label": "Platform (US)", "org": "acme-corp", "team_slug": "platform", "repos": ["gateway", "billing"]}
]
```

Run it the same way as the CLI (cron / GitHub Actions `schedule`), at
whatever cadence makes sense for a digest — e.g. once daily, not every 15
minutes:

```bash
DRY_RUN=true GH_TOKEN=… PR_WATCH_TEAMS_JSON=teams.json \
SLACK_BOT_TOKEN=… LANDSCAPE_SLACK_CHANNEL_ID=C_DIRECTOR \
python3 rollup.py
```

**Example digest:**

```
🗺️ Cross-Team Review Landscape — 2025-10-13 09:15
Teams observed: 3 · Teams with items past SLA: 1

This is an awareness digest, not a performance ranking — flow varies with
team size, review load and time zone. Useful for spotting where support
might help, not for judging who's behind.

Past SLA (worth being aware of)
- Payments (EU): 2 item(s) waiting, oldest 9.2h

Everything else
- Checkout: 0 past SLA, 1 approaching, 2 fresh
- Platform (US): 0 past SLA, 0 approaching, 0 fresh
```

## GitHub App mode (webhook, replaces polling)

`github_app/` turns the discovery half of PR Watch (currently: poll the
Search API every N minutes) into a real GitHub App that receives
`pull_request` webhook events — specifically `review_requested` — the
moment a team is asked to review, instead of finding out on the next poll.
This is **additive**: the CLI/cron/GitHub-Actions polling mode above
continues to work unchanged; the webhook path is a new, separate way to
run PR Watch.

**What still runs on a schedule:** an SLA breach is a *time-elapsed*
condition ("has it now been ≥ 4 business hours"), not something GitHub
fires an event for. So `github_app/webhook.sweep_and_alert()` still runs
periodically — the difference is it reads PRs the webhook already
recorded locally and makes **zero Search-API calls**. `github_app/server.py`
runs this sweep on a background thread by default; swap it for cron / a
scheduled Action if you'd rather not run a long-lived process.

| File | What it does |
|---|---|
| `github_app/manifest.json` | GitHub App Manifest (for the manifest-flow registration — see below) |
| `github_app/webhook.py` | Signature verification (`X-Hub-Signature-256`), `pull_request` event parsing, event-populated state, and the sweep/alert cycle |
| `github_app/auth.py` | App JWT (RS256) signing + installation-token exchange |
| `github_app/server.py` | Stdlib `http.server` webhook receiver (`POST /github/webhook`, `GET /healthz`) + background sweep thread |

**Dependency note:** RS256 JWT signing needs real RSA key operations,
which the standard library doesn't provide. Rather than pull that into
`pr_watch.py` (deliberately zero-dependency), it's isolated in
`github_app/auth.py` behind an optional dependency:

```bash
uv sync --extra github-app   # installs `cryptography`, only for App-token minting
```

Receiving and parsing webhooks (`webhook.py`, `server.py`) needs **no**
extra dependency — only minting installation tokens to call back into the
GitHub API does.

### Manual setup: GitHub App (cannot be done by this session)

Registering a GitHub App requires Karl's own GitHub account/org action at
github.com — nothing here can do that. Once you have a public HTTPS URL
for `github_app/server.py` (a small VM, a container behind a reverse
proxy, etc. — `localhost` will not work, GitHub must be able to reach it):

1. **Fill in the manifest.** Edit `github_app/manifest.json`: replace
   `REPLACE_WITH_PUBLIC_URL` and `REPLACE_WITH_WEBHOOK_URL` with your real
   HTTPS host (webhook path is `/github/webhook`).
2. **Register via the App Manifest flow** — go to
   `https://github.com/settings/apps/new?state=<random-string>` (personal
   account) or `https://github.com/organizations/<org>/settings/apps/new`
   (org-owned), and POST the manifest JSON per
   [GitHub's manifest-flow docs](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest).
   GitHub will hand back the **App ID**, a **generated webhook secret**,
   and let you download the **private key** (`.pem`) — this only happens
   once, save the key immediately.
3. **Set these as environment variables** wherever `github_app/server.py`
   runs (same host, alongside the existing `GH_TOKEN`/`GH_ORG`/`TEAM_SLUG`/
   `SLACK_BOT_TOKEN`/`SLACK_CHANNEL_ID` from the CLI mode):
   - `GH_WEBHOOK_SECRET` — the secret GitHub generated in step 2
   - `GH_APP_ID` — the App ID from step 2
   - `GH_APP_PRIVATE_KEY_PATH` — path to the downloaded `.pem`
4. **Install the App** on the org/repos you want it watching (Settings →
   Developer settings → GitHub Apps → your app → Install App).
5. **Verify permissions match the manifest**: Pull requests: Read-only,
   Metadata: Read-only, Members: Read-only. If GitHub's UI shows anything
   beyond read access, something in the manifest drifted — fix it before
   installing anywhere with real data.
6. **Confirm delivery**: GitHub Apps → your app → Advanced → Recent
   Deliveries should show `200`/`202` responses after step 4. `github_app/
   server.py` logs every delivery's event name and parsed outcome to
   stdout.
7. **List on GitHub Marketplace** (once you're ready to sell it): App
   settings → Marketplace tab → fill in listing details, pricing (the
   earlier audit's 5% fee point), and submit for review. This is a
   separate manual step from App registration and needs the App to be
   public with at least one real installation first.

## Atlassian Forge app (Jira dashboard gadget)

`forge_app/` is a Forge app (not Connect — Connect is closed to new apps)
exposing the same cross-team landscape digest as a Jira dashboard gadget,
for the Atlassian Marketplace side of the dual listing.

| File | What it does |
|---|---|
| `forge_app/manifest.yml` | Forge app manifest: a `jira:dashboardGadget` module, an hourly `scheduledTrigger`, and the two backend functions |
| `forge_app/src/resolvers/index.js` | `refreshLandscape` (scheduled, fetches the JSON digest and caches it in Forge storage) + `handler` (serves the cached digest to the gadget) |
| `forge_app/src/frontend/index.jsx` | The gadget UI (UI Kit 2) — same awareness framing as the Slack digest, no per-team ranking |

**How the gadget gets data:** Forge apps cannot reach arbitrary URLs —
egress is limited to hosts explicitly declared in
`permissions.external.fetch.backend`, and Forge cannot call back into a
local process at all. So the gadget's scheduled function fetches a JSON
snapshot over HTTPS from wherever you publish one; `rollup.py` writes that
snapshot when `PR_WATCH_LANDSCAPE_JSON_OUT` is set. Wire the two together
by publishing that file somewhere Atlassian's infrastructure can reach
(the same host running `github_app/server.py`, a static host, GitHub
Pages, etc.) — this scaffold does not choose a host for you.

### Manual setup: Atlassian Forge (cannot be done by this session)

Needs Karl's own Atlassian developer account — nothing here can register
or deploy this.

1. **Install the Forge CLI** and log in:
   ```bash
   npm install -g @forge/cli
   forge login
   ```
2. **Register the app** from `forge_app/`:
   ```bash
   cd forge_app
   npm install
   forge create   # or `forge register` if you're wiring this exact manifest
   ```
   This assigns the real `app.id` — replace the `REPLACE_WITH_APP_ID`
   placeholder in `manifest.yml` with what it gives you.
3. **Decide where the landscape JSON is published**, then:
   - Replace `REPLACE_WITH_LANDSCAPE_JSON_HOST` in `manifest.yml`'s
     `permissions.external.fetch.backend` with that host.
   - Set the fetch URL for the resolver:
     ```bash
     forge variables set PR_WATCH_LANDSCAPE_URL https://your-host/landscape.json
     ```
4. **Deploy and install**:
   ```bash
   forge deploy
   forge install   # choose the target Jira site
   ```
5. **Verify the gadget**: add it to a Jira dashboard, confirm it shows
   "No data yet" before the first scheduled refresh, then real counts
   after (`scheduledTrigger` runs hourly per the manifest).
6. **List on the Atlassian Marketplace** (once ready to sell it): Developer
   Console → your app → Marketplace listing → fill in listing details and
   pricing, submit for review. Atlassian's fee is higher than GitHub
   Marketplace's (~16-17%+ vs 5%) — a deliberate choice to have both
   surfaces rather than picking one.

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q          # 105 tests: pr_watch core (45) + rollup (22) +
                           # github_app webhook (25) + server (7) + auth (6)
```

The test suite uses **no network and no tokens**: the GitHub, Slack and PagerDuty
layers are driven through a monkeypatched `urlopen`, and time is injected so the
business-hours, categorisation, digest-ordering and review-window logic are
deterministic. A committed-secret scan guards against tokens leaking into source.
The webhook tests use realistic `pull_request` payload shapes matching GitHub's
own webhook docs (field names, nesting, event actions), and the App-JWT tests
verify a real RS256 signature locally against its matching public key — none of
this touches a live, registered GitHub App or Atlassian tenant.

## Error handling

- **Missing required config** → exits with a clear list of missing variables.
- **GitHub rate limits (403/429)** → respects `Retry-After`, retries with backoff.
- **PagerDuty/Slack errors** → logged and skipped; on-call falls back to the static mention.
- **Network hiccups** → small retry loop with jitter, capped, to keep runs fast.

## Licence

Unlicensed (all rights reserved) — personal tooling, not published for reuse.
