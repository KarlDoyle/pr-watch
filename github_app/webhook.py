"""GitHub App webhook receiver + event-driven state, replacing polling.

The original CLI (`pr_watch.py`) discovers open review requests by polling
GitHub's Search API every N minutes. The earlier technical audit
recommended replacing that discovery step with the `pull_request` webhook
event (action `review_requested`) once this becomes a real GitHub App —
GitHub pushes the event the moment a team is requested, instead of PR
Watch finding out up to N minutes later on the next poll.

What stays polling-shaped: SLA breach/digest is a *time-elapsed* condition
("has it now been >= SLA_APPROVAL_HRS business hours"), not a discrete
event GitHub fires. So this module still needs a periodic sweep
(`sweep_and_alert`) — the difference is that sweep reads PRs the webhook
already recorded locally, with zero calls to the Search API. Wire the
sweep to any scheduler (cron, GitHub Actions `schedule`, a background
thread in `server.py`) at whatever cadence you'd have polled at before.

State: a JSON file, same shape as `pr_watch.py`'s state plus one new
top-level key, `"open"`, keyed the same way (`"owner/repo#number"`) that
holds the currently-tracked PR fields the webhook has told us about. The
existing `"alerted"` dedup key is reused unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime

import pr_watch as pw


class SignatureError(Exception):
    """Raised when a webhook payload's HMAC signature does not verify."""


def verify_signature(payload: bytes, signature_header: str | None, secret: str) -> None:
    """Verify GitHub's `X-Hub-Signature-256: sha256=<hex hmac>` header.

    Constant-time compare via `hmac.compare_digest`. Raises `SignatureError`
    on any mismatch or malformed header — callers should respond 401/403
    and must NOT process the payload.
    """
    if not secret:
        raise SignatureError("No webhook secret configured (GH_WEBHOOK_SECRET unset).")
    if not signature_header or not signature_header.startswith("sha256="):
        raise SignatureError("Missing or malformed X-Hub-Signature-256 header.")
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    provided = signature_header.split("=", 1)[1]
    if not hmac.compare_digest(expected, provided):
        raise SignatureError("Signature does not match payload.")


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------
REVIEW_REQUESTED = "review_requested"
REVIEW_REQUEST_REMOVED = "review_request_removed"
CLOSED = "closed"
IGNORED = "ignored"


@dataclass
class WebhookEvent:
    kind: str  # REVIEW_REQUESTED | REVIEW_REQUEST_REMOVED | CLOSED | IGNORED
    key: str | None = None  # "owner/repo#number"
    pr: pw.PR | None = None


def _pr_key(payload: dict) -> str | None:
    repo_full = (payload.get("repository") or {}).get("full_name", "")
    number = (payload.get("pull_request") or {}).get("number")
    if not repo_full or not number:
        return None
    return f"{repo_full}#{number}"


def _pr_from_payload(payload: dict, requested_at: datetime) -> pw.PR:
    pr_obj = payload["pull_request"]
    repo_full = payload["repository"]["full_name"]
    return pw.PR(
        repo=repo_full,
        number=pr_obj["number"],
        title=pr_obj.get("title", ""),
        author=(pr_obj.get("user") or {}).get("login", "unknown"),
        url=pr_obj.get("html_url", ""),
        branch=(pr_obj.get("head") or {}).get("ref", ""),
        requested_at=requested_at,
    )


def parse_pull_request_event(payload: dict, team_slug: str) -> WebhookEvent:
    """Parse a `pull_request` webhook delivery into a `WebhookEvent`.

    Only these actions matter to PR Watch; everything else (including
    `review_requested` aimed at a different team, or an individual
    reviewer rather than a team) is IGNORED:

      - review_requested        (requested_team.slug == team_slug) -> track
      - review_request_removed  (requested_team.slug == team_slug) -> untrack
      - closed                                                     -> untrack
    """
    action = payload.get("action")
    key = _pr_key(payload)

    if action == REVIEW_REQUESTED:
        team = payload.get("requested_team") or payload.get("requested_reviewer_team") or {}
        if not isinstance(team, dict) or team.get("slug") != team_slug:
            return WebhookEvent(kind=IGNORED)
        if key is None:
            return WebhookEvent(kind=IGNORED)
        pr_obj = payload.get("pull_request")
        updated_at = pr_obj.get("updated_at") if isinstance(pr_obj, dict) else None
        if not updated_at:
            # Malformed/unexpected payload shape (e.g. missing the field
            # entirely) — skip rather than crash; there is nothing safe to
            # track without a timestamp to measure elapsed time from.
            return WebhookEvent(kind=IGNORED)
        # `updated_at` on the PR at delivery time is GitHub's closest
        # webhook-payload proxy for "when this request happened" — there is
        # no dedicated event timestamp field in the pull_request payload.
        try:
            requested_at = pw._parse_iso(updated_at)
        except (ValueError, TypeError):
            return WebhookEvent(kind=IGNORED)
        return WebhookEvent(
            kind=REVIEW_REQUESTED, key=key, pr=_pr_from_payload(payload, requested_at)
        )

    if action == REVIEW_REQUEST_REMOVED:
        team = payload.get("requested_team") or payload.get("requested_reviewer_team") or {}
        if not isinstance(team, dict) or team.get("slug") != team_slug:
            return WebhookEvent(kind=IGNORED)
        return WebhookEvent(kind=REVIEW_REQUEST_REMOVED, key=key)

    if action == CLOSED:
        return WebhookEvent(kind=CLOSED, key=key)

    return WebhookEvent(kind=IGNORED)


# ---------------------------------------------------------------------------
# Event-populated state ("open" tracking, alongside pw's "alerted" state)
# ---------------------------------------------------------------------------
def load_webhook_state(path: str) -> dict:
    state = pw.load_state(path)
    state.setdefault("open", {})
    if not isinstance(state["open"], dict):
        state["open"] = {}
    return state


def apply_event(state: dict, event: WebhookEvent) -> None:
    """Mutate `state["open"]` in place per the event. Does not save to disk."""
    if event.kind == REVIEW_REQUESTED and event.pr is not None and event.key is not None:
        pr = event.pr
        state["open"][event.key] = {
            "repo": pr.repo,
            "number": pr.number,
            "title": pr.title,
            "author": pr.author,
            "url": pr.url,
            "branch": pr.branch,
            "requested_at": pr.requested_at_iso,
        }
    elif event.kind in (REVIEW_REQUEST_REMOVED, CLOSED) and event.key is not None:
        state["open"].pop(event.key, None)
        state["alerted"].pop(event.key, None)


def tracked_prs(state: dict, cfg: pw.Config, now: datetime) -> list[pw.PR]:
    """Rebuild categorised `PR` objects from the event-populated state.

    This is the webhook-mode replacement for `pr_watch.build_prs()` — same
    output shape, but sourced entirely from local state instead of a fresh
    Search API + Issue Events call.
    """
    prs: list[pw.PR] = []
    for record in state.get("open", {}).values():
        requested_at = pw._parse_iso(record["requested_at"])
        elapsed = pw.business_hours_between(
            requested_at,
            now,
            tz=cfg.tz,
            start_hour=cfg.business_start_hour,
            end_hour=cfg.business_end_hour,
            holidays=cfg.holidays,
        )
        pr = pw.PR(
            repo=record["repo"],
            number=record["number"],
            title=record.get("title", ""),
            author=record.get("author", "unknown"),
            url=record.get("url", ""),
            branch=record.get("branch", ""),
            requested_at=requested_at,
            elapsed_hrs=elapsed,
        )
        pr.category = pw.categorise(elapsed, cfg.sla_approval_hrs, cfg.approaching_buffer_hrs)
        prs.append(pr)
    return prs


# ---------------------------------------------------------------------------
# Sweep: the still-periodic half. Reuses pw's alert/digest/emit logic
# unmodified — only the PR *discovery* step is event-driven now.
# ---------------------------------------------------------------------------
def sweep_and_alert(cfg: pw.Config, now: datetime) -> pw.RunResult:
    """One event-driven watch cycle: no Search API call, everything else
    (breach dedup, digest windows, Slack emit) identical to `pw.run_cycle`.
    """
    result = pw.RunResult()

    state = load_webhook_state(cfg.state_file)
    prs = tracked_prs(state, cfg, now)
    result.prs = prs

    open_keys = {pr.key for pr in prs}
    result.pruned = pw.prune_state(state, open_keys)

    oncall = pw.resolve_oncall_mention(cfg, now)

    for pr in prs:
        if pr.category != pw.BREACHED:
            continue
        if pw.needs_alert(state, pr.key, pr.requested_at_iso):
            text, blocks = pw.format_breach_alert(pr, oncall, cfg.sla_approval_hrs, cfg.tz)
            pw._emit(cfg, text, blocks, f"breach alert {pr.key}")
            pw.mark_alerted(state, pr, now)
            result.alerts_sent.append(pr.key)

    pw.save_state(cfg.state_file, state)

    if cfg.send_daily_digest and pw.is_review_window(now, cfg.review_windows):
        breached, approaching, fresh = pw.order_digest(prs)
        text, blocks = pw.format_digest(breached, approaching, fresh, oncall, now, cfg.tz)
        pw._emit(cfg, text, blocks, "daily digest")
        result.digest_sent = True

    return result


# ---------------------------------------------------------------------------
# Delivery handling (framework-agnostic: called by server.py's HTTP layer)
# ---------------------------------------------------------------------------
GH_WEBHOOK_SECRET_ENV = "GH_WEBHOOK_SECRET"


def handle_delivery(
    *,
    event_name: str,
    payload: bytes,
    signature_header: str | None,
    team_slug: str,
    state_path: str,
    secret: str | None = None,
) -> WebhookEvent:
    """Verify, parse, and apply one webhook delivery. Raises SignatureError
    on bad signatures (caller should respond 401). Returns the parsed event
    (kind=IGNORED for anything PR Watch doesn't act on, including every
    event type other than `pull_request`).
    """
    resolved_secret = secret if secret is not None else os.getenv(GH_WEBHOOK_SECRET_ENV, "")
    verify_signature(payload, signature_header, resolved_secret)

    if event_name != "pull_request":
        return WebhookEvent(kind=IGNORED)

    data = json.loads(payload.decode("utf-8"))
    event = parse_pull_request_event(data, team_slug)

    if event.kind != IGNORED:
        state = load_webhook_state(state_path)
        apply_event(state, event)
        pw.save_state(state_path, state)

    return event
