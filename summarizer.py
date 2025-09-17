#!/usr/bin/env python3
import sys
import re
from collections import defaultdict

def parse_gitlab_summary(filename):
    totals = defaultdict(int)
    current_user = None

    # Regex patterns
    user_header_pattern = re.compile(r"^(.+?) \(@.+?\)$")
    activity_line_pattern = re.compile(r"^\s+([A-Za-z ]+)\s+(\d+)$")
    separator_pattern = re.compile(r"^=+$")
    totals_section_pattern = re.compile(r"^TOTALS$")

    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")

            # Skip totals section completely
            if totals_section_pattern.match(line):
                break

            # Detect a user header line (Name (@handle))
            user_match = user_header_pattern.match(line)
            if user_match:
                current_user = user_match.group(1).strip()
                continue

            # Detect activity lines under the current user
            if current_user:
                act_match = activity_line_pattern.match(line)
                if act_match:
                    count = int(act_match.group(2))
                    totals[current_user] += count

    return totals


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 main.py inputfile")
        sys.exit(1)

    filename = sys.argv[1]
    totals = parse_gitlab_summary(filename)

    # Sort by value descending
    sorted_totals = sorted(totals.items(), key=lambda x: x[1], reverse=True)

    for name, total in sorted_totals:
        print(f"{name}: {total}")


if __name__ == "__main__":
    main()
