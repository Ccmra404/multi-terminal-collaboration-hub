import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOLS = str(Path(__file__).resolve().parents[1] / "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import project_task_planner


REPO = "example/coordination-hub"


def task_issue(block, labels=("codex-task",)):
    return {
        "number": 23,
        "labels": [{"name": label} for label in labels],
        "body": "Request text is untrusted.\n\n```codex-task\n" + json.dumps(block) + "\n```",
    }


def valid_block():
    return {
        "target": "server",
        "repository": REPO,
        "goal": "Add a small deterministic helper and focused tests",
        "allowed_paths": ["tools/example.py", "tests/test_example.py"],
        "test_command": [
            "python3",
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_example.py",
            "-v",
        ],
        "timeout_seconds": 120,
    }


class ProjectTaskPlannerTests(unittest.TestCase):
    def test_standard_task_requires_label_and_strict_fields(self):
        parsed = project_task_planner.parse_standard_task(task_issue(valid_block()), REPO)
        self.assertEqual(parsed["repository"], REPO)
        self.assertEqual(parsed["allowed_paths"], ("tools/example.py", "tests/test_example.py"))
        with self.assertRaises(project_task_planner.TaskNotEligibleError):
            project_task_planner.parse_standard_task(task_issue(valid_block(), labels=()), REPO)

        invalid = valid_block()
        invalid["extra"] = "ignored"
        with self.assertRaises(project_task_planner.TaskNotEligibleError):
            project_task_planner.parse_standard_task(task_issue(invalid), REPO)

    def test_trusted_plain_issue_uses_repository_allowlist_and_fixed_tests(self):
        issue = {
            "number": 23, "title": "Improve the status cards", "body": "Keep the current UI and add a test.",
            "author_association": "COLLABORATOR", "labels": [],
        }
        policy = {
            "allowed_path_patterns": ("web/status/**", "tests/**"),
            "test_command": ("node", "--test", "tests/*.test.cjs"),
        }
        task = project_task_planner.parse_standard_task(issue, REPO, repository_policy=policy)
        self.assertEqual(task["allowed_paths"], ("web/status/**", "tests/**"))
        self.assertEqual(task["test_command"], ("node", "--test", "tests/*.test.cjs"))
        with self.assertRaises(project_task_planner.TaskNotEligibleError):
            project_task_planner.parse_standard_task(dict(issue, author_association="NONE"), REPO, repository_policy=policy)

    def test_riskier_bounded_plan_is_available_for_explicit_approval(self):
        task = project_task_planner.parse_standard_task({
            "number": 23, "title": "Add a scoped dashboard test", "body": "Create a focused UI test.",
            "author_association": "OWNER", "labels": [],
        }, REPO, repository_policy={
            "allowed_path_patterns": ("web/status/**", "tests/**"),
            "test_command": ("node", "--test", "tests/*.test.cjs"),
        })
        output = {
            "summary": "Review a bounded status-page change", "allowed_paths": ["web/status/app.js"],
            "test_command": list(task["test_command"]), "risk": "medium",
        }
        parsed = project_task_planner._parse_plan_output(json.dumps(output), task)
        self.assertEqual(parsed["risk"], "medium")
        self.assertEqual(parsed["allowed_paths"], ("web/status/app.js",))

    def test_safety_control_changes_require_maintainer_approval_even_if_model_says_low(self):
        block = valid_block()
        block["allowed_paths"] = ["tools/project_task_planner.py"]
        task = project_task_planner.parse_standard_task(task_issue(block), REPO)
        output = {
            "summary": "Update planner behavior",
            "allowed_paths": ["tools/project_task_planner.py"],
            "test_command": list(task["test_command"]),
            "risk": "low",
        }
        parsed = project_task_planner._parse_plan_output(json.dumps(output), task)
        self.assertEqual(parsed["risk"], "high")

    def test_sensitive_goal_path_and_test_are_blocked(self):
        invalid = valid_block()
        invalid["goal"] = "change the deployment credentials"
        with self.assertRaises(project_task_planner.ApprovalRequiredError):
            project_task_planner.parse_standard_task(task_issue(invalid), REPO)

    def _run_candidate(self, tmp, issue):
        import task_dispatch

        store = task_dispatch.DispatchStore(Path(tmp) / "dispatch.json")
        store.sync({
            "schema_version": 3,
            "generated_at": "2026-10-13T18:00:00Z",
            "repositories": [{
                "repository": REPO,
                "tasks": [{
                    "issue": 23,
                    "title": "server task",
                    "state": "open",
                    "status": "PENDING",
                    "status_verified": True,
                    "target": "server",
                    "target_verified": True,
                }],
            }],
        })
        api = mock.Mock()
        api.get.return_value = issue
        plan_config = {
            "plans_file": str(Path(tmp) / "plans.json"),
            "planner_state_file": str(Path(tmp) / "planner-state.json"),
            "max_tasks_per_run": 1,
            "task_label": "codex-task",
        }
        project_config = {"repositories": {REPO: {}}, "plans": {}}
        saved_state = {}

        def capture_state(path, value, label):
            saved_state.update(value)

        with mock.patch.object(project_task_planner, "_monitor_specs", return_value={REPO: {"token_env": "TEST_TOKEN"}}), \
                mock.patch.object(project_task_planner.project_executor, "load_project_config", return_value=project_config), \
                mock.patch.object(project_task_planner, "_api_for", return_value=api), \
                mock.patch.object(project_task_planner, "_write_json", side_effect=capture_state):
            project_task_planner.run_next(store, plan_config, "unused-monitor-config", "unused-project-config")
        return store.get(REPO + "#23"), saved_state

    def test_noneligible_issue_is_not_misreported_as_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            record, planner_state = self._run_candidate(tmp, task_issue(valid_block(), labels=()))
            self.assertEqual(record["status"], "BLOCKED")
            self.assertEqual(record["result"]["reason"], "task_not_eligible")
            self.assertEqual(record["result"]["approval_state"], "NOT_REQUIRED")
            self.assertEqual(planner_state["tasks"][REPO + "#23"]["status"], "INELIGIBLE")

    def test_ineligible_issue_is_rechecked_only_after_source_update(self):
        old = {"source_updated_at": "2026-10-13T17:00:00Z"}
        unchanged = {"source_updated_at": "2026-10-13T17:00:00Z"}
        changed = {"source_updated_at": "2026-10-13T17:01:00Z"}
        self.assertFalse(project_task_planner._ineligible_issue_was_updated(old, unchanged))
        self.assertTrue(project_task_planner._ineligible_issue_was_updated(old, changed))
        self.assertFalse(project_task_planner._ineligible_issue_was_updated(old, {"source_updated_at": "unknown"}))

    def test_policy_sensitive_issue_is_marked_for_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            block = valid_block()
            block["goal"] = "change the deployment credentials"
            record, planner_state = self._run_candidate(tmp, task_issue(block))
            self.assertEqual(record["status"], "BLOCKED")
            self.assertEqual(record["result"]["reason"], "plan_requires_approval")
            self.assertEqual(record["result"]["approval_state"], "PENDING_APPROVAL")
            self.assertEqual(planner_state["tasks"][REPO + "#23"]["status"], "BLOCKED")

        invalid = valid_block()
        invalid["allowed_paths"] = ["config/settings.json"]
        with self.assertRaises(project_task_planner.ApprovalRequiredError):
            project_task_planner.parse_standard_task(task_issue(invalid), REPO)

        invalid = valid_block()
        invalid["test_command"] = ["/bin/sh", "-c", "echo unsafe"]
        with self.assertRaises(project_task_planner.ApprovalRequiredError):
            project_task_planner.parse_standard_task(task_issue(invalid), REPO)

    def test_planner_output_cannot_expand_scope_or_change_tests(self):
        task = project_task_planner.parse_standard_task(task_issue(valid_block()), REPO)
        output = {
            "summary": "small low-risk helper",
            "allowed_paths": ["tools/example.py"],
            "test_command": list(task["test_command"]),
            "risk": "low",
        }
        parsed = project_task_planner._parse_plan_output(json.dumps(output), task)
        self.assertEqual(parsed["allowed_paths"], ("tools/example.py",))

        output["allowed_paths"] = ["README.md"]
        with self.assertRaises(project_task_planner.PlannerError):
            project_task_planner._parse_plan_output(json.dumps(output), task)

    def test_planner_config_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "planner.json"
            value = {
                "schema_version": 1,
                "executor": "codex_task_planner",
                "codex_path": "/usr/local/bin/codex",
                "codex_home": str(Path(tmp) / "codex-home"),
                "workspace_root": str(Path(tmp) / "workspaces"),
                "plans_file": str(Path(tmp) / "plans.json"),
                "planner_state_file": str(Path(tmp) / "state.json"),
                "task_label": "codex-task",
                "max_tasks_per_run": 1,
                "timeout_seconds": 120,
            }
            path.write_text(json.dumps(value), encoding="utf-8")
            loaded = project_task_planner.load_planner_config(path)
            self.assertEqual(loaded["task_label"], "codex-task")
            self.assertEqual(loaded["approval_file"], Path(project_task_planner.DEFAULT_APPROVAL_FILE))
            value["task_label"] = "unsafe label with spaces"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(project_task_planner.PlannerError):
                project_task_planner.load_planner_config(path)


if __name__ == "__main__":
    unittest.main()
