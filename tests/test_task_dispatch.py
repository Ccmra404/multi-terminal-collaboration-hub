import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


TOOLS = str(Path(__file__).resolve().parents[1] / "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import task_dispatch


NOW = "2026-10-12T12:00:00Z"
REPO = "example/coordination-hub"
SERVER_REPO = "example/managed-project"


def monitor_report():
    return {
        "schema_version": 3,
        "generated_at": NOW,
        "repositories": [
            {
                "repository": REPO,
                "tasks": [
                    {"issue": 2, "title": "computer task", "state": "open", "status": "unknown"},
                    {"issue": 4, "title": "mobile task", "state": "open", "status": "unknown"},
                    {"issue": 8, "title": "unassigned task", "state": "open", "status": "unknown"},
                ],
            },
            {
                "repository": SERVER_REPO,
                "tasks": [
                    {
                        "issue": 10,
                        "title": "server task",
                        "state": "open",
                        "status": "待执行",
                        "status_verified": False,
                        "owner": "服务器端",
                        "owner_verified": True,
                    },
                ],
            },
        ],
    }


class TaskDispatchTests(unittest.TestCase):
    def test_sync_is_deduplicated_and_unknown_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = task_dispatch.DispatchStore(Path(tmp) / "state.json")
            overrides = {
                REPO + "#2": "computer",
                REPO + "#4": "mobile",
            }
            first = store.sync(monitor_report(), overrides, now=NOW)
            second = store.sync(monitor_report(), overrides, now=NOW)
            self.assertEqual(first["task_count"], 4)
            self.assertEqual(second["task_count"], 4)
            self.assertEqual(len(store.list_tasks()), 4)
            self.assertEqual(store.get(REPO + "#8")["target"], "unknown")
            self.assertEqual(store.get(SERVER_REPO + "#10")["target"], "server")

    def test_single_claim_and_manual_computer_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = task_dispatch.DispatchStore(Path(tmp) / "state.json")
            store.sync(monitor_report(), {REPO + "#2": "computer", REPO + "#4": "mobile"}, now=NOW)
            claimed = store.claim(REPO + "#2", "computer", now=NOW)
            self.assertEqual(claimed["status"], "CLAIMED")
            with self.assertRaises(task_dispatch.DispatchError):
                store.claim(REPO + "#2", "mobile", now=NOW)
            store.start(REPO + "#2", "computer", now=NOW)
            submitted = store.submit(
                REPO + "#2",
                "computer",
                "REVIEW",
                result={"summary": "manual handoff"},
                commit="a" * 40,
                pr=REPO + "#7",
                log_summary="tests passed",
                now=NOW,
            )
            self.assertEqual(submitted["status"], "REVIEW")
            self.assertEqual(submitted["pr"], REPO + "#7")

    def test_pause_stop_switch_and_stale_recovery_require_explicit_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = task_dispatch.DispatchStore(Path(tmp) / "state.json")
            store.sync(monitor_report(), {REPO + "#2": "computer", REPO + "#4": "mobile"}, now=NOW)
            store.pause("maintenance")
            with self.assertRaises(task_dispatch.DispatchError):
                store.claim(REPO + "#2", "computer", now=NOW)
            store.resume(now=NOW)
            store.claim(REPO + "#2", "computer", now=NOW)
            recovered = store.recover(stale_after=30, now="2026-10-12T12:01:00Z")
            self.assertEqual(recovered["recovered"], [REPO + "#2"])
            with self.assertRaises(task_dispatch.DispatchError):
                store.claim(REPO + "#2", "computer", now=NOW)
            retry = store.claim(REPO + "#2", "computer", now=NOW, allow_retry=True)
            self.assertEqual(retry["attempt"], 2)
            self.assertEqual(Path(tmp, "state.json").stat().st_mode & 0o777, 0o600)

    def test_fixed_server_executor_runs_real_child_and_records_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_path = root / "report.json"
            report_path.write_text(json.dumps(monitor_report()), encoding="utf-8")
            store = task_dispatch.DispatchStore(root / "state.json")
            store.sync(monitor_report(), {REPO + "#2": "computer", REPO + "#4": "mobile"}, now=NOW)
            evidence = task_dispatch.run_next_server_test(store, report_path, timeout=10)
            self.assertEqual(evidence["runtime_status"], "QUEUE_ONLY")
            self.assertEqual(evidence["selected_task_id"], SERVER_REPO + "#10")
            record = evidence["task"]
            self.assertEqual(record["status"], "DONE")
            self.assertEqual(record["result"]["executor"], "server_health_check-v1")
            self.assertEqual(record["result"]["result"], "healthy")
            self.assertEqual(record["attempt"], 1)
            self.assertEqual(store.summary()["runtime_status"], "QUEUE_ONLY")

    def test_server_runner_rejects_unknown_or_non_server_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = task_dispatch.DispatchStore(Path(tmp) / "state.json")
            store.sync(monitor_report(), {REPO + "#2": "computer"}, now=NOW)
            with self.assertRaises(task_dispatch.DispatchError):
                task_dispatch.run_server_test(store, REPO + "#2", Path(tmp) / "report.json")
            with self.assertRaises(task_dispatch.DispatchError):
                store.claim(REPO + "#8", "server", now=NOW)

    def test_planner_metadata_is_bounded_and_persisted_without_claiming(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = task_dispatch.DispatchStore(Path(tmp) / "state.json")
            store.sync(monitor_report(), {REPO + "#2": "computer", REPO + "#4": "mobile"}, now=NOW)
            planner = store.set_planner_metadata(
                SERVER_REPO + "#10",
                {"status": "PLANNED", "plan_id": "auto-10", "summary": "safe helper"},
            )
            self.assertEqual(planner["status"], "PLANNED")
            self.assertEqual(store.get(SERVER_REPO + "#10")["status"], "PENDING")
            self.assertEqual(store.get(SERVER_REPO + "#10")["planner"]["plan_id"], "auto-10")

    def test_verified_source_completion_resolves_stale_local_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = task_dispatch.DispatchStore(Path(tmp) / "state.json")
            task_id = SERVER_REPO + "#10"
            store.sync(monitor_report(), now=NOW)
            store.claim(task_id, "server", now=NOW)
            store.start(task_id, "server", now=NOW)
            store.submit(task_id, "server", "BLOCKED", result={"reason": "task_not_eligible"}, now=NOW)

            completed = monitor_report()
            source_task = completed["repositories"][1]["tasks"][0]
            source_task.update({
                "state": "closed",
                "status": "DONE",
                "status_verified": True,
                "updated_at": "2026-10-13T18:00:00Z",
            })
            store.sync(completed, now="2026-10-13T18:00:05Z")

            record = store.get(task_id)
            self.assertEqual(record["status"], "DONE")
            self.assertTrue(record["source_status_verified"])
            self.assertEqual(record["result"]["reason"], "source_status_done")

    def test_corrected_ineligible_task_is_requeued_after_issue_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = task_dispatch.DispatchStore(Path(tmp) / "state.json")
            task_id = SERVER_REPO + "#10"
            initial = monitor_report()
            initial["repositories"][1]["tasks"][0]["updated_at"] = "2026-10-13T17:00:00Z"
            store.sync(initial, now=NOW)
            store.claim(task_id, "server", now=NOW)
            store.start(task_id, "server", now=NOW)
            store.submit(
                task_id,
                "server",
                "BLOCKED",
                result={"reason": "task_not_eligible", "approval_state": "NOT_REQUIRED"},
                now=NOW,
            )

            corrected = monitor_report()
            corrected["repositories"][1]["tasks"][0]["updated_at"] = "2026-10-13T17:05:00Z"
            store.sync(corrected, now="2026-10-13T17:05:01Z")

            record = store.get(task_id)
            self.assertEqual(record["status"], "PENDING")
            self.assertEqual(record["result"], {})


if __name__ == "__main__":
    unittest.main()
