import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError, URLError


SPEC = importlib.util.spec_from_file_location(
    "github_coordination", Path(__file__).resolve().parents[1] / "tools/github_coordination.py"
)
coordination = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(coordination)


REPO_A = "example/alpha"
REPO_B = "example/beta"
SHA_A = "a" * 40
SHA_B = "b" * 40
PR_SHA_A = "c" * 40
PR_SHA_B = "d" * 40
MAIN_SHA = "e" * 40


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.payload


class GithubApiFixture:
    def __init__(self, repository, fail_coordination=False, labeled=False):
        self.repository = repository
        self.fail_coordination = fail_coordination
        self.labeled = labeled
        self.calls = []
        self.suffix = repository.rsplit("/", 1)[-1]
        self.task_sha = SHA_A if repository == REPO_A else SHA_B
        self.pr_sha = PR_SHA_A if repository == REPO_A else PR_SHA_B

    def get(self, path, params=None):
        self.calls.append((path, params or {}))
        prefix = "/repos/" + self.repository
        if self.fail_coordination and path == prefix + "/issues/9":
            raise coordination.ApiError(path, status=404)
        if path == prefix + "/issues/9":
            return {
                "number": 9,
                "title": "Coordination " + self.suffix,
                "state": "open",
                "user": {"login": "server-bot"},
                "body": (
                    "[task](https://github.com/%s/issues/10) "
                    "[PR](https://github.com/%s/pull/8)" % (self.repository, self.repository)
                ),
                "updated_at": "2026-10-12T00:00:00Z",
            }
        if path == prefix + "/issues/9/comments":
            return []
        if path == prefix + "/issues/10":
            return {
                "number": 10,
                "title": "Task " + self.suffix,
                "state": "open",
                "user": {"login": "server-bot"},
                "created_at": "2026-10-12T00:00:30Z",
                "body": (
                    "工作端/唯一写入者：服务器端\n"
                    "当前状态：执行中\n"
                    "origin/main 基线完整 SHA：" + self.task_sha + "\n"
                    "分支/PR、最新 HEAD SHA：feat/%s / PR #8 / %s\n"
                    "是否需要维护者审批：是（合并）\n"
                    "repo-only body %s"
                    % (self.suffix, self.pr_sha, self.suffix)
                ),
                "updated_at": "2026-10-12T00:01:00Z",
            }
        if path == prefix + "/issues/10/comments":
            return []
        if path == prefix + "/pulls":
            return []
        if path == prefix + "/issues":
            if self.labeled:
                return [{
                    "number": 11,
                    "title": "Label task " + self.suffix,
                    "state": "open",
                    "labels": [{"name": "codex-task"}],
                    "body": (
                        "```codex-task\n"
                        '{"target":"server","repository":"%s"}\n'
                        "```" % self.repository
                    ),
                }]
            return []
        if path == prefix + "/issues/11":
            return {
                "number": 11,
                "title": "Label task " + self.suffix,
                "state": "open",
                "labels": [{"name": "codex-task"}],
                "body": (
                    "```codex-task\n"
                    '{"target":"server","repository":"%s"}\n'
                    "```" % self.repository
                ),
                "updated_at": "2026-10-12T00:03:00Z",
            }
        if path == prefix + "/issues/11/comments":
            return []
        if path == prefix + "/branches/main":
            return {"name": "main", "commit": {"sha": MAIN_SHA}}
        if path == prefix + "/pulls/8":
            return {
                "number": 8,
                "title": "PR " + self.suffix,
                "state": "open",
                "draft": True,
                "merged": False,
                "head": {"ref": "feat/" + self.suffix, "sha": self.pr_sha},
                "base": {"ref": "main"},
                "updated_at": "2026-10-12T00:02:00Z",
            }
        if path == prefix + "/pulls/8/reviews":
            return [{"state": "COMMENTED", "submitted_at": "2026-10-12T00:02:30Z"}]
        if path == prefix + "/commits/" + self.pr_sha + "/status":
            return {"state": "success", "statuses": [{"state": "success"}]}
        if path == prefix + "/actions/runs":
            return {"workflow_runs": [{"name": "checks", "status": "completed", "conclusion": "success"}]}
        raise AssertionError("unexpected API call: %s %s" % (path, params))


class GithubCoordinationTests(unittest.TestCase):
    def make_monitor(self, repository, fail_coordination=False):
        return coordination.Monitor(
            GithubApiFixture(repository, fail_coordination=fail_coordination),
            repository,
            coordination_issue=9,
            authorized_handoff_users=("server-bot",),
        )

    def test_labelled_task_is_discovered_without_manual_issue_number(self):
        monitor = coordination.Monitor(
            GithubApiFixture(REPO_A, labeled=True),
            REPO_A,
            coordination_issue=9,
            task_label="codex-task",
        )
        report = monitor.collect()
        self.assertEqual([task["issue"] for task in report["tasks"]], [10, 11])
        self.assertEqual(report["tasks"][1]["target"], "server")
        self.assertTrue(report["tasks"][1]["target_verified"])

    def test_task_label_is_retained_in_normalized_specs(self):
        specs = coordination.normalize_repository_specs({
            "repositories": [{"repository": REPO_A, "token_env": "GH_TOKEN_A", "task_label": "codex-task"}]
        })
        self.assertEqual(specs[0]["task_label"], "codex-task")

    def test_handoff_parser_is_bounded_and_never_executes_comment_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "should-not-exist"
            text = (
                "工作端/唯一写入者：服务器端\n"
                "当前状态：执行中;$(touch " + str(marker) + ")\n"
                + "x" * 10000
            )
            fields = coordination.parse_handoff_fields(text, max_chars=240)
            self.assertEqual(fields["owner"], "服务器端")
            self.assertLessEqual(len(fields["status"]), 240)
            self.assertFalse(marker.exists())

    def test_latest_client_handoff_event_is_validated_matched_and_deduplicated(self):
        first = {
            "task_id": REPO_A + "#10",
            "target": "computer",
            "status": "RUNNING",
            "event_id": "evt-1234567890abcdef",
        }
        latest = {
            "task_id": REPO_A + "#10",
            "target": "mobile",
            "status": "REVIEW",
            "event_id": "evt-abcdef1234567890",
            "commit": "ABCDEF1",
        }
        comments = [
            {
                "source_issue_ref": REPO_A + "#10",
                "created_at": "2026-10-12T00:01:00Z",
                "body": "```handoff-event\n%s\n```" % json.dumps(first),
            },
            {
                "source_issue_ref": REPO_A + "#10",
                "created_at": "2026-10-12T00:02:00Z",
                "body": "```handoff-event\n%s\n```" % json.dumps(latest),
            },
            {
                "source_issue_ref": REPO_A + "#10",
                "created_at": "2026-10-12T00:03:00Z",
                "body": "```handoff-event\n%s\n```"
                % json.dumps(dict(first, status="DONE")),
            },
        ]
        events = coordination.latest_client_handoff_events(
            comments, (REPO_A + "#10",)
        )
        self.assertEqual(events[REPO_A + "#10"], dict(latest, commit="abcdef1"))

    def test_client_handoff_ignores_malformed_and_mismatched_events(self):
        mismatched = {
            "task_id": REPO_B + "#10",
            "target": "computer",
            "status": "DONE",
            "event_id": "evt-1234567890abcdef",
        }
        comments = [{
            "source_issue_ref": REPO_A + "#10",
            "created_at": "2026-10-12T00:01:00Z",
            "body": (
                "```handoff-event\nnot json\n```\n"
                "```handoff-event\n%s\n```" % json.dumps(mismatched)
            ),
        }]
        self.assertEqual(
            coordination.latest_client_handoff_events(comments, (REPO_A + "#10",)),
            {},
        )

    def test_client_handoff_rejects_event_from_different_issue_source(self):
        forged = {
            "task_id": REPO_A + "#11",
            "target": "computer",
            "status": "DONE",
            "event_id": "evt-abcdefabcdefabcd",
        }
        comments = [{
            "source_issue_ref": REPO_A + "#10",
            "created_at": "2026-10-12T00:01:00Z",
            "body": "```handoff-event\n%s\n```" % json.dumps(forged),
        }]
        self.assertEqual(
            coordination.latest_client_handoff_events(
                comments, (REPO_A + "#10", REPO_A + "#11")
            ),
            {},
        )

    def test_coordination_comment_cannot_forge_task_event(self):
        forged = {
            "task_id": REPO_A + "#10",
            "target": "computer",
            "status": "DONE",
            "event_id": "evt-0123456789abcdef",
        }

        class CoordinationEventFixture(GithubApiFixture):
            def get(self, path, params=None):
                if path == "/repos/%s/issues/9/comments" % REPO_A:
                    return [{
                        "created_at": "2026-10-12T00:04:00Z",
                        "body": "```handoff-event\n%s\n```" % json.dumps(forged),
                    }]
                return super().get(path, params)

        task = self.make_monitor(REPO_A)
        task.api = CoordinationEventFixture(REPO_A)
        self.assertNotIn("client_handoff_event", task.collect()["tasks"][0])

    def test_monitor_attaches_event_without_changing_verified_task_state(self):
        event = {
            "task_id": REPO_A + "#10",
            "target": "mobile",
            "status": "DONE",
            "event_id": "evt-fedcba0987654321",
        }

        class EventFixture(GithubApiFixture):
            def get(self, path, params=None):
                if path == "/repos/%s/issues/10/comments" % REPO_A:
                    return [{
                        "user": {"login": "untrusted-client"},
                        "created_at": "2026-10-12T00:04:00Z",
                        "body": "当前状态：已完成\n```handoff-event\n%s\n```"
                        % json.dumps(event),
                    }]
                return super().get(path, params)

        monitor = coordination.Monitor(
            EventFixture(REPO_A),
            REPO_A,
            coordination_issue=9,
            authorized_handoff_users=("server-bot",),
        )
        task = monitor.collect()["tasks"][0]
        self.assertEqual(task["client_handoff_event"], event)
        self.assertEqual(task["owner"], "服务器端")
        self.assertEqual(task["status"], "执行中")
        self.assertFalse(task["handoff_verified"])

    def test_client_retries_rate_limit_then_returns_json(self):
        sleeps = []
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, timeout))
            if len(calls) == 1:
                raise HTTPError(
                    request.full_url,
                    429,
                    "rate limited",
                    {"Retry-After": "0"},
                    io.BytesIO(b"{}"),
                )
            return Response({"ok": True})

        client = coordination.GithubClient(
            REPO_A, opener=opener, sleep=sleeps.append, max_retries=2
        )
        self.assertEqual(client.get("/test"), {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [0.0])

    def test_client_retries_network_failure(self):
        sleeps = []
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise URLError("temporary network failure")
            return Response({"ok": True})

        client = coordination.GithubClient(
            REPO_A, opener=opener, sleep=sleeps.append, max_retries=1
        )
        self.assertEqual(client.get("/test"), {"ok": True})
        self.assertEqual(sleeps, [1.0])

    def test_client_reports_forbidden_without_exposing_token(self):
        secret = "ghp_" + "forbidden_secret"

        def opener(request, timeout):
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.headers.get("Authorization"), "Bearer " + secret)
            raise HTTPError(request.full_url, 403, "forbidden", {}, io.BytesIO(b"private body"))

        client = coordination.GithubClient(
            REPO_A, token=secret, opener=opener, max_retries=0
        )
        with self.assertRaises(coordination.ApiError) as context:
            client.get("/private")
        self.assertEqual(context.exception.status, 403)
        self.assertNotIn(secret, context.exception.public_message)

    def test_api_base_requires_explicit_https_host(self):
        with self.assertRaises(coordination.CoordinationError):
            coordination.GithubClient(REPO_A, api_base="http://api.github.com")
        with self.assertRaises(coordination.CoordinationError):
            coordination.GithubClient(REPO_A, api_base="https://evil.example")

    def test_pages_stop_after_short_page(self):
        requests = []

        def opener(request, timeout):
            query = dict(item.split("=") for item in request.full_url.split("?", 1)[1].split("&"))
            page = int(query["page"])
            requests.append(page)
            return Response(list(range(100)) if page == 1 else [101])

        client = coordination.GithubClient(REPO_A, opener=opener, max_retries=0)
        rows = list(client.pages("/items", max_pages=3))
        self.assertEqual(len(rows), 101)
        self.assertEqual(requests, [1, 2])

    def test_state_store_deduplicates_across_process_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            first = coordination.StateStore(path)
            report = {"repository": REPO_A, "tasks": [{"issue": 10, "status": "执行中"}]}
            self.assertTrue(first.record(report))
            self.assertFalse(first.record(report))
            second = coordination.StateStore(path)
            self.assertFalse(second.record(report))
            changed = {"repository": REPO_A, "tasks": [{"issue": 10, "status": "待审核"}]}
            self.assertTrue(second.record(changed))

    def test_multi_repository_state_is_isolated_and_restart_safe(self):
        collection = coordination.MultiRepositoryMonitor(
            [self.make_monitor(REPO_A), self.make_monitor(REPO_B)]
        ).collect()
        self.assertTrue(collection["repositories"])
        with tempfile.TemporaryDirectory() as tmp:
            store = coordination.StateStore(Path(tmp) / "state.json")
            self.assertTrue(store.record_collection(collection))
            self.assertFalse(store.record_collection(collection))
            saved = json.loads((Path(tmp) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(set(saved["repositories"]), {REPO_A, REPO_B})
            self.assertNotEqual(
                saved["repositories"][REPO_A]["event_keys"],
                saved["repositories"][REPO_B]["event_keys"],
            )

    def test_sensitive_values_are_redacted_before_reporting(self):
        value = coordination.redact_sensitive(
            "Authorization: Bearer ghp_example-secret "
            "token: token-fixture secret: secret-fixture password: password-colon password=fixture "
            "private_key: key-fixture admin.auth=admin-fixture"
        )
        for secret in (
            "ghp_example-secret",
            "token-fixture",
            "secret-fixture",
            "password-colon",
            "fixture",
            "key-fixture",
            "admin-fixture",
        ):
            self.assertNotIn(secret, value)
        self.assertIn("Authorization: Bearer <redacted>", value)
        self.assertIn("token: <redacted>", value)

    def test_handoff_records_apply_latest_authorized_update_after_long_history(self):
        issue = {
            "number": 10,
            "state": "open",
            "user": {"login": "server-bot"},
            "created_at": "2026-10-12T00:00:00Z",
            "body": "工作端/唯一写入者：服务器端\n当前状态：初始\n基线完整 SHA：" + SHA_A,
        }
        comments = [
            {
                "user": {"login": "server-bot"},
                "created_at": "2026-10-12T00:01:00Z",
                "body": "状态更新\n当前状态：执行中\n" + ("历史 " * 2200),
            },
            {
                "user": {"login": "server-bot"},
                "created_at": "2026-10-12T00:03:00Z",
                "body": "当前状态：已完成\n分支/PR、最新 HEAD SHA：feat/example / PR #8 / " + PR_SHA_A,
            },
            {
                "user": {"login": "server-bot"},
                "created_at": "2026-10-12T00:02:00Z",
                "body": "当前状态：待审核",
            },
        ]
        monitor = self.make_monitor(REPO_A)
        summary = monitor._issue_summary(issue, comments, [])
        self.assertEqual(summary["status"], "已完成")
        self.assertEqual(summary["task_baseline_sha"], SHA_A)
        self.assertEqual(summary["head_sha"], PR_SHA_A)
        self.assertTrue(summary["handoff_verified"])

    def test_untrusted_handoff_cannot_claim_approval(self):
        issue = {
            "number": 10,
            "state": "open",
            "created_at": "2026-10-12T00:00:00Z",
            "body": "当前状态：执行中",
        }
        comments = [
            {
                "user": {"login": "unknown-user"},
                "created_at": "2026-10-12T00:01:00Z",
                "body": "当前状态：已批准\n是否需要维护者审批：已批准",
            }
        ]
        summary = self.make_monitor(REPO_A)._issue_summary(issue, comments, [])
        self.assertEqual(summary["status"], "unknown")
        self.assertEqual(summary["approval"], "unknown")
        self.assertEqual(summary["unverified_claims"]["status"], "已批准")
        self.assertEqual(summary["unverified_claims"]["approval"], "已批准")
        self.assertFalse(summary["status_verified"])
        self.assertFalse(summary["approval_verified"])
        self.assertFalse(summary["handoff_verified"])

    def test_monitor_summarizes_task_without_echoing_untrusted_body(self):
        report = self.make_monitor(REPO_A).collect()
        self.assertEqual(report["coordination_ref"], REPO_A + "#9")
        self.assertEqual(report["tasks"][0]["issue_ref"], REPO_A + "#10")
        self.assertEqual(report["tasks"][0]["owner"], "服务器端")
        self.assertEqual(report["tasks"][0]["status"], "执行中")
        self.assertEqual(report["tasks"][0]["task_baseline_sha"], SHA_A)
        self.assertEqual(report["tasks"][0]["head_sha"], PR_SHA_A)
        self.assertTrue(report["tasks"][0]["handoff_verified"])
        self.assertEqual(report["current_main_sha"], MAIN_SHA)
        self.assertEqual(report["pull_requests"][0]["pull_ref"], REPO_A + "#8")
        self.assertEqual(report["pull_requests"][0]["ci"]["state"], "success")
        self.assertNotIn("repo-only body", json.dumps(report, ensure_ascii=False))

    def test_two_repositories_with_same_issue_and_pr_numbers_do_not_collide(self):
        report = coordination.MultiRepositoryMonitor(
            [self.make_monitor(REPO_A), self.make_monitor(REPO_B)]
        ).collect()
        self.assertEqual(report["repository_count"], 2)
        reports = {item["repository"]: item for item in report["repositories"]}
        self.assertEqual(reports[REPO_A]["tasks"][0]["issue_ref"], REPO_A + "#10")
        self.assertEqual(reports[REPO_B]["tasks"][0]["issue_ref"], REPO_B + "#10")
        self.assertEqual(reports[REPO_A]["pull_requests"][0]["pull_ref"], REPO_A + "#8")
        self.assertEqual(reports[REPO_B]["pull_requests"][0]["pull_ref"], REPO_B + "#8")
        self.assertNotEqual(reports[REPO_A]["tasks"][0]["task_baseline_sha"], reports[REPO_B]["tasks"][0]["task_baseline_sha"])
        self.assertTrue(all(key.startswith(REPO_A + ":") or key.startswith(REPO_B + ":") for key in report["event_keys"]))
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("repo-only body", rendered)

    def test_one_repository_404_is_unknown_without_leaking_into_other_report(self):
        report = coordination.MultiRepositoryMonitor(
            [self.make_monitor(REPO_A), self.make_monitor(REPO_B, fail_coordination=True)]
        ).collect()
        reports = {item["repository"]: item for item in report["repositories"]}
        self.assertEqual(len(reports[REPO_A]["tasks"]), 1)
        self.assertEqual(reports[REPO_B]["current_main_sha"], "unknown")
        self.assertTrue(any("status=404" in warning for warning in reports[REPO_B]["warnings"]))
        self.assertNotIn(REPO_A, json.dumps(reports[REPO_B], ensure_ascii=False))

    def test_normalize_supports_old_single_repo_config_and_rejects_duplicates(self):
        specs = coordination.normalize_repository_specs(
            {"repository": REPO_A, "coordination_issue": 9, "main_branch": "main"}
        )
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["repository"], REPO_A)
        self.assertEqual(specs[0]["token_env"], "GH_TOKEN")
        specs = coordination.normalize_repository_specs(
            {
                "repositories": [
                    {"repository": REPO_A, "token_env": "GH_TOKEN_ALPHA"},
                    {"repository": REPO_B, "token_env": "GH_TOKEN_BETA"},
                ]
            }
        )
        self.assertEqual([item["repository"] for item in specs], [REPO_A, REPO_B])
        with self.assertRaises(coordination.CoordinationError):
            coordination.normalize_repository_specs(
                {"repositories": [{"repository": REPO_A}, {"repository": REPO_B}]}
            )
        with self.assertRaises(coordination.CoordinationError):
            coordination.normalize_repository_specs(
                {
                    "repositories": [
                        {"repository": REPO_A, "token_env": "GH_TOKEN_SHARED"},
                        {"repository": REPO_B, "token_env": "GH_TOKEN_SHARED"},
                    ]
                }
            )
        shared = coordination.normalize_repository_specs(
            {
                "allow_shared_token_env": True,
                "repositories": [
                    {"repository": REPO_A, "token_env": "GH_TOKEN_SHARED"},
                    {"repository": REPO_B, "token_env": "GH_TOKEN_SHARED"},
                ],
            }
        )
        self.assertEqual(shared[0]["token_env"], shared[1]["token_env"])
        with self.assertRaises(coordination.CoordinationError):
            coordination.normalize_repository_specs(
                {
                    "repositories": [
                        {"repository": REPO_A, "token_env": "GH_TOKEN_A"},
                        {"repository": REPO_A, "token_env": "GH_TOKEN_B"},
                    ]
                }
            )


if __name__ == "__main__":
    unittest.main()
