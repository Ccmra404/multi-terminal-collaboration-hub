import sys
import unittest
from pathlib import Path


TOOLS = str(Path(__file__).resolve().parents[1] / "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import task_status_summary


class TaskStatusSummaryTests(unittest.TestCase):
    def test_empty_queue_includes_zero_for_each_status(self):
        self.assertEqual(
            task_status_summary.summarize_status_counts({}),
            {
                "PENDING": 0,
                "CLAIMED": 0,
                "RUNNING": 0,
                "REVIEW": 0,
                "DONE": 0,
                "FAILED": 0,
                "BLOCKED": 0,
                "UNKNOWN": 0,
            },
        )

    def test_normal_queue_counts_known_statuses(self):
        tasks = {
            "example/hub#1": {"status": "PENDING"},
            "example/hub#2": {"status": "RUNNING"},
            "example/hub#3": {"status": "PENDING"},
            "example/hub#4": {"status": "DONE"},
        }
        counts = task_status_summary.summarize_status_counts(tasks)
        self.assertEqual(counts["PENDING"], 2)
        self.assertEqual(counts["RUNNING"], 1)
        self.assertEqual(counts["DONE"], 1)
        self.assertEqual(counts["UNKNOWN"], 0)

    def test_unknown_and_missing_statuses_are_bucketed(self):
        tasks = [{"status": "PAUSED"}, {"status": "pending"}, {}]
        counts = task_status_summary.summarize_status_counts(tasks)
        self.assertEqual(counts["UNKNOWN"], 3)
        self.assertEqual(sum(counts.values()), 3)


if __name__ == "__main__":
    unittest.main()
