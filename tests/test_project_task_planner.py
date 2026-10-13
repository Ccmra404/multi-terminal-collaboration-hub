import json
import sys
import tempfile
import unittest
from pathlib import Path


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
        with self.assertRaises(project_task_planner.PlannerError):
            project_task_planner.parse_standard_task(task_issue(valid_block(), labels=()), REPO)

        invalid = valid_block()
        invalid["extra"] = "ignored"
        with self.assertRaises(project_task_planner.PlannerError):
            project_task_planner.parse_standard_task(task_issue(invalid), REPO)

    def test_sensitive_goal_path_and_test_are_blocked(self):
        invalid = valid_block()
        invalid["goal"] = "change the deployment credentials"
        with self.assertRaises(project_task_planner.PlannerError):
            project_task_planner.parse_standard_task(task_issue(invalid), REPO)

        invalid = valid_block()
        invalid["allowed_paths"] = ["config/settings.json"]
        with self.assertRaises(project_task_planner.PlannerError):
            project_task_planner.parse_standard_task(task_issue(invalid), REPO)

        invalid = valid_block()
        invalid["test_command"] = ["/bin/sh", "-c", "echo unsafe"]
        with self.assertRaises(project_task_planner.PlannerError):
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
            self.assertEqual(project_task_planner.load_planner_config(path)["task_label"], "codex-task")
            value["task_label"] = "unsafe label with spaces"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(project_task_planner.PlannerError):
                project_task_planner.load_planner_config(path)


if __name__ == "__main__":
    unittest.main()
