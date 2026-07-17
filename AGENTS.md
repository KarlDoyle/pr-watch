# AGENTS.md — PR Watch (pr-watch)

The canonical agent instructions for this repo (CLAUDE.md defers here).

## Project

A single-file, stdlib-only **PR SLA watcher**. `pr_watch.py` polls open GitHub
PRs where a given team is explicitly requested to review, measures how long
each has waited in **business hours** since the latest team review-request,
and posts Slack alerts/digests accordingly. It is **read-only against
GitHub** — it never merges, approves, or comments on a PR.

- 🚨 real-time Slack breach alerts, deduped via a local JSON state file
  (`pr_watch_state.json`)
- 📋 daily digests at fixed review windows, grouped Breached / Approaching / Fresh
- 👤 pings the current on-call by resolving PagerDuty → Slack ID, with a
  static-mention fallback

## Build / test / lint

Python 3.10+, managed with [uv](https://docs.astral.sh/uv/). No runtime
dependencies; `ruff` + `pytest` are dev-only.

```bash
uv sync --dev              # install dev tools into .venv
uv run ruff check .        # lint
uv run ruff format --check .   # format check (double quotes, line-length 100)
uv run pytest               # test suite (45 tests)
```

Dry-run the bot itself (prints, posts nothing):

```bash
DRY_RUN=true GH_TOKEN=… GH_ORG=… TEAM_SLUG=… \
SLACK_BOT_TOKEN=… SLACK_CHANNEL_ID=… python3 pr_watch.py
```

## Architecture

Everything lives in one file, `pr_watch.py`, organised top-to-bottom as
numbered sections (grep `^# ---` to jump between them):

```
Config             Config / ConfigError / load_config() — constant → env var →
                   default precedence; required secrets error-and-exit if unset
Business hours     business_hours_between() — timezone- and holiday-aware elapsed time
Categorisation     categorise() — Breached / Approaching / Fresh vs SLA_APPROVAL_HRS
PR                 PR — the per-pull-request record
State              load_state / save_state / needs_alert / mark_alerted / prune_state
                   — JSON dedup file so each breach alerts once; re-arms on a
                   newer review-request; closed/merged PRs are pruned each run
Digest ordering     order_digest() — groups PRs into the three digest sections
Review windows      is_review_window() — fires digests at configured HH:MM slots
HTTP                http_json() — urllib wrapper with rate-limit/backoff handling
GitHub              github_search_prs / github_latest_team_request / build_prs()
                   — GitHub Search + Issue Events APIs
PagerDuty           pagerduty_oncall_email / pd_to_slack_mention / resolve_oncall_mention()
                   — On-Calls API → email → Slack ID (static fallback)
Slack               slack_post_message() — chat.postMessage
Formatting          format_breach_alert / format_digest() — the Slack message bodies
Run loop            RunResult / run_cycle() / main() — one poll-alert-digest cycle
```

External APIs: **GitHub** (Search + Issue Events, PAT auth), **Slack**
(`chat.postMessage`, bot token), **PagerDuty** (On-Calls, optional). All HTTP
goes through the stdlib `urllib` — no `requests`, no `pytz`/`dateutil`.

## Conventions

- Language/runtime: Python 3.10+, standard library only for runtime code.
- Package manager: `uv` (falls back to stock `venv` + `pip`, see README).
- Formatting/lint: `ruff` — line-length 100, double quotes, rule set
  `E, W, F, I, UP, B` (see `ruff.toml`).
- Tests: `pytest`, `tests/test_pr_watch.py`, no network and no real tokens —
  the GitHub/Slack/PagerDuty layers are driven through a monkeypatched
  `urlopen`; time is injected for deterministic business-hours/categorisation/
  digest-ordering/review-window checks.
- Config: settings resolve as constant (top of `pr_watch.py`) → environment
  variable → default; secret constants (`GH_TOKEN`, `SLACK_BOT_TOKEN`) must
  stay `None` in source and be supplied via environment only.

## Gates

CI (`.github/workflows/ci.yml`) runs on push to `main` and on every PR, in
this order (cheapest-first, mirrors any local pre-push hook):

1. `uv run ruff check .` — lint
2. `uv run ruff format --check .` — format check
3. `uv run pytest` — full test suite

The test suite includes a **committed-secret scan**
(`test_no_committed_secrets` in `tests/test_pr_watch.py`) that fails the build
if a real Slack/GitHub/OpenAI-style token pattern, or an employer-specific
placeholder left over from the original spec, appears in `pr_watch.py`. Keep
this test green — do not weaken or remove it.

## Rules

- Never commit secrets or tokens. `GH_TOKEN` and `SLACK_BOT_TOKEN` are sourced
  from the environment only; the source constants must remain `None`.
- Keep tests green — `uv run pytest` must pass before any change lands.
- This tool is read-only against GitHub: do not add code paths that merge,
  approve, or otherwise mutate a PR.
- No new runtime dependencies without a strong reason — the zero-dependency
  design is deliberate (see README's "Why it is built this way").
