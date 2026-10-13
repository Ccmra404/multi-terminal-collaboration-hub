#!/usr/bin/env python3
"""Deterministically summarize statuses in a local task queue state file."""

import argparse
import json
from pathlib import Path


KNOWN_STATUSES = (
    "PENDING",
    "CLAIMED",
    "RUNNING",
    "REVIEW",
    "DONE",
    "FAILED",
    "BLOCKED",
)
UNKNOWN_STATUS = "UNKNOWN"


def summarize_status_counts(tasks):
    """Return counts for every known status plus unrecognized statuses."""
    counts = {status: 0 for status in KNOWN_STATUSES}
    counts[UNKNOWN_STATUS] = 0

    records = tasks.values() if isinstance(tasks, dict) else tasks
    for record in records:
        status = record.get("status") if isinstance(record, dict) else None
        if status in counts and status != UNKNOWN_STATUS:
            counts[status] += 1
        else:
            counts[UNKNOWN_STATUS] += 1
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_file", type=Path, help="local task dispatch state JSON")
    args = parser.parse_args(argv)
    state = json.loads(args.state_file.read_text(encoding="utf-8"))
    tasks = state.get("tasks", {}) if isinstance(state, dict) else {}
    print(json.dumps(summarize_status_counts(tasks), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
