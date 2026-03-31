"""
GitLab Sprint Time Summary

Fetch all open issues in a GitLab milestone (sprint) and summarize time tracking:
- original estimate
- time spent
- remaining time (estimate - spent, floored at 0)

Usage examples:
  python filterOpenMilestoneIssues.py \
      --base-url https://git.data-modul.com \
      --milestone-url https://git.data-modul.com/groups/easyanalyzer/-/milestones/6#tab-issues \
      --token $GITLAB_TOKEN

  python filterOpenMilestoneIssues.py \
      --base-url https://git.data-modul.com \
      --milestone-url https://git.data-modul.com/groups/easyanalyzer/-/milestones/6#tab-issues
      # reads token from env GITLAB_TOKEN
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, Iterable, List, Tuple
from urllib.parse import urlparse, quote_plus

import requests


# ------------------------------
# Arg parsing
# ------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Summarize remaining time for open issues in a GitLab milestone (sprint)."
    )
    p.add_argument(
        "--base-url",
        required=True,
        help="Base URL of your GitLab instance, e.g. https://git.data-modul.com",
    )
    p.add_argument(
        "--milestone-url",
        required=True,
        help=(
            "Full URL of the milestone (sprint) page, e.g. "
            "https://git.data-modul.com/groups/easyanalyzer/-/milestones/6"
        ),
    )
    p.add_argument(
        "--token",
        help="Private access token or personal access token. If omitted, reads GITLAB_TOKEN env var.",
    )
    p.add_argument(
        "--per-page",
        type=int,
        default=100,
        help="Pagination size (default 100, max 100).",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="HTTP timeout in seconds (default 30).",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print progress to stderr.",
    )
    return p.parse_args()


# ------------------------------
# HTTP helpers
# ------------------------------

def _wrap_request_with_timeout(request_fn, default_timeout: int):
    def _wrapped(method, url, **kwargs):
        if "timeout" not in kwargs:
            kwargs["timeout"] = default_timeout
        return request_fn(method, url, **kwargs)

    return _wrapped


def build_session(token: str, timeout: int) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "PRIVATE-TOKEN": token,
            "Accept": "application/json",
            "User-Agent": "gitlab-sprint-time-summary/1.0",
        }
    )
    # stash a default timeout on the session
    s.request = _wrap_request_with_timeout(s.request, default_timeout=timeout)  # type: ignore
    return s


def get_paginated(
    session: requests.Session,
    url: str,
    params: Dict[str, str | int] | None = None,
    per_page: int = 100,
    verbose: bool = False,
) -> Iterable[dict]:
    """Yield JSON items from a paginated GitLab API endpoint."""
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
            yield data

        next_page = resp.headers.get("X-Next-Page")
        if verbose:
            total = resp.headers.get("X-Total")
            sys.stderr.write(
                f"Fetched page {page}; total={total or '?'} next_page={next_page or 'None'}\n"
            )
        if not next_page:
            break
        page = int(next_page)


# ------------------------------
# Milestone URL parsing
# ------------------------------

def parse_milestone_url(milestone_url: str) -> Tuple[str, str, str]:
    """Parse a GitLab milestone URL and return (kind, namespace_path, milestone_iid).

    Supports:
      /groups/<group>/-/milestones/<iid>
      /<namespace>/<project>/-/milestones/<iid>
    """
    parsed = urlparse(milestone_url)
    path = parsed.path
    parts = [p for p in path.split("/") if p]

    try:
        idx_dash = parts.index("-")
        idx_m = parts.index("milestones")
    except ValueError:
        raise ValueError(f"Invalid milestone URL path: {path!r}")

    try:
        milestone_iid = parts[idx_m + 1]
    except IndexError:
        raise ValueError(f"Could not find milestone iid after 'milestones' in path: {path!r}")

    # detect group vs project
    if parts[0] == "groups":
        namespace_parts = parts[1:idx_dash]
        kind = "group"
    else:
        namespace_parts = parts[:idx_dash]
        kind = "project"

    if not namespace_parts:
        raise ValueError(f"Could not determine namespace from URL path: {path!r}")

    namespace_path = "/".join(namespace_parts)

    return kind, namespace_path, milestone_iid


# ------------------------------
# API calls
# ------------------------------

def fetch_milestone(
    session: requests.Session,
    base_url: str,
    kind: str,
    namespace: str,
    milestone_iid: str,
) -> dict:
    """Fetch milestone details by IID."""
    encoded = quote_plus(namespace)

    if kind == "group":
        url = f"{base_url.rstrip('/')}/api/v4/groups/{encoded}/milestones"
    else:
        url = f"{base_url.rstrip('/')}/api/v4/projects/{encoded}/milestones"

    params = {"iids[]": milestone_iid}

    resp = session.get(url, params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"GET {url} failed: {resp.status_code} {resp.text}")

    data = resp.json()
    if not isinstance(data, list) or not data:
        raise RuntimeError(
            f"No milestone found for IID={milestone_iid!r} in {namespace!r}"
        )

    return data[0]


def fetch_milestone_issues(
    session: requests.Session,
    base_url: str,
    kind: str,
    namespace: str,
    milestone: dict,
    per_page: int,
    verbose: bool,
) -> List[dict]:
    """Fetch all issues (open + closed) for a given milestone."""
    encoded = quote_plus(namespace)

    if kind == "group":
        url = f"{base_url.rstrip('/')}/api/v4/groups/{encoded}/issues"
    else:
        url = f"{base_url.rstrip('/')}/api/v4/projects/{encoded}/issues"

    milestone_title = milestone.get("title")
    if not milestone_title:
        raise RuntimeError("Milestone object has no title field.")

    params = {
        "state": "all",               # include open + closed
        "milestone": milestone_title,
        "include_subgroups": "true",  # harmless for projects
    }

    return list(get_paginated(session, url, params=params, per_page=per_page, verbose=verbose))


# ------------------------------
# Formatting helpers
# ------------------------------

def format_seconds(seconds: int) -> str:
    if seconds is None or seconds <= 0:
        return "0h"
    minutes_total = seconds // 60
    hours = minutes_total // 60
    minutes = minutes_total % 60
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "0h"


def build_issue_rows(issues: List[dict]):
    rows = []
    total_estimate = 0
    total_spent = 0
    total_remaining = 0

    for issue in issues:
        iid = issue.get("iid")
        title = issue.get("title") or ""
        label = f"#{iid} {title}".strip()
        if len(label) > 70:
            label = label[:67] + "..."

        time_stats = issue.get("time_stats") or {}
        est = int(time_stats.get("time_estimate") or 0)
        spent = int(time_stats.get("total_time_spent") or 0)
        remaining = max(est - spent, 0)

        total_estimate += est
        total_spent += spent
        total_remaining += remaining

        rows.append((label, format_seconds(est), format_seconds(spent), format_seconds(remaining)))

    return rows, total_estimate, total_spent, total_remaining


def print_report(milestone: dict, issues: List[dict]) -> None:
    open_issues = [i for i in issues if i.get("state") == "opened"]
    closed_issues = [i for i in issues if i.get("state") == "closed"]

    milestone_title = milestone.get("title") or ""
    milestone_id = milestone.get("id")
    milestone_state = milestone.get("state") or ""

    print("=" * 80)
    print("GitLab Sprint Time Summary")
    print("-" * 80)
    print(f"Milestone: {milestone_title} (id={milestone_id}, state={milestone_state})")
    print(f"Open issues: {len(open_issues)} | Closed issues: {len(closed_issues)}")
    print("=" * 80)

    def print_section(title: str, items: List[dict]):
        rows, total_estimate, total_spent, total_remaining = build_issue_rows(items)

        print(f"\n{title}: {len(items)}")
        if not items:
            print("None")
            return

        issue_width = max(len(r[0]) for r in rows)
        header = f"{'Issue':<{issue_width}}  {'Estimate':>10}  {'Spent':>10}  {'Remaining':>10}"
        print(header)
        print("-" * len(header))

        for label, est, spent, rem in rows:
            print(f"{label:<{issue_width}}  {est:>10}  {spent:>10}  {rem:>10}")

        print("-" * len(header))
        print(f"{'TOTAL':<{issue_width}}  {format_seconds(total_estimate):>10}  {format_seconds(total_spent):>10}  {format_seconds(total_remaining):>10}")

    print_section("OPEN ISSUES", open_issues)
    print_section("CLOSED ISSUES", closed_issues)
    print("=" * 80)


# ------------------------------
# Main
# ------------------------------

def main() -> int:
    args = parse_args()

    token = args.token or os.getenv("GITLAB_TOKEN")
    if not token:
        sys.stderr.write("Error: supply --token or set GITLAB_TOKEN env var.\n")
        return 2

    try:
        kind, namespace, milestone_iid = parse_milestone_url(args.milestone_url)
    except Exception as e:
        sys.stderr.write(f"Error parsing --milestone-url: {e}\n")
        return 2

    if args.verbose:
        sys.stderr.write(
            f"Parsed milestone URL: kind={kind!r}, namespace={namespace!r}, milestone_iid={milestone_iid!r}\n"
        )

    sess = build_session(token, timeout=args.timeout)

    if args.verbose:
        sys.stderr.write("Fetching milestone details (by IID)...\n")
    milestone = fetch_milestone(sess, args.base_url, kind, namespace, milestone_iid)

    if args.verbose:
        sys.stderr.write("Fetching issues for milestone (open + closed)...\n")
    issues = fetch_milestone_issues(
        sess,
        args.base_url,
        kind,
        namespace,
        milestone,
        per_page=args.per_page,
        verbose=args.verbose,
    )

    print_report(milestone, issues)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
