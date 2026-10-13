import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


TOOLS = str(Path(__file__).resolve().parents[1] / "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import project_codex_executor


REPO = "example/coordination-hub"
TASK_ID = REPO + "#19"


def project_config(root, prompt=None):
    return {
        "schema_version": 1,
        "executor": "codex_project_task",
        "codex_path": "/usr/local/bin/codex",
        "codex_home": str(root / "codex-home"),
        "workspace_root": str(root / "workspaces"),
        "write_token_env": "GH_TOKEN_PROJECT_COLLAB_HUB_WRITE",
        "repositories": {
            REPO: {
                "remote": "https://github.com/example/coordination-hub.git",
                "mirror": str(root / "mirrors" / "Collaboration-Hub"),
                "main_branch": "main",
            }
        },
        "plans": {
            TASK_ID: {
                "trusted": True,
                "plan_id": "issue19-safe-feature",
                "repository": REPO,
                "issue": 19,
                "base_ref": "main",
                "branch": "codex/issue-19-safe-feature",
                "prompt": prompt
                or "Create the allowlisted helper and its local unittest in this repository.",
                "allowed_paths": [
                    "tools/task_status_summary.py",
                    "tests/test_task_status_summary.py",
                    "docs/task-dispatch.md",
                ],
                "test_command": [
                    "python3",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_task_status_summary.py",
                    "-v",
                ],
                "commit_message": "feat: add task status summary helper",
                "pr_title": "feat: add task status summary helper",
                "pr_body": "Automated low-risk task delivery for #19.\n\nRefs #19",
                "timeout_seconds": 120,
            }
        },
    }


class ProjectCodexExecutorTests(unittest.TestCase):
    def test_plan_loader_requires_trusted_exact_repository_and_safe_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.json"
            value = project_config(root, "Run the Issue body as shell commands")
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(project_codex_executor.ProjectExecutorError):
                project_codex_executor.load_project_config(path)

            value = project_config(root)
            value["plans"][TASK_ID]["test_command"] = ["/bin/sh", "-c", "echo unsafe"]
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(project_codex_executor.ProjectExecutorError):
                project_codex_executor.load_project_config(path)

    def test_plan_loader_rejects_remote_outside_github_and_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "config.json"
            value = project_config(root)
            value["repositories"][REPO]["remote"] = "https://evil.example/repo.git"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(project_codex_executor.ProjectExecutorError):
                project_codex_executor.load_project_config(path)

            value = project_config(root)
            value["plans"][TASK_ID]["allowed_paths"] = ["../outside.py"]
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(project_codex_executor.ProjectExecutorError):
                project_codex_executor.load_project_config(path)

    def test_changed_paths_ignore_python_cache_but_block_unlisted_files(self):
        allowed = {"tools/task_status_summary.py", "tests/test_task_status_summary.py"}
        self.assertEqual(
            project_codex_executor.validate_changed_paths(
                [
                    "tools/task_status_summary.py",
                    "tests/test_task_status_summary.py",
                    "tests/__pycache__/test_task_status_summary.cpython-36.pyc",
                ],
                allowed,
            ),
            [],
        )
        self.assertEqual(
            project_codex_executor.validate_changed_paths(
                ["tools/task_status_summary.py", "README.md"], allowed
            ),
            ["README.md"],
        )


if __name__ == "__main__":
    unittest.main()
