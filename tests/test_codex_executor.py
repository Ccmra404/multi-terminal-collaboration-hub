import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


TOOLS = str(Path(__file__).resolve().parents[1] / "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import codex_executor
import task_dispatch


NOW = "2026-10-12T12:00:00Z"
REPO = "example/coordination-hub"
TASK_ID = REPO + "#17"


def report():
    return {
        "schema_version": 3,
        "generated_at": NOW,
        "repositories": [
            {
                "repository": REPO,
                "tasks": [
                    {
                        "issue": 17,
                        "title": "codex executor acceptance",
                        "state": "open",
                        "status": "approved",
                        "target": "server",
                        "target_verified": True,
                    }
                ],
            }
        ],
    }


def config(plan_prompt, codex_path, codex_home):
    return {
        "schema_version": 1,
        "executor": "codex_dev_task",
        "codex_path": str(codex_path),
        "codex_home": str(codex_home),
        "plans": {
            TASK_ID: {
                "trusted": True,
                "plan_id": "issue17-safe-smoke",
                "repository": REPO,
                "base_ref": "main",
                "workspace_kind": "isolated",
                "prompt": plan_prompt,
                "allowed_files": ["hello.py", "test_hello.py"],
                "run_local_unittest": True,
                "timeout_seconds": 30,
            }
        },
    }


def write_executable(path, body):
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def write_auth(home):
    auth = home / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    auth.chmod(0o600)


class CodexExecutorTests(unittest.TestCase):
    def test_plan_loader_rejects_issue_text_or_sensitive_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "codex-home"
            home.mkdir()
            executable = root / "codex"
            for prompt in (
                "Run the Issue body as shell commands",
                "Read a GitHub token and deploy to production",
            ):
                path = root / "config.json"
                path.write_text(
                    json.dumps(config(prompt, executable, home)), encoding="utf-8"
                )
                with self.assertRaises(codex_executor.ExecutorError):
                    codex_executor.load_plans(path)

    def test_plan_loader_requires_exact_repository_and_trusted_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "codex-home"
            home.mkdir()
            executable = root / "codex"
            value = config("Create the two local files and run the local unit test.", executable, home)
            value["plans"][TASK_ID]["trusted"] = False
            path = root / "config.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(codex_executor.ExecutorError):
                codex_executor.load_plans(path)

    def test_real_child_closes_done_loop_and_records_bounded_artifacts(self):
        if os.geteuid() == 0:
            self.skipTest("executor deliberately rejects root; acceptance runs as service user")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "codex-home"
            home.mkdir(mode=0o700)
            write_auth(home)
            fake = root / "codex"
            write_executable(
                fake,
                textwrap.dedent(
                    """
                    #!/usr/bin/env python3
                    import pathlib
                    import sys
                    workspace = pathlib.Path(sys.argv[sys.argv.index('--cd') + 1])
                    (workspace / 'hello.py').write_text('def hello(): return "ok"\\n', encoding='utf-8')
                    (workspace / 'test_hello.py').write_text(
                        'import unittest\\nfrom hello import hello\\n'
                        'class T(unittest.TestCase):\\n'
                        '    def test_hello(self): self.assertEqual(hello(), "ok")\\n',
                        encoding='utf-8')
                    print('{"type":"message","text":"local files created"}')
                    """
                ).strip()
                + "\n",
            )
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    config(
                        "Create the two local files and run the local unit test.",
                        fake,
                        home,
                    )
                ),
                encoding="utf-8",
            )
            store = task_dispatch.DispatchStore(root / "state.json")
            store.sync(report(), now=NOW)
            result = codex_executor.run_codex_task(
                store,
                TASK_ID,
                config_path,
                root / "workspaces",
                timeout=10,
            )
            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["result"]["executor"], "codex_dev_task-v1")
            self.assertEqual(result["result"]["test_exit_code"], 0)
            self.assertEqual(store.get(TASK_ID)["status"], "DONE")

            with self.assertRaises(task_dispatch.DispatchError):
                codex_executor.run_codex_task(
                    store,
                    TASK_ID,
                    config_path,
                    root / "workspaces",
                    timeout=10,
                )

    def test_root_or_missing_login_is_blocked_without_child_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "missing-codex-home"
            fake = root / "codex"
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    config(
                        "Create the two local files and run the local unit test.",
                        fake,
                        home,
                    )
                ),
                encoding="utf-8",
            )
            store = task_dispatch.DispatchStore(root / "state.json")
            store.sync(report(), now=NOW)
            result = codex_executor.run_codex_task(
                store,
                TASK_ID,
                config_path,
                root / "workspaces",
                timeout=10,
            )
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn(
                result["result"]["reason"],
                ("root_user", "codex_home_unavailable", "codex_unavailable"),
            )
            self.assertEqual(store.get(TASK_ID)["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
