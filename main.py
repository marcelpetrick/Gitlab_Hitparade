"""
GitLab Monthly Activity Report

Fetch all users visible to your account on a GitLab instance and summarize their
activity during the previous calendar month (e.g., for Sept 2025 run on Oct 1, it
summarizes Sept 1–Sept 30 inclusive).

Outputs a readable summary to stdout.

Usage examples:
  python gitlab_monthly_activity_report.py --base-url https://git.data-modul.com --token $GITLAB_TOKEN
  python gitlab_monthly_activity_report.py --base-url https://git.data-modul.com  # reads token from env GITLAB_TOKEN

You can override the time window with --since / --until if needed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from typing import Dict, Iterable, List, Tuple

import requests

# ------------------------------
# Time window helpers
# ------------------------------

def previous_calendar_month(today: dt.date | None = None) -> Tuple[dt.datetime, dt.datetime]:
    """Return (since, until) UTC datetimes for the previous calendar month.

    since is 00:00:00 on the 1st of the previous month (inclusive)
    until is 23:59:59.999999 on the last day of the previous month (inclusive)
    """
    if today is None:
        today = dt.date.today()
    first_of_this_month = today.replace(day=1)
    last_of_prev_month_date = first_of_this_month - dt.timedelta(days=1)
    first_of_prev_month_date = last_of_prev_month_date.replace(day=1)
    since = dt.datetime.combine(first_of_prev_month_date, dt.time.min, tzinfo=dt.timezone.utc)
    # end of last day inclusive
    until = dt.datetime.combine(last_of_prev_month_date, dt.time.max, tzinfo=dt.timezone.utc)
    return since, until


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Summarize GitLab user activity for the previous calendar month.")
    p.add_argument("--base-url", required=True, help="Base URL of your GitLab instance, e.g. https://git.data-modul.com")
    p.add_argument("--token", help="Private access token or personal access token. If omitted, reads GITLAB_TOKEN env var.")
    p.add_argument("--since", help="Start timestamp (ISO 8601). Overrides previous calendar month.")
    p.add_argument("--until", help="End timestamp (ISO 8601). Overrides previous calendar month.")
    p.add_argument("--per-page", type=int, default=100, help="Pagination size (default 100, max 100).")
    p.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds (default 30).")
    p.add_argument("--verbose", action="store_true", help="Print progress to stderr.")
    return p.parse_args()


# ------------------------------
# HTTP helpers
# ------------------------------

def build_session(token: str, timeout: int) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "PRIVATE-TOKEN": token,
        "Accept": "application/json",
        "User-Agent": "gitlab-monthly-activity-report/1.0",
    })
    # stash a default timeout on the session
    s.request = _wrap_request_with_timeout(s.request, default_timeout=timeout)  # type: ignore
    return s


def _wrap_request_with_timeout(request_fn, default_timeout: int):
    def _wrapped(method, url, **kwargs):
        if "timeout" not in kwargs:
            kwargs["timeout"] = default_timeout
        return request_fn(method, url, **kwargs)
    return _wrapped


def get_paginated(session: requests.Session, url: str, params: Dict[str, str | int] | None = None,
                  per_page: int = 100, verbose: bool = False) -> Iterable[dict]:
    """Yield JSON items from a paginated GitLab API endpoint.

    Handles X-Next-Page style pagination.
    """
    if params is None:
        params = {}
    params = {**params, "per_page": min(max(per_page, 1), 100)}
    page = 1
    while True:
        params_with_page = {**params, "page": page}
        resp = session.get(url, params=params_with_page)
        if resp.status_code != 200:
            raise RuntimeError(f"GET {url} failed: {resp.status_code} {resp.text}")
        data = resp.json()
        if isinstance(data, list):
            for item in data:
                yield item
        else:
            # Some endpoints may return an object – yield as single item
            yield data
        next_page = resp.headers.get("X-Next-Page")
        if verbose:
            total = resp.headers.get("X-Total")
            sys.stderr.write(f"Fetched page {page}; total={total or '?'} next_page={next_page or 'None'}\n")
        if not next_page:
            break
        page = int(next_page)


# ------------------------------
# Domain logic
# ------------------------------

ActivitySummary = Dict[str, int]

CATEGORY_ALIASES = {
    # Canonical category name -> list of (action_name, target_type) tuples to match
    # We'll also handle commits specially using push_data.commit_count.
    "commits": [],  # handled specially via push events
    "pushes": [("pushed to", None)],
    "merge_requests_opened": [("opened", "MergeRequest")],
    "merge_requests_merged": [("merged", "MergeRequest")],
    "merge_requests_closed": [("closed", "MergeRequest")],
    "issues_opened": [("opened", "Issue")],
    "issues_closed": [("closed", "Issue")],
    "comments": [("commented on", None)],  # any target_type
    "wiki_updates": [("updated", "WikiPage"), ("created", "WikiPage")],
    "milestones": [("created", "Milestone"), ("closed", "Milestone"), ("updated", "Milestone")],
    "snippets": [("created", "Snippet"), ("updated", "Snippet")],
    "approvals": [("approved", "MergeRequest")],
}


def classify_event(event: dict) -> Tuple[str | None, int]:
    """Return (category, count_increment).

    For push events, we add commit_count to 'commits' and 1 to 'pushes'.
    For comments, count 1 per event.
    For other actions, count 1 in their specific category when recognizable.
    If not recognized, return (None, 0).
    """
    action = (event.get("action_name") or "").lower()
    target_type = event.get("target_type")  # may be None

    # Push handling: commits live in push_data
    if action == "pushed to":
        commit_count = 0
        push_data = event.get("push_data") or {}
        try:
            commit_count = int(push_data.get("commit_count") or 0)
        except Exception:
            commit_count = 0
        return ("commits", commit_count)  # commits accounted; caller will also count a push

    # Comments
    if action == "commented on":
        return ("comments", 1)

    # Generic mapping
    for category, patterns in CATEGORY_ALIASES.items():
        if not patterns:
            continue
        for a_name, t_type in patterns:
            if a_name == action and (t_type is None or t_type == target_type):
                return (category, 1)
    return (None, 0)


def summarize_user_events(events: List[dict]) -> ActivitySummary:
    summary: ActivitySummary = {k: 0 for k in CATEGORY_ALIASES.keys()}
    # We'll also keep an 'other' bucket for unknowns
    summary["other"] = 0

    for ev in events:
        # commits via push
        if (ev.get("action_name") or "").lower() == "pushed to":
            # commits
            commits_cat, commit_inc = classify_event(ev)
            summary[commits_cat] += commit_inc
            # and pushes
            summary["pushes"] += 1
            continue
        cat, inc = classify_event(ev)
        if cat is None:
            summary["other"] += 1
        else:
            summary[cat] += inc
    # prune zero keys for cleaner output
    return {k: v for k, v in summary.items() if v}


# ------------------------------
# API calls
# ------------------------------

def fetch_all_users(session: requests.Session, base_url: str, per_page: int, verbose: bool) -> List[dict]:
    url = f"{base_url.rstrip('/')}/api/v4/users"
    params = {"active": True, "without_projects": False, "order_by": "id", "sort": "asc"}
    return list(get_paginated(session, url, params=params, per_page=per_page, verbose=verbose))


def fetch_user_events(session: requests.Session, base_url: str, user_id: int, since_iso: str, until_iso: str,
                      per_page: int, verbose: bool) -> List[dict]:
    url = f"{base_url.rstrip('/')}/api/v4/users/{user_id}/events"
    params = {"after": since_iso, "before": until_iso}
    return list(get_paginated(session, url, params=params, per_page=per_page, verbose=verbose))


# ------------------------------
# Output formatting
# ------------------------------

def human_dt(dtobj: dt.datetime) -> str:
    # ISO 8601 without microseconds
    return dtobj.replace(microsecond=0).isoformat()


def print_report(users: List[dict], per_user_summaries: Dict[int, ActivitySummary],
                 since: dt.datetime, until: dt.datetime) -> None:
    # Header
    print("=" * 72)
    print("GitLab Activity Summary")
    print("Window:", human_dt(since), "to", human_dt(until), "(UTC)")
    print("Users considered:", len(users))
    print("=" * 72)

    # Per-user
    grand_total: ActivitySummary = {}
    for user in users:
        uid = int(user["id"])
        summary = per_user_summaries.get(uid, {})
        if not summary:
            continue
        name = user.get("name") or user.get("username") or f"user:{uid}"
        print(f"\n{name} (@{user.get('username')})")
        print("-" * (len(name) + len(user.get('username', '')) + 4))
        for k, v in sorted(summary.items()):
            print(f"  {k.replace('_', ' ').title():28s} {v:>6d}")
            grand_total[k] = grand_total.get(k, 0) + v

    # Totals
    if grand_total:
        print("\n" + "=" * 72)
        print("TOTALS")
        print("-" * 72)
        for k, v in sorted(grand_total.items()):
            print(f"  {k.replace('_', ' ').title():28s} {v:>6d}")
        print("=" * 72)


# ------------------------------
# Main
# ------------------------------

def main() -> int:
    args = parse_args()

    token = args.token or os.getenv("GITLAB_TOKEN")
    if not token:
        sys.stderr.write("Error: supply --token or set GITLAB_TOKEN env var.\n")
        return 2

    # Time window
    if args.since and args.until:
        since = dt.datetime.fromisoformat(args.since)
        until = dt.datetime.fromisoformat(args.until)
        if since.tzinfo is None:
            since = since.replace(tzinfo=dt.timezone.utc)
        if until.tzinfo is None:
            until = until.replace(tzinfo=dt.timezone.utc)
    elif args.since or args.until:
        sys.stderr.write("Error: provide both --since and --until, or neither to use previous month.\n")
        return 2
    else:
        since, until = previous_calendar_month()

    since_iso = human_dt(since)
    until_iso = human_dt(until)

    sess = build_session(token, timeout=args.timeout)

    if args.verbose:
        sys.stderr.write(f"Listing users from {args.base_url}...\n")
    users = fetch_all_users(sess, args.base_url, per_page=args.per_page, verbose=args.verbose)

    per_user_summaries: Dict[int, ActivitySummary] = {}

    for user in users:
        uid = int(user["id"])
        if args.verbose:
            sys.stderr.write(f"Fetching events for {user.get('username')} (id={uid})...\n")
        events = fetch_user_events(sess, args.base_url, uid, since_iso, until_iso, args.per_page, args.verbose)
        if not events:
            continue
        per_user_summaries[uid] = summarize_user_events(events)

    print_report(users, per_user_summaries, since, until)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
