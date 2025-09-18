#!/usr/bin/env python3
"""
GitLab comment extractor

Given a base URL, token, author name, and a time window, fetch that user's
"commented on" events and print only the comments whose body contains the
words "estimation" or "effort" (case-insensitive). For each match, also print
a direct URL to the comment in the browser (when derivable).

Usage examples:
  export GITLAB_TOKEN=...
  python commentExtractor.py --base-url https://git.example.com --author "Jane Doe" --since 2025-09-01T00:00:00+00:00 --until 2025-09-18T23:59:59+00:00

Notes:
- Requires a Personal Access Token with read_api scope.
- Pagination, timeouts, and verbosity flags are similar to your existing script.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from typing import Dict, Iterable, List, Optional, Tuple

import requests


# ------------------------------
# Time window helpers
# ------------------------------

def parse_iso_dt(s: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def human_dt(dtobj: dt.datetime) -> str:
    # ISO 8601 without microseconds
    return dtobj.replace(microsecond=0).isoformat()


# ------------------------------
# Arg parsing
# ------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Extract an author's GitLab comments containing 'estimation' or 'effort'.")
    p.add_argument("--base-url", required=True, help="Base URL of your GitLab instance, e.g. https://git.data-modul.com")
    p.add_argument("--author", required=True, help="Author's display name or username to match.")
    p.add_argument("--token", help="Personal/Private access token. If omitted, reads GITLAB_TOKEN env var.")
    p.add_argument("--since", required=True, help="Start timestamp (ISO 8601), inclusive.")
    p.add_argument("--until", required=True, help="End timestamp (ISO 8601), inclusive.")
    p.add_argument("--per-page", type=int, default=100, help="Pagination size (default 100, max 100).")
    p.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds (default 30).")
    p.add_argument("--verbose", action="store_true", help="Print progress to stderr.")
    return p.parse_args()


# ------------------------------
# HTTP helpers (mirrors your style)
# ------------------------------

def build_session(token: str, timeout: int) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "PRIVATE-TOKEN": token,
        "Accept": "application/json",
        "User-Agent": "gitlab-comment-extractor/1.0",
    })
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
            sys.stderr.write(f"Fetched page {page}; total={total or '?'} next_page={next_page or 'None'}\n")
        if not next_page:
            break
        page = int(next_page)


# ------------------------------
# API: users, events, projects
# ------------------------------

def find_user(session: requests.Session, base_url: str, author: str, per_page: int, verbose: bool) -> Optional[dict]:
    """Find a user by display name or username using /users?search=."""
    url = f"{base_url.rstrip('/')}/api/v4/users"
    # Search endpoint returns matches by name/username/email
    results = list(get_paginated(session, url, params={"search": author}, per_page=per_page, verbose=verbose))
    if not results:
        return None

    # Prefer exact match on username or name (case-insensitive)
    author_lower = author.lower()
    for u in results:
        if (u.get("username") or "").lower() == author_lower:
            return u
    for u in results:
        if (u.get("name") or "").lower() == author_lower:
            return u
    # Fallback: first result
    return results[0]


def fetch_user_events(session: requests.Session, base_url: str, user_id: int, since_iso: str, until_iso: str,
                      per_page: int, verbose: bool) -> List[dict]:
    url = f"{base_url.rstrip('/')}/api/v4/users/{user_id}/events"
    # GitLab expects "after" and "before"
    params = {"after": since_iso, "before": until_iso, "action": "commented"}  # narrow to comments if supported
    return list(get_paginated(session, url, params=params, per_page=per_page, verbose=verbose))


def fetch_project_web_url(session: requests.Session, base_url: str, project_id: int) -> Optional[str]:
    url = f"{base_url.rstrip('/')}/api/v4/projects/{project_id}"
    resp = session.get(url)
    if resp.status_code != 200:
        return None
    return (resp.json() or {}).get("web_url")


# ------------------------------
# URL builder for note targets
# ------------------------------

def build_comment_url(base_url: str, project_web_url: Optional[str], event: dict) -> Optional[str]:
    """
    Construct a direct browser URL to the note, when possible.
    For GitLab, anchors are usually #note_<note_id> on the target page.
    """
    note = (event.get("note") or {})  # many GitLab instances include this on commented events
    note_id = note.get("id")
    target_type = event.get("target_type")
    target_iid = event.get("target_iid")  # for Issues/MRs
    target_id = event.get("target_id")    # for Commits (SHA) and some others

    if not project_web_url:
        return None

    path = None
    if target_type == "Issue" and target_iid is not None:
        path = f"/-/issues/{target_iid}"
    elif target_type == "MergeRequest" and target_iid is not None:
        path = f"/-/merge_requests/{target_iid}"
    elif target_type == "Commit" and target_id:
        path = f"/-/commit/{target_id}"
    elif target_type == "Snippet" and target_id:
        path = f"/-/snippets/{target_id}"
    else:
        # Unknown/unsupported target; we can't build a stable URL.
        return None

    anchor = f"#note_{note_id}" if note_id is not None else ""
    return f"{project_web_url}{path}{anchor}"


# ------------------------------
# Core logic
# ------------------------------

KEYWORD_RE = re.compile(r"\b(estimation|effort)\b", re.IGNORECASE)

def filter_comment_body(body: str) -> bool:
    return bool(KEYWORD_RE.search(body or ""))


def normalize_whitespace(s: str, max_len: int = 0) -> str:
    out = " ".join((s or "").split())
    if max_len and len(out) > max_len:
        return out[: max_len - 1] + "…"
    return out


def main() -> int:
    args = parse_args()

    token = args.token or os.getenv("GITLAB_TOKEN")
    if not token:
        sys.stderr.write("Error: supply --token or set GITLAB_TOKEN env var.\n")
        return 2

    try:
        since = parse_iso_dt(args.since)
        until = parse_iso_dt(args.until)
    except Exception as e:
        sys.stderr.write(f"Error parsing --since/--until: {e}\n")
        return 2

    since_iso = human_dt(since)
    until_iso = human_dt(until)

    sess = build_session(token, timeout=args.timeout)

    if args.verbose:
        sys.stderr.write(f"Searching for author '{args.author}'...\n")
    user = find_user(sess, args.base_url, args.author, per_page=args.per_page, verbose=args.verbose)
    if not user:
        sys.stderr.write(f"Error: author '{args.author}' not found.\n")
        return 2

    uid = int(user["id"])
    author_disp = user.get("name") or user.get("username") or f"user:{uid}"
    if args.verbose:
        sys.stderr.write(f"Found author: {author_disp} (id={uid})\n")
        sys.stderr.write(f"Fetching comment events {since_iso} .. {until_iso} (UTC)...\n")

    events = fetch_user_events(sess, args.base_url, uid, since_iso, until_iso, args.per_page, args.verbose)
    if not events:
        if args.verbose:
            sys.stderr.write("No events in the given window.\n")
        return 0

    # Cache project web_url lookups
    project_url_cache: Dict[int, Optional[str]] = {}

    # Header
    print("=" * 72)
    print(f"Comments by: {author_disp} (@{user.get('username')})")
    print(f"Window: {since_iso} to {until_iso} (UTC)")
    print("=" * 72)

    matches = 0
    for ev in events:
        action = (ev.get("action_name") or "").lower()
        if action != "commented on":
            # Some GitLab versions ignore ?action=commented; filter here
            continue

        note = ev.get("note") or {}
        body = note.get("body") or ev.get("body") or ""  # try a few common spots
        if not body:
            # Nothing to scan
            continue

        if not filter_comment_body(body):
            continue

        matches += 1

        created_at = ev.get("created_at") or note.get("created_at") or ""
        project_id = ev.get("project_id")
        project_web_url: Optional[str] = None
        if isinstance(project_id, int):
            if project_id not in project_url_cache:
                project_url_cache[project_id] = fetch_project_web_url(sess, args.base_url, project_id)
            project_web_url = project_url_cache[project_id]

        url = build_comment_url(args.base_url, project_web_url, ev)

        # Output (single block per match)
        print(f"- When:   {created_at}")
        print(f"  Where:  {ev.get('target_type') or 'Unknown'} | Project ID: {project_id if project_id is not None else 'N/A'}")
        if url:
            print(f"  URL:    {url}")
        else:
            print(f"  URL:    (not available)")
        print(f"  Text:   {normalize_whitespace(body)}")
        print()

    if matches == 0:
        print("(No matching comments found containing 'estimation' or 'effort' in the given window.)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
