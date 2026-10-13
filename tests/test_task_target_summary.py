import sys
import unittest
from pathlib import Path


TOOLS = str(Path(__file__).resolve().parents[1] / "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import task_target_summary


class TaskTargetSummaryTests(unittest.TestCase):
    def test_empty_queue_includes_zero_for_each_target(self):
        self.assertEqual(
            task_target_summary.summarize_target_counts({}),
            {
                "server": 0,
                "computer": 0,
                "mobile": 0,
                "unknown": 0,
            },
        )

    def test_normal_queue_counts_known_targets(self):
        tasks = {
            "example/hub#1": {"target": "server"},
            "example/hub#2": {"target": "computer"},
            "example/hub#3": {"target": "server"},
            "example/hub#4": {"target": "mobile"},
        }
        counts = task_target_summary.summarize_target_counts(tasks)
        self.assertEqual(counts["server"], 2)
        self.assertEqual(counts["computer"], 1)
        self.assertEqual(counts["mobile"], 1)
        self.assertEqual(counts["unknown"], 0)

    def test_unknown_and_missing_targets_are_bucketed(self):
        tasks = [{"target": "unknown"}, {"target": "SERVER"}, {}]
        counts = task_target_summary.summarize_target_counts(tasks)
        self.assertEqual(counts["unknown"], 3)
        self.assertEqual(sum(counts.values()), 3)


if __name__ == "__main__":
    unittest.main()
