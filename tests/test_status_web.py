import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


TOOLS = str(Path(__file__).resolve().parents[1] / "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import status_web


NOW = 1000
WEB_STATUS = Path(__file__).resolve().parents[1] / "web" / "status"


def render_app(payload):
    script = r'''
const payload = JSON.parse(process.argv[1]);
class Element {
  constructor(tag, id) {
    this.tagName = tag.toUpperCase(); this.id = id || ""; this.children = [];
    this.attributes = {}; this.listeners = {}; this.className = ""; this.textContent = "";
    this.disabled = false; this.value = "";
    this.classList = {
      add: (...names) => names.forEach((name) => this._classes().add(name)),
      remove: (...names) => names.forEach((name) => this._classes().delete(name)),
      toggle: (name, force) => { const set = this._classes(); force ? set.add(name) : set.delete(name); },
    };
  }
  _classes() {
    const set = new Set(this.className.split(/\s+/).filter(Boolean));
    const sync = (method) => (...args) => { const result = Set.prototype[method].apply(set, args); this.className = [...set].join(" "); return result; };
    set.add = sync("add"); set.delete = sync("delete"); return set;
  }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); return child; }
  get firstChild() { return this.children[0] || null; }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  getAttribute(name) { return this.attributes[name]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  scrollIntoView() {}
}
const ids = {};
["refresh","search","repo-filter","filters","stage-filter","sync","sync-text","origin","warning","pull-count",
 "owners","tasks","task-count","unassigned-count","unassigned-tasks","pulls","tasks-heading"].forEach((id) => ids[id] = new Element("div", id));
const filters = ["attention","all","server","computer","mobile","approval","completed_failed","unknown"].map((key) => {
  const item = new Element("option"); item.value = key; item.setAttribute("data-filter", key); return item;
});
const stages = ["all","pending","claimed","running","review","blocked","failed","completed","unknown"].map((key) => {
  const item = new Element("option"); item.value = key; item.setAttribute("data-status-filter", key); return item;
});
global.document = {
  hidden: false,
  createElement: (tag) => new Element(tag),
  getElementById: (id) => ids[id],
  querySelectorAll: (selector) => selector === "#filters option" ? filters :
    selector === "#stage-filter option" ? stages : [],
};
global.fetch = () => Promise.resolve({ok: true, json: () => Promise.resolve(payload)});
global.setInterval = () => 0;
require(process.argv[2]);
function flatten(root, out) { out.push(root); root.children.forEach((child) => flatten(child, out)); return out; }
setImmediate(() => setImmediate(() => {
  const initial = flatten(ids.tasks, []);
  const initialCards = ids.tasks.children.length;
  ids.filters.value = "completed_failed";
  ids.filters.listeners.change({target: ids.filters});
  const completed = flatten(ids.tasks, []);
  process.stdout.write(JSON.stringify({
    initialText: initial.map((item) => item.textContent).filter(Boolean),
    links: initial.filter((item) => item.tagName === "A").map((item) => ({text: item.textContent, href: item.href})),
    initialCards,
    completedText: completed.map((item) => item.textContent).filter(Boolean),
    completedCards: ids.tasks.children.length,
  }));
}));
'''
    completed = subprocess.run(
        ["node", "-e", script, json.dumps(payload), str(WEB_STATUS / "app.js")],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return json.loads(completed.stdout.decode("utf-8"))


def report():
    return {
        "schema_version": 3,
        "generated_at": "1970-01-01T00:16:40Z",
        "repository_count": 1,
        "warnings": [],
        "repositories": [{
            "repository": "example/hub",
            "coordination_ref": "example/hub#1",
            "coordination_state": "open",
            "main_branch": "main",
            "current_main_sha": "a" * 40,
            "warnings": [],
            "tasks": [
                {"issue": 1, "issue_ref": "example/hub#1", "title": "server task", "state": "open", "owner": "服务器端", "status": "待执行", "updated_at": "1970-01-01T00:16:00Z", "pull_requests": []},
                {"issue": 2, "issue_ref": "example/hub#2", "title": "computer task", "state": "open", "owner": "电脑端", "status": "待测试", "updated_at": "1970-01-01T00:15:00Z", "pull_requests": [7]},
                {"issue": 3, "issue_ref": "example/hub#3", "title": "mobile task", "state": "open", "owner": "server", "status": "待手机端审核", "updated_at": "1970-01-01T00:14:00Z", "pull_requests": []},
                {"issue": 4, "issue_ref": "example/hub#4", "title": "approval task", "state": "open", "owner": "server", "status": "进行中", "approval": "待维护者批准", "updated_at": "1970-01-01T00:13:00Z", "pull_requests": []},
                {"issue": 5, "issue_ref": "example/hub#5", "title": "done task", "state": "closed", "owner": "server", "status": "已完成", "updated_at": "1970-01-01T00:12:00Z", "pull_requests": []},
                {"issue": 6, "issue_ref": "example/hub#6", "title": "blocked task", "state": "open", "owner": "unknown", "status": "BLOCKED", "updated_at": "1970-01-01T00:11:00Z", "pull_requests": []},
                {"issue": 8, "issue_ref": "example/hub#8", "title": "unknown task", "state": "open", "owner": "unknown", "status": "unknown", "updated_at": "1970-01-01T00:10:00Z", "pull_requests": []},
            ],
            "pull_requests": [{
                "number": 7,
                "title": "safe PR",
                "state": "open",
                "draft": True,
                "merged": False,
                "branch": "feat/task",
                "base": "main",
                "head_sha": "b" * 40,
                "updated_at": "1970-01-01T00:15:00Z",
                "ci": {"state": "success", "status_count": 1, "workflow_runs": []},
            }],
        }],
    }


class StatusWebTests(unittest.TestCase):
    def test_frontend_is_device_first_and_hides_secondary_details(self):
        html = (WEB_STATUS / "index.html").read_text(encoding="utf-8")
        self.assertIn("多端协作中心", html)
        self.assertIn('<header class="topbar">', html)
        self.assertIn('<a class="brand" href="#top">多端协作中心</a>', html)
        self.assertIn('id="refresh" class="refresh"', html)
        self.assertNotIn("side-nav", html)
        self.assertIn('id="stage-filter"', html)
        self.assertNotIn("status-filters", html)
        self.assertLess(html.index('id="owners"'), html.index('id="tasks"'))
        self.assertLess(html.index('id="tasks"'), html.index('id="task-controls"'))
        self.assertLess(html.index('id="task-controls"'), html.index('id="pulls-section"'))
        self.assertNotIn('id="summary-section"', html)
        self.assertIn('<details id="pulls-section"', html)
        self.assertIn('data-filter="attention"', html)
        app = (WEB_STATUS / "app.js").read_text(encoding="utf-8")
        self.assertIn("targetLabel(task)", app)
        self.assertIn('node("details", "task-card "', app)
        self.assertIn('githubLink("打开 Issue", task.url', app)

    def test_frontend_preserves_safe_relative_read_only_data_flow(self):
        app = (WEB_STATUS / "app.js").read_text(encoding="utf-8")
        self.assertIn('fetch("./api/refresh?', app)
        self.assertIn("credentials: \"same-origin\"", app)
        self.assertIn("textContent", app)
        self.assertIn("function taskStatusClass", app)
        self.assertNotIn("innerHTML", app)

    def test_frontend_keeps_mobile_focus_and_reduced_motion_guards(self):
        css = (WEB_STATUS / "styles.css").read_text(encoding="utf-8")
        self.assertIn("@media (max-width: 700px)", css)
        self.assertIn("@media (max-width: 355px)", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("focus-visible", css)

    def test_projection_groups_real_report_fields_and_builds_links(self):
        payload = status_web.build_dashboard_payload(report(), now=NOW, stale_after=900)
        self.assertEqual(payload["source"], "read_only_monitor_report")
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["summary"]["task_count"], 7)
        self.assertEqual(
            {group["id"]: group["count"] for group in payload["groups"]},
            {"server": 1, "computer": 1, "mobile": 1, "approval": 1, "completed_failed": 2, "unknown": 1},
        )
        task = next(item for item in payload["tasks"] if item["issue"] == 2)
        self.assertEqual(task["url"], "https://github.com/example/hub/issues/2")
        self.assertEqual(task["pull_requests"][0]["url"], "https://github.com/example/hub/pull/7")
        self.assertEqual(payload["pull_requests"][0]["ci"]["state"], "success")

    def test_projection_redacts_token_like_report_text(self):
        value = report()
        value["repositories"][0]["tasks"][0]["title"] = "ghp_dashboard-secret"
        payload = status_web.build_dashboard_payload(value, now=NOW)
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("ghp_dashboard-secret", rendered)
        self.assertIn("<redacted>", rendered)

    def test_projection_exposes_validated_client_handoff_without_replacing_task_state(self):
        value = report()
        value["repositories"][0]["tasks"][0].update({
            "owner_verified": True,
            "status_verified": True,
            "client_handoff_event": {
                "task_id": "example/hub#1",
                "target": "mobile",
                "status": "DONE",
                "event_id": "evt-1234567890abcdef",
                "pull_request_url": "https://github.com/example/hub/pull/9",
                "commit": "ABCDEF1",
            },
        })
        payload = status_web.build_dashboard_payload(value, now=NOW)
        task = next(item for item in payload["tasks"] if item["issue"] == 1)
        self.assertEqual(task["owner"], "服务器端")
        self.assertEqual(task["status"], "待执行")
        self.assertEqual(task["status_kind"], "server")
        self.assertEqual(task["client_handoff_event"]["status"], "DONE")
        self.assertEqual(task["client_handoff_event"]["commit"], "abcdef1")
        self.assertEqual(
            task["client_handoff_event"]["pull_request_url"],
            "https://github.com/example/hub/pull/9",
        )
        dispatched = status_web.build_dashboard_payload(
            value,
            now=NOW,
            dispatch_state={"tasks": {"example/hub#1": {
                "task_id": "example/hub#1", "target": "computer", "status": "RUNNING",
            }}},
        )
        task = next(item for item in dispatched["tasks"] if item["issue"] == 1)
        self.assertEqual(task["owner"], "电脑端")
        self.assertEqual(task["status"], "电脑执行中")
        self.assertEqual(task["client_handoff_event"]["status"], "DONE")

    def test_projection_rejects_invalid_or_mismatched_client_handoff(self):
        for event in (
            {"task_id": "example/hub#1", "target": "server", "status": "DONE", "event_id": "bad"},
            {"task_id": "example/hub#2", "target": "mobile", "status": "DONE", "event_id": "evt-1234567890abcdef"},
        ):
            with self.subTest(event=event):
                value = report()
                value["repositories"][0]["tasks"][0]["client_handoff_event"] = event
                payload = status_web.build_dashboard_payload(value, now=NOW)
                task = next(item for item in payload["tasks"] if item["issue"] == 1)
                self.assertNotIn("client_handoff_event", task)

    def test_projection_exposes_local_dispatch_queue_and_manual_targets(self):
        dispatch = {
            "schema_version": 1,
            "paused": True,
            "pause_reason": "maintenance",
            "last_sync_at": "1970-01-01T00:16:40Z",
            "source_generated_at": "1970-01-01T00:16:40Z",
            "tasks": {
                "example/hub#2": {
                    "task_id": "example/hub#2",
                    "target": "computer",
                    "target_source": "target_override",
                    "target_verified": True,
                    "status": "PENDING",
                    "attempt": 0,
                    "result": {"note": "ghp_dispatch_secret"},
                },
            },
        }
        payload = status_web.build_dashboard_payload(report(), now=NOW, dispatch_state=dispatch)
        task = next(item for item in payload["tasks"] if item["issue"] == 2)
        self.assertEqual(task["status"], "待电脑领取")
        self.assertEqual(task["status_kind"], "computer")
        self.assertEqual(task["dispatch"]["target"], "computer")
        self.assertEqual(payload["dispatch"]["task_count"], 1)
        self.assertTrue(payload["dispatch"]["paused"])
        self.assertEqual(payload["dispatch"]["runtime_status"], "QUEUE_ONLY")
        self.assertEqual(task["dispatch"]["result"]["note"], "<redacted>")

    def test_closed_issue_is_presented_as_closed_despite_unfinished_dispatch(self):
        for dispatch_status in ("PENDING", "CLAIMED", "RUNNING", "REVIEW"):
            with self.subTest(dispatch_status=dispatch_status):
                value = report()
                value["repositories"][0]["tasks"][4]["status"] = dispatch_status
                dispatch = {
                    "tasks": {
                        "example/hub#5": {
                            "task_id": "example/hub#5",
                            "target": "server",
                            "status": dispatch_status,
                        },
                    },
                }
                payload = status_web.build_dashboard_payload(value, now=NOW, dispatch_state=dispatch)
                task = next(item for item in payload["tasks"] if item["issue"] == 5)
                self.assertEqual(task["status"], "已关闭")
                self.assertEqual(task["status_kind"], "completed_failed")
                self.assertEqual(task["dispatch"]["status"], dispatch_status)
        value = report()
        value["repositories"][0]["tasks"][4]["status"] = "DONE"
        payload = status_web.build_dashboard_payload(
            value,
            now=NOW,
            dispatch_state={
                "tasks": {
                    "example/hub#5": {
                        "task_id": "example/hub#5",
                        "target": "server",
                        "status": "DONE",
                    },
                },
            },
        )
        task = next(item for item in payload["tasks"] if item["issue"] == 5)
        self.assertEqual(task["status"], "已完成")

    def test_failed_and_blocked_tasks_keep_their_device_target(self):
        value = report()
        value["repositories"][0]["tasks"][5].update({"owner": "server", "status": "BLOCKED"})
        value["repositories"][0]["tasks"][6].update({"owner": "mobile", "status": "FAILED"})
        payload = status_web.build_dashboard_payload(value, now=NOW)
        blocked = next(item for item in payload["tasks"] if item["issue"] == 6)
        failed = next(item for item in payload["tasks"] if item["issue"] == 8)
        self.assertEqual(blocked["status_kind"], "completed_failed")
        self.assertEqual(blocked["owner"], "server")
        self.assertEqual(failed["status_kind"], "completed_failed")
        self.assertEqual(failed["owner"], "mobile")

    def test_frontend_target_cards_and_lists_share_unfinished_rules(self):
        app = (WEB_STATUS / "app.js").read_text(encoding="utf-8")
        self.assertIn("targetKey(item) === key && isUnfinished(item)", app)
        self.assertIn("return targetKey(task) === scope && isUnfinished(task);", app)
        self.assertIn("if (!matchesScope(item, state.filter)) return false;", app)
        self.assertIn('task.state || "").toLowerCase() === "closed"', app)
        self.assertIn('raw === "CLAIMED"', app)
        self.assertIn(
            'function isUnfinished(task) {\n'
            '    return ["pending", "claimed", "running", "review", "blocked", "failed", "unknown"]',
            app,
        )

    def test_frontend_renders_strict_deduplicated_pr_sources_and_unapproved_client_note(self):
        valid = "https://github.com/example/hub/pull/44"
        unsafe = [
            "https://github.com/example/hub/issues/44",
            "https://example-user@github.com/example/hub/pull/45",
            "https://github.com/example/hub/pull/46?x=1",
            "https://github.com/example/hub/pull/47#files",
            "https://github.com:443/example/hub/pull/48",
            "https://evil.example/example/hub/pull/49",
            "http://github.com/example/hub/pull/50",
        ]
        tasks = [{
            "issue": 1, "issue_ref": "example/hub#1", "title": "active", "state": "open",
            "owner": "server", "status": "待执行", "status_kind": "server",
            "status_verified": True, "url": "https://github.com/example/hub/issues/1",
            "dispatch": {"target": "server", "status": "RUNNING", "label": "服务器执行中", "pr": valid},
            "pull_requests": [{"url": valid}] + [{"url": url} for url in unsafe],
        }, {
            "issue": 2, "issue_ref": "example/hub#2", "title": "fallback", "state": "open",
            "owner": "computer", "status": "待测试", "status_kind": "computer",
            "status_verified": True, "url": "https://github.com/example/hub/issues/2",
            "pull_requests": [{"url": "https://github.com/example/hub/pull/7"}],
        }, {
            "issue": 4, "issue_ref": "example/hub#4", "title": "client report", "state": "open",
            "owner": "mobile", "status": "待手机端审核", "status_kind": "mobile",
            "status_verified": True, "url": "https://github.com/example/hub/issues/4",
            "client_handoff_event": {"target": "mobile", "status": "DONE",
                                     "pull_request_url": "https://github.com/example/hub/pull/45"},
            "pull_requests": [{"url": "https://github.com/example/hub/pull/45"}],
        }, {
            "issue": 3, "issue_ref": "example/hub#3", "title": "closed", "state": "closed",
            "owner": "server", "status": "已关闭", "status_kind": "completed_failed",
            "status_verified": True, "url": "https://github.com/example/hub/issues/3",
            "client_handoff_event": {"target": "computer", "status": "RUNNING"}, "pull_requests": [],
        }]
        payload = {
            "source": "test", "sync": {"state": "fresh"},
            "summary": {"task_count": 4, "pull_count": 0}, "tasks": tasks,
            "pull_requests": [], "groups": [], "repositories": [{"repository": "example/hub"}],
        }
        rendered = render_app(payload)
        self.assertEqual(rendered["initialCards"], 3)
        self.assertIn("客户端自报 · 手机 · 已完成 · 未经审批", rendered["initialText"])
        pr_links = [item for item in rendered["links"] if "/pull/" in item.get("href", "")]
        self.assertEqual(sorted(pr_links, key=lambda item: item["href"]), [
            {"text": "调度器 PR", "href": valid},
            {"text": "客户端 PR", "href": "https://github.com/example/hub/pull/45"},
            {"text": "PR #7", "href": "https://github.com/example/hub/pull/7"},
        ])
        self.assertEqual(rendered["completedCards"], 1)
        self.assertIn("已关闭", rendered["completedText"])
        self.assertNotIn("执行中", rendered["completedText"])

    def test_projection_accepts_only_unambiguous_dispatch_pr_url(self):
        valid = "https://github.com/example/coordination-hub/pull/44"
        rejected = [
            "https://github.com/example/coordination-hub/issues/44",
            "https://user@github.com/example/coordination-hub/pull/44",
            valid + "?diff=split", valid + "#files",
            "https://github.com:443/example/coordination-hub/pull/44",
            "https://example.com/example/coordination-hub/pull/44",
            "javascript:alert(1)",
        ]
        for candidate, expected in [(valid, valid)] + [(item, "") for item in rejected]:
            with self.subTest(candidate=candidate):
                payload = status_web.build_dashboard_payload(
                    report(), now=NOW,
                    dispatch_state={"tasks": {"example/hub#1": {
                        "task_id": "example/hub#1", "target": "server", "status": "RUNNING", "pr": candidate,
                    }}},
                )
                task = next(item for item in payload["tasks"] if item["issue"] == 1)
                self.assertEqual(task["dispatch"]["pr"], expected)

    def test_frontend_attention_total_and_filter_match_device_cards(self):
        app = (WEB_STATUS / "app.js").read_text(encoding="utf-8")
        self.assertIn(
            'function attentionTasks() { return tasks().filter(isUnfinished); }',
            app,
        )
        self.assertIn('tasks().filter(function (item) { return matchesScope(item, key); }).length', app)
        self.assertIn('function renderCounts()', app)
        self.assertIn('scope === "completed_failed") return kind(task) === scope;', app)

    def test_frontend_has_counted_unassigned_area_and_empty_state(self):
        html = (WEB_STATUS / "index.html").read_text(encoding="utf-8")
        app = (WEB_STATUS / "app.js").read_text(encoding="utf-8")
        self.assertIn('<details id="unassigned-section"', html)
        self.assertIn('id="unassigned-count"', html)
        self.assertIn('id="unassigned-tasks"', html)
        self.assertIn('targetKey(item) === "unknown" && isUnfinished(item)', app)
        self.assertIn('ids("unassigned-count").textContent = unassigned.length', app)
        self.assertIn("暂无未完成的未分配任务。", app)

    def test_dispatch_snapshot_keeps_last_good_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_path = root / "report.json"
            dispatch_path = root / "dispatch.json"
            report_path.write_text(json.dumps(report()), encoding="utf-8")
            dispatch_path.write_text(json.dumps({"tasks": {}, "paused": False}), encoding="utf-8")
            store = status_web.DashboardStore(
                report_path, clock=lambda: NOW, dispatch_path=dispatch_path
            )
            self.assertTrue(store.payload()["dispatch"]["available"])
            dispatch_path.write_text("not-json", encoding="utf-8")
            stale = store.payload()
            self.assertTrue(stale["dispatch"]["available"])
            self.assertTrue(any("调度状态读取失败" in item for item in stale["sync"]["warnings"]))

    def test_last_good_snapshot_is_retained_when_report_read_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_text(json.dumps(report()), encoding="utf-8")
            store = status_web.DashboardStore(path, clock=lambda: NOW)
            first = store.payload()
            self.assertEqual(first["sync"]["state"], "fresh")
            path.write_text("not-json", encoding="utf-8")
            stale = store.payload()
            self.assertEqual(stale["sync"]["state"], "stale")
            self.assertEqual(stale["summary"]["task_count"], 7)
            self.assertTrue(any("上一次成功快照" in item for item in stale["sync"]["warnings"]))

    def test_unavailable_report_is_explicit_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = status_web.DashboardStore(Path(tmp) / "missing.json").payload()
            self.assertEqual(payload["sync"]["state"], "unavailable")
            self.assertEqual(payload["summary"]["task_count"], 0)
            self.assertEqual(payload["repositories"], [])

    def test_http_surface_is_local_read_only_and_serves_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text("<title>dashboard</title>", encoding="utf-8")
            (root / "app.js").write_text("console.log('ok');", encoding="utf-8")
            (root / "styles.css").write_text("body{}", encoding="utf-8")
            report_path = root / "report.json"
            report_path.write_text(json.dumps(report()), encoding="utf-8")
            server = status_web.StatusHTTPServer(
                ("127.0.0.1", 0), status_web.StatusRequestHandler,
                status_web.DashboardStore(report_path, clock=lambda: NOW), root,
            )
            thread = threading.Thread(target=server.serve_forever)
            thread.daemon = True
            thread.start()
            try:
                base = "http://127.0.0.1:%d" % server.server_port
                self.assertIn("dashboard", urlopen(base + "/", timeout=2).read().decode("utf-8"))
                data = json.loads(urlopen(base + "/api/status", timeout=2).read().decode("utf-8"))
                self.assertEqual(data["summary"]["task_count"], 7)
                request = Request(base + "/api/status", method="POST")
                with self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=2)
                self.assertEqual(error.exception.code, 405)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
