import importlib.util
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "authorization", Path(__file__).resolve().parents[1] / "tools" / "authorization.py"
)
authorization = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(authorization)


SHA = "a" * 40
OTHER_SHA = "b" * 40
NOW = "2026-10-12T08:00:00Z"


def policy():
    return {
        "schema_version": 1,
        "approval_ttl_seconds": 900,
        "projects": [
            {
                "project_id": "hub",
                "repositories": ["example/hub"],
                "allowed_branches": ["main", "feat/*"],
                "allowed_environments": ["development", "ci"],
                "authorized_actors": ["server", "maintainer", "computer"],
                "authorized_executors": ["server", "computer"],
                "task_writers": {"task-1": "server"},
                "action_policies": {
                    "read": "allow",
                    "run_tests": "allow",
                    "code_change": "allow",
                    "branch_push": "allow",
                    "pull_request": "allow",
                    "merge": "approval",
                    "issue_comment": "approval",
                },
                "sensitive_paths": list(authorization.MINIMUM_SENSITIVE_PATHS),
                "required_checks": {"ci": True, "independent_review": True},
            },
            {
                "project_id": "other",
                "repositories": ["example/other"],
                "allowed_branches": ["main"],
                "allowed_environments": ["development"],
                "authorized_actors": ["server"],
                "authorized_executors": ["server"],
                "task_writers": {},
                "action_policies": {"read": "allow"},
                "sensitive_paths": list(authorization.MINIMUM_SENSITIVE_PATHS),
                "required_checks": {"ci": True, "independent_review": True},
            },
        ],
    }


def request(**overrides):
    value = {
        "project_id": "hub",
        "repository": "example/hub",
        "branch": "feat/task-1",
        "environment": "development",
        "action": "read",
        "head_sha": SHA,
        "actor": "server",
        "executor": "server",
        "task_id": "",
        "issue_ref": "example/hub#1",
        "created_at": NOW,
        "paths": [],
        "reason": "test",
    }
    value.update(overrides)
    if value["action"] in authorization.CODE_ACTIONS and "path_evidence" not in overrides:
        value["path_evidence"] = {
            "source": "trusted_diff",
            "complete": True,
            "paths": list(value["paths"]),
        }
    return value


class FakeVerifier(authorization.ApprovalVerifier):
    def __init__(self, state="approved", approval_id="approval-1", binding_digest=None, expires_at=None):
        self.state = state
        self.approval_id = approval_id
        self.binding_digest = binding_digest
        self.expires_at = expires_at

    def verify(self, approval, binding, now):
        del approval, now
        return {
            "state": self.state,
            "approval_id": self.approval_id,
            "binding_digest": self.binding_digest or authorization._digest(binding),
            "expires_at": self.expires_at or "2026-10-12T08:15:00Z",
        }


class AuthorizationTests(unittest.TestCase):
    def test_sensitive_path_policy_requires_immutable_baseline(self):
        candidate = copy.deepcopy(policy())
        candidate["projects"][0]["sensitive_paths"] = ["deploy/**"]
        with self.assertRaises(authorization.AuthorizationError):
            authorization.PolicyEngine(candidate)
        candidate = copy.deepcopy(policy())
        candidate["projects"][0]["sensitive_paths"] = []
        with self.assertRaises(authorization.AuthorizationError):
            authorization.PolicyEngine(candidate)
        candidate = copy.deepcopy(policy())
        candidate["projects"][0]["sensitive_paths"] = list(
            authorization.MINIMUM_SENSITIVE_PATHS
        ) + ["secrets/**"]
        self.assertIsNotNone(authorization.PolicyEngine(candidate))

    def test_ordinary_in_scope_action_is_allowed(self):
        result = authorization.PolicyEngine(policy()).evaluate(request(), now=NOW)
        self.assertEqual(result["decision"], authorization.ALLOW)
        self.assertEqual(result["reason_code"], "within_scope")

    def test_code_action_requires_the_single_configured_writer(self):
        engine = authorization.PolicyEngine(policy())
        allowed = engine.evaluate(
            request(action="code_change", task_id="task-1", paths=["tools/example.py"]), now=NOW
        )
        self.assertEqual(allowed["decision"], authorization.ALLOW)
        denied = engine.evaluate(
            request(action="code_change", task_id="task-1", executor="computer"), now=NOW
        )
        self.assertEqual(denied["decision"], authorization.DENY)
        self.assertEqual(denied["reason_code"], "task_writer_conflict")

    def test_high_risk_actions_cannot_be_downgraded_to_allow(self):
        for action in authorization.MANDATORY_APPROVAL_ACTIONS:
            candidate = copy.deepcopy(policy())
            candidate["projects"][0]["action_policies"][action] = "allow"
            with self.assertRaises(authorization.AuthorizationError, msg=action):
                authorization.PolicyEngine(candidate)
        candidate = copy.deepcopy(policy())
        candidate["projects"][0]["action_policies"]["unreviewed_action"] = "allow"
        with self.assertRaises(authorization.AuthorizationError):
            authorization.PolicyEngine(candidate)
        engine = authorization.PolicyEngine(policy())
        engine.projects["hub"]["action_policies"]["release"] = "allow"
        runtime = engine.evaluate(request(action="release"), now=NOW)
        self.assertEqual(runtime["decision"], authorization.PENDING_APPROVAL)
        self.assertEqual(runtime["reason_code"], "approval_required")

    def test_code_actions_require_complete_trusted_path_evidence(self):
        engine = authorization.PolicyEngine(policy())
        missing = engine.evaluate(
            request(action="code_change", task_id="task-1", paths=[], path_evidence=None),
            now=NOW,
        )
        self.assertEqual(missing["decision"], authorization.DENY)
        self.assertEqual(missing["reason_code"], "path_evidence_required")
        untrusted = engine.evaluate(
            request(
                action="code_change",
                task_id="task-1",
                paths=["tools/example.py"],
                path_evidence={
                    "source": "self_reported",
                    "complete": True,
                    "paths": ["tools/example.py"],
                },
            ),
            now=NOW,
        )
        self.assertEqual(untrusted["decision"], authorization.DENY)
        self.assertEqual(untrusted["reason_code"], "path_evidence_required")
        mismatch = engine.evaluate(
            request(
                action="pull_request",
                task_id="task-1",
                paths=["tools/example.py"],
                path_evidence={
                    "source": "trusted_diff",
                    "complete": True,
                    "paths": ["docs/other.md"],
                },
            ),
            now=NOW,
        )
        self.assertEqual(mismatch["decision"], authorization.DENY)
        self.assertEqual(mismatch["reason_code"], "path_evidence_required")

    def test_sensitive_action_and_path_stop_at_pending(self):
        engine = authorization.PolicyEngine(policy())
        action = engine.evaluate(
            request(action="merge", task_id="task-1", paths=["tools/example.py"]), now=NOW
        )
        self.assertEqual(action["decision"], authorization.PENDING_APPROVAL)
        self.assertEqual(action["reason_code"], "approval_required")
        path = engine.evaluate(
            request(action="code_change", task_id="task-1", paths=["deploy/service.unit"]), now=NOW
        )
        self.assertEqual(path["decision"], authorization.PENDING_APPROVAL)
        self.assertEqual(path["reason_code"], "sensitive_path")
        self.assertIn("head_sha", path["approval_request"])
        self.assertIn("revoke_if", path["approval_request"])

    def test_untrusted_issue_comment_cannot_approve(self):
        engine = authorization.PolicyEngine(policy())
        result = engine.evaluate(
            request(
                action="merge",
                task_id="task-1",
                paths=["tools/example.py"],
                ci_passed=True,
                independent_review=True,
            ),
            approval={"source": "github_issue", "approved_by": "example-owner", "decision": "approve"},
            now=NOW,
        )
        self.assertEqual(result["decision"], authorization.PENDING_APPROVAL)
        self.assertEqual(result["reason_code"], "approval_unverified")

    def test_trusted_approval_is_bound_and_single_use(self):
        engine = authorization.PolicyEngine(
            policy(),
            verifier=FakeVerifier(expires_at="2026-10-12T08:15:00Z"),
        )
        target = request(
            action="merge",
            task_id="task-1",
            paths=["tools/example.py"],
            ci_passed=True,
            independent_review=True,
        )
        first = engine.evaluate(target, approval={"trusted": True}, now=NOW)
        self.assertEqual(first["decision"], authorization.ALLOW)
        second = engine.evaluate(target, approval={"trusted": True}, now=NOW)
        self.assertEqual(second["decision"], authorization.DENY)
        self.assertEqual(second["reason_code"], "approval_already_used")

    def test_sha_change_invalidates_trusted_approval(self):
        original = request(
            action="merge",
            task_id="task-1",
            paths=["tools/example.py"],
            ci_passed=True,
            independent_review=True,
        )
        original_engine = authorization.PolicyEngine(policy())
        original_binding = original_engine._normalize_request(original, authorization._utc(NOW))[0]["binding_digest"]
        engine = authorization.PolicyEngine(
            policy(), verifier=FakeVerifier(binding_digest=original_binding)
        )
        changed = dict(original)
        changed["head_sha"] = OTHER_SHA
        result = engine.evaluate(changed, approval={"trusted": True}, now=NOW)
        self.assertEqual(result["decision"], authorization.DENY)
        self.assertEqual(result["reason_code"], "approval_binding_mismatch")

    def test_rejected_or_expired_approval_denies(self):
        rejected = authorization.PolicyEngine(policy(), verifier=FakeVerifier(state="rejected"))
        result = rejected.evaluate(
            request(
                action="merge",
                task_id="task-1",
                paths=["tools/example.py"],
                ci_passed=True,
                independent_review=True,
            ),
            approval={"trusted": True},
            now=NOW,
        )
        self.assertEqual(result["decision"], authorization.DENY)
        self.assertEqual(result["reason_code"], "approval_rejected")
        expired = authorization.PolicyEngine(
            policy(), verifier=FakeVerifier(expires_at="2026-10-12T07:59:00Z")
        )
        result = expired.evaluate(
            request(
                action="merge",
                task_id="task-1",
                paths=["tools/example.py"],
                ci_passed=True,
                independent_review=True,
            ),
            approval={"trusted": True},
            now=NOW,
        )
        self.assertEqual(result["decision"], authorization.DENY)
        self.assertEqual(result["reason_code"], "approval_expired_or_rebound")

    def test_out_of_scope_and_cross_project_requests_are_denied(self):
        engine = authorization.PolicyEngine(policy())
        wrong_repository = engine.evaluate(
            request(repository="example/other"), now=NOW
        )
        self.assertEqual(wrong_repository["decision"], authorization.DENY)
        self.assertEqual(wrong_repository["reason_code"], "repository_out_of_scope")
        wrong_project = engine.evaluate(
            request(project_id="other", repository="example/other"), now=NOW
        )
        self.assertEqual(wrong_project["decision"], authorization.DENY)
        self.assertEqual(wrong_project["reason_code"], "branch_out_of_scope")

    def test_expired_request_and_missing_merge_checks_do_not_execute(self):
        engine = authorization.PolicyEngine(policy())
        old = request(created_at="2026-10-12T07:00:00Z")
        expired = engine.evaluate(old, now=NOW)
        self.assertEqual(expired["decision"], authorization.DENY)
        self.assertEqual(expired["reason_code"], "request_expired")
        merge = engine.evaluate(
            request(action="merge", task_id="task-1", paths=["tools/example.py"]), now=NOW
        )
        self.assertEqual(merge["decision"], authorization.PENDING_APPROVAL)
        self.assertIn("ci", merge["missing_checks"])

    def test_pending_request_is_idempotent_for_same_created_at(self):
        engine = authorization.PolicyEngine(policy())
        target = request(
            action="merge",
            task_id="task-1",
            paths=["tools/example.py"],
            ci_passed=True,
            independent_review=True,
        )
        first = engine.evaluate(target, now=NOW)
        later = engine.evaluate(target, now="2026-10-12T08:05:00Z")
        self.assertEqual(first["decision"], authorization.PENDING_APPROVAL)
        self.assertEqual(first["request_id"], later["request_id"])
        self.assertEqual(first["expires_at"], later["expires_at"])

    def test_pending_queue_is_local_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = authorization.PolicyEngine(policy())
            result = engine.evaluate(
                request(
                    action="merge",
                    task_id="task-1",
                    paths=["tools/example.py"],
                    ci_passed=True,
                    independent_review=True,
                ),
                now=NOW,
            )
            queue = authorization.ApprovalQueue(Path(tmp) / "pending.json")
            self.assertTrue(queue.enqueue(result))
            self.assertFalse(queue.enqueue(result))
            self.assertEqual(len(queue.pending(now=NOW)), 1)
            saved = json.loads((Path(tmp) / "pending.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], 1)
            self.assertEqual(saved["items"][0]["request_id"], result["request_id"])
            self.assertEqual((Path(tmp) / "pending.json").stat().st_mode & 0o777, 0o600)

    def test_policy_file_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            path.write_text(json.dumps(policy()), encoding="utf-8")
            loaded = authorization.load_policy(path)
        self.assertEqual(len(loaded["projects"]), 2)
        self.assertEqual(loaded["projects"][0]["repositories"], ["example/hub"])


if __name__ == "__main__":
    unittest.main()
