#!/usr/bin/env python3
"""Deterministically summarize targets in a local task queue state file."""

import argparse
import json
from pathlib import Path


KNOWN_TARGETS = (
    "server",
    "computer",
    "mobile",
    "unknown",
)
UNKNOWN_TARGET = "unknown"


def summarize_target_counts(tasks):
    """Return counts for every known target, grouping unrecognized targets."""
    counts = {target: 0 for target in KNOWN_TARGETS}

    records = tasks.values() if isinstance(tasks, dict) else tasks
    for record in records:
        target = record.get("target") if isinstance(record, dict) else None
        if target in counts and target != UNKNOWN_TARGET:
            counts[target] += 1
        else:
            counts[UNKNOWN_TARGET] += 1
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_file", type=Path, help="local task dispatch state JSON")
    args = parser.parse_args(argv)
    state = json.loads(args.state_file.read_text(encoding="utf-8"))
    tasks = state.get("tasks", {}) if isinstance(state, dict) else {}
    print(json.dumps(summarize_target_counts(tasks), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
