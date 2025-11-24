# GitLab Monthly Activity Report
This script queries a self‑hosted GitLab instance and summarizes user activity for the previous calendar month.

## Features
- Lists all users visible to your account
- Collects each user’s activity events between the first and last day of the previous month
- Groups events into categories (commits, pushes, MRs, issues, comments, etc.)
- Prints a clean per‑user summary and overall totals

## Requirements
- **Python 3.10+**
- `requests` library (`pip install requests`)
- GitLab **Personal Access Token** with `read_api` scope

## Usage
```bash
export GITLAB_TOKEN="<your-token>"
python gitlab_monthly_activity_report.py --base-url https://git.data-modul.com
```
Or pass token explicitly:
```bash
python gitlab_monthly_activity_report.py --base-url https://git.data-modul.com --token <your-token>
```

### Options
- `--since` / `--until`: Custom time range (ISO 8601). Overrides previous month.
- `--verbose`: Show progress information.
- `--per-page`: Page size for API pagination (default 100).

## Example Output - TODO
```
=======================================================================
GitLab Activity Summary
Window: 2025-08-01T00:00:00+00:00 to 2025-08-31T23:59:59+00:00 (UTC)
Users considered: 42
=======================================================================


Jane Doe (@jane)
-----------------
Commits 15
Merge Requests Opened 3
Comments 14


... (other users)


=======================================================================
TOTALS
-----------------------------------------------------------------------
Commits 152
Merge Requests Opened 33
Comments 204
=======================================================================
```

## License
* GPLv3
* Author: Marcel Petrick // mail@marcelpetrick.it

------

## additional tools
### filterOpenMilestoneIssues.py

```sh
export GITLAB_TOKEN=...  # or use --token

python3 filterOpenMilestoneIssues.py \
  --base-url https://git.data-modul.com \
  --milestone-url "https://git.data-modul.com/groups/easyanalyzer/-/milestones/6#tab-issues" \
  --verbose
```
