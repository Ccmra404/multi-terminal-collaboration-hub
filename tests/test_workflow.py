import json
import sys
import tempfile
import unittest
from pathlib import Path


TOOLS = str(Path(__file__).resolve().parents[1] / "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import authorization
import evidence
import workflow


REPOSITORY = "example/coordination-hub"
SHA = "a" * 40
NOW = "2026-10-12T08:00:00Z"


def make_request():
    return {
        "project_id": "collaboration-hub",
        "repository": REPOSITORY,
        "branch": "feat/issue-8-v2-workflow",
        "environment": "development",
        "action": "merge",
        "head_sha": SHA,
        "pull_number": 8,
        "actor": "server",
        "executor": "server",
        "task_id": "8",
        "paths": ["deploy/forged"],
        "ci_passed": False,
        "independent_review": False,
        "created_at": NOW,
        "reason": "test workflow",
    }


def make_coordinator(tmp, simulation=False, fixtures=None):
    policy = authorization.load_policy(
        Path(__file__).resolve().parents[1] / "config" / "project-authorization.example.json"
    )
    fixtures = fixtures or {
        (REPOSITORY, 8, SHA): {
            "paths": ["tools/workflow.py"],
            "ci_passed": True,
            "independent_review": True,
        }
    }
    store = workflow.WorkflowStore(Path(tmp) / "workflow.json")
    adapter = evidence.FixtureEvidenceAdapter(fixtures, repository=REPOSITORY)
    authority = workflow.SimulationApprovalAuthority(enabled=simulation)
    return workflow.WorkflowCoordinator(
        authorization.PolicyEngine(policy), store, adapter, authority
    ), store, adapter


class HandoffApi(object):
    def __init__(self, body, comments):
        self.body = body
        self.comments = comments
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params or {}))
        if path.endswith("/issues/8"):
            return {
                "number": 8,
                "body": self.body,
                "user": {"login": "server"},
                "created_at": NOW,
            }
        if path.endswith("/issues/8/comments"):
            return self.comments
        raise AssertionError(path)


class WorkflowTests(unittest.TestCase):
    def test_events_are_deduplicated_and_ack_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = workflow.WorkflowStore(path)
            first = store.append_event(
                "task_submitted", "hub", REPOSITORY, "8", "server", "computer",
                payload={"x": 1}, dedupe_key="task-8"
            )
            second = store.append_event(
                "task_submitted", "hub", REPOSITORY, "8", "server", "computer",
                payload={"x": 1}, dedupe_key="task-8"
            )
            self.assertEqual(first["event_id"], second["event_id"])
            self.assertEqual(len(store.discover("computer")), 1)
            self.assertTrue(store.ack(first["event_id"], "computer"))
            self.assertFalse(store.discover("computer"))
            restarted = workflow.WorkflowStore(path)
            self.assertFalse(restarted.discover("computer"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(str(path) + ".lock").stat().st_mode & 0o777, 0o600)

    def test_event_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = workflow.WorkflowStore(Path(tmp) / "state.json")
            store.append_event("task_submitted", "hub", REPOSITORY, "8", "server", "computer", {"x": 1}, dedupe_key="same")
            with self.assertRaises(workflow.WorkflowError):
                store.append_event("task_submitted", "hub", REPOSITORY, "8", "server", "computer", {"x": 2}, dedupe_key="same")

    def test_store_cannot_be_used_as_an_unverified_approval_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator, store, _adapter = make_coordinator(tmp, simulation=False)
            pending = coordinator.request_authorization(make_request(), now=NOW)
            with self.assertRaises(workflow.WorkflowError):
                store.decide(pending["request_id"], "approve", "maintainer", now=NOW)

    def test_end_to_end_waits_without_trusted_authority_then_consumes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator, store, adapter = make_coordinator(tmp, simulation=False)
            task = coordinator.submit_task(
                "collaboration-hub", REPOSITORY, "8", "server", "computer", {"work": "test"}
            )
            self.assertEqual(len(coordinator.discover("computer")), 1)
            report = coordinator.report(task["event_id"], "computer", "reported", "ready")
            self.assertEqual(report["kind"], "task_reported")
            self.assertFalse(coordinator.discover("computer"))

            pending = coordinator.request_authorization(make_request(), now=NOW)
            self.assertEqual(pending["decision"], authorization.PENDING_APPROVAL)
            self.assertEqual(pending["paths"], ["tools/workflow.py"])
            self.assertTrue(store.pending_approvals(now=NOW))
            unverified = coordinator.decide(pending["request_id"], "approve", "maintainer", now=NOW)
            self.assertEqual(unverified["decision"], authorization.PENDING_APPROVAL)
            self.assertEqual(store.get_approval(pending["request_id"])["status"], authorization.PENDING_APPROVAL)

            coordinator.approval_authority = workflow.SimulationApprovalAuthority(enabled=True)
            approved = coordinator.decide(
                pending["request_id"], "approve", "simulated-maintainer", now=NOW
            )
            self.assertEqual(approved["status"], "APPROVED")
            allowed = coordinator.resume(pending["request_id"], now=NOW)
            self.assertEqual(allowed["decision"], authorization.ALLOW)
            replay = coordinator.resume(pending["request_id"], now=NOW)
            self.assertEqual(replay["decision"], authorization.DENY)
            self.assertEqual(replay["reason_code"], "approval_consumed")

    def test_changed_evidence_cannot_resume_an_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator, store, adapter = make_coordinator(tmp, simulation=True)
            pending = coordinator.request_authorization(make_request(), now=NOW)
            coordinator.decide(pending["request_id"], "approve", "simulated-maintainer", now=NOW)
            adapter.fixtures[(REPOSITORY, 8, SHA)]["paths"] = ["tools/changed.py"]
            result = coordinator.resume(pending["request_id"], now=NOW)
            self.assertEqual(result["decision"], authorization.DENY)
            self.assertEqual(result["reason_code"], "evidence_unavailable")
            self.assertEqual(store.get_approval(pending["request_id"])["status"], "APPROVED")

    def test_simulated_reject_and_revoke_are_persistent_terminal_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator, store, _adapter = make_coordinator(tmp, simulation=True)
            rejected = coordinator.request_authorization(make_request(), now=NOW)
            item = coordinator.decide(rejected["request_id"], "reject", "simulated-maintainer", now=NOW)
            self.assertEqual(item["status"], "REJECTED")
            self.assertEqual(coordinator.resume(rejected["request_id"], now=NOW)["reason_code"], "approval_rejected")

            revoked = coordinator.request_authorization(dict(make_request(), reason="second", version="v2"), now=NOW)
            coordinator.decide(revoked["request_id"], "approve", "simulated-maintainer", now=NOW)
            item = coordinator.decide(revoked["request_id"], "revoke", "simulated-maintainer", now=NOW)
            self.assertEqual(item["status"], "REVOKED")
            self.assertEqual(coordinator.resume(revoked["request_id"], now=NOW)["reason_code"], "approval_revoked")

    def test_github_handoff_is_bounded_and_unverified_authors_are_not_trusted(self):
        event = {
            "schema_version": 1,
            "event_id": "evt-1234567890abcdef",
            "kind": "task_reported",
            "project_id": "hub",
            "repository": REPOSITORY,
            "task_id": "8",
            "actor": "server",
            "target": "all",
            "payload": {"status": "reported"},
        }
        envelope = workflow.GithubHandoffAdapter.render_event(event)
        api = HandoffApi(
            envelope,
            [{"body": envelope, "user": {"login": "unknown"}, "created_at": NOW}],
        )
        adapter = workflow.GithubHandoffAdapter(api, REPOSITORY, authorized_users=("server",))
        result = adapter.read_issue(8)
        self.assertEqual(len(result["events"]), 2)
        self.assertTrue(result["events"][0]["verified"])
        self.assertFalse(result["events"][1]["verified"])
        self.assertFalse(result["write_capability"])
        self.assertTrue(all(path.endswith("/issues/8") or path.endswith("/issues/8/comments") for path, _ in api.calls))

    def test_handoff_parser_never_executes_json_as_code(self):
        marker = workflow.EVENT_MARKER
        body = marker + '\n{"schema_version":1,"event_id":"evt-1234567890abcdef","kind":"x","payload":"$(touch /tmp/should-not-exist)"}'
        parsed = workflow.GithubHandoffAdapter.parse_envelope(body, authorized_users=())
        self.assertIsNotNone(parsed)
        self.assertFalse(parsed["verified"])


if __name__ == "__main__":
    unittest.main()
