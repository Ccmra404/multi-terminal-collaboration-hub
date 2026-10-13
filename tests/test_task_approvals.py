import tempfile
import unittest
from pathlib import Path
import sys

TOOLS = str(Path(__file__).resolve().parents[1] / "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import task_approvals


class TaskApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [1_800_000_000]
        self.store = task_approvals.TaskApprovalStore(
            Path(self.temp.name) / "approvals.json", clock=lambda: self.now[0]
        )
        self.request = {
            "task_id": "Ccmra404/multi-terminal-collaboration-hub#3",
            "repository": "Ccmra404/multi-terminal-collaboration-hub",
            "issue": 3,
            "title": "Improve the task card",
            "summary": "Update a scoped component and add focused tests",
            "issue_digest": "a" * 64,
            "allowed_paths": ["web/status/app.js", "tests/status_frontend.test.cjs"],
            "test_command": ["node", "--test", "tests/status_frontend.test.cjs"],
            "plan": {"trusted": True, "prompt": "fixed plan", "paths": ["web/status/app.js"]},
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_plan_can_be_approved_once_and_consumed_once(self):
        pending = self.store.create_pending(self.request)
        self.assertEqual(pending["status"], "PENDING")
        self.assertEqual(self.store.pending()[0]["request_id"], pending["request_id"])
        approved = self.store.decide(pending["request_id"], "approve", "admin")
        self.assertEqual(approved["status"], "APPROVED")
        consumed = self.store.consume(pending["request_id"], self.request["issue_digest"])
        self.assertEqual(consumed["state"], "approved")
        self.assertEqual(consumed["plan"]["prompt"], "fixed plan")
        self.assertEqual(self.store.consume(pending["request_id"], self.request["issue_digest"])["state"], "consumed")

    def test_issue_edit_or_rejection_cannot_release_a_plan(self):
        pending = self.store.create_pending(self.request)
        self.store.decide(pending["request_id"], "approve", "admin")
        self.assertEqual(self.store.consume(pending["request_id"], "b" * 64)["state"], "mismatch")
        rejected = self.store.create_pending(dict(self.request, issue_digest="c" * 64))
        self.store.decide(rejected["request_id"], "reject", "admin")
        self.assertEqual(self.store.consume(rejected["request_id"], "c" * 64)["state"], "rejected")

    def test_decisions_expire_and_replays_are_rejected(self):
        pending = self.store.create_pending(self.request, ttl_seconds=60)
        self.now[0] += 61
        with self.assertRaises(task_approvals.ApprovalError):
            self.store.decide(pending["request_id"], "approve", "admin")
        self.assertEqual(self.store.consume(pending["request_id"], self.request["issue_digest"])["state"], "expired")


if __name__ == "__main__":
    unittest.main()
