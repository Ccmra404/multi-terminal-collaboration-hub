#!/usr/bin/env python3
"""Serve an authenticated dashboard from the existing monitor report.

The monitor remains the only GitHub reader.  This process reads its private
0600 JSON report and exposes a small, sanitized JSON view to the browser. The
only write surface is one-time maintainer decisions for bounded task plans.
"""

from __future__ import print_function

import argparse
import http.cookies
import json
import mimetypes
import os
import re
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

try:  # ThreadingHTTPServer was added in Python 3.7; the server host also has 3.6.
    from http.server import ThreadingHTTPServer
except ImportError:  # pragma: no cover - exercised on Python 3.6 deployments
    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

try:
    import github_coordination as coordination
except ImportError:  # pragma: no cover - run-from-repository-root convenience
    _tools = str(Path(__file__).resolve().parent)
    if _tools not in sys.path:
        sys.path.insert(0, _tools)
    import github_coordination as coordination

try:
    import status_auth
    import task_approvals
except ImportError:  # pragma: no cover - run-from-repository-root convenience
    _tools = str(Path(__file__).resolve().parent)
    if _tools not in sys.path:
        sys.path.insert(0, _tools)
    import status_auth
    import task_approvals


DEFAULT_REPORT_FILE = "/var/lib/collaboration-hub/once/report.json"
DEFAULT_DISPATCH_FILE = "/var/lib/collaboration-hub/dispatch/state.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18080
DEFAULT_STALE_AFTER = 900
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_DISPATCH_BYTES = 4 * 1024 * 1024
MAX_STATIC_BYTES = 512 * 1024
GROUPS = (
    ("server", "服务器待执行 / 进行中"),
    ("computer", "电脑待领取 / 待测试"),
    ("mobile", "手机待审核"),
    ("approval", "待维护者批准"),
    ("completed_failed", "已完成 / 失败"),
    ("unknown", "未指定 / 未知"),
)


def _clean(value, maximum=500):
    return coordination.clean_text(value, maximum)


def _bounded_json(value, depth=0):
    """Keep local queue result data bounded and token-redacted like reports."""
    if depth > 5:
        return "<truncated>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clean(value, 8000)
    if isinstance(value, list):
        return [_bounded_json(item, depth + 1) for item in value[:50]]
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:50]:
            normalized = _clean(key, 100)
            if normalized:
                result[normalized] = _bounded_json(item, depth + 1)
        return result
    return _clean(value, 240)


def _number(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _iso_seconds(value):
    """Return an epoch timestamp for the monitor's bounded ISO-8601 format."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except (AttributeError, TypeError, ValueError):
        # Python 3.6 compatibility: the server's supported parser is narrow.
        try:
            raw = str(value).replace("Z", "+0000")
            parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _github_url(repository, kind, number):
    number = _number(number)
    if not number or not coordination.REPOSITORY_RE.fullmatch(str(repository or "")):
        return ""
    suffix = "pull" if kind == "pull" else "issues"
    return "https://github.com/%s/%s/%d" % (repository, suffix, number)


def _github_pull_url(value):
    """Accept only an unambiguous canonical GitHub pull-request URL."""
    text = _clean(value, 240)
    try:
        parsed = urlparse(text)
    except (TypeError, ValueError):
        return ""
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*", parsed.path)
    ):
        return ""
    return "https://github.com" + parsed.path


def _status_kind(task):
    status = _clean(task.get("status"), 240).lower()
    state = _clean(task.get("state"), 80).lower()
    owner = _clean(task.get("owner"), 120).lower()
    approval = _clean(task.get("approval"), 240).lower()
    combined = " ".join((status, state))

    if any(word in combined for word in ("failed", "failure", "error", "blocked", "失败", "阻断")):
        return "completed_failed"
    if state == "closed" or any(
        word in combined for word in ("completed", "complete", "done", "已完成", "已关闭", "merged", "通过")
    ):
        return "completed_failed"
    if any(
        word in approval
        for word in ("待批准", "待审批", "待维护者", "需要审批", "需要批准", "approval required", "approve")
    ):
        return "approval"
    if any(word in status for word in ("手机", "移动", "mobile", "审核", "复审")) or "mobile" in owner:
        return "mobile"
    if any(word in status for word in ("电脑", "测试", "computer", "领取")) or "computer" in owner:
        return "computer"
    if any(word in status for word in ("进行", "执行", "待", "pending", "todo", "ready", "unknown")):
        if "server" in owner or "服务器" in owner:
            return "server"
        if "computer" in owner or "电脑" in owner:
            return "computer"
        if "mobile" in owner or "手机" in owner:
            return "mobile"
    if "server" in owner or "服务器" in owner:
        return "server"
    return "unknown"


def _pull_projection(repository, pull):
    number = _number(pull.get("number"))
    ci = pull.get("ci") if isinstance(pull.get("ci"), dict) else {}
    runs = []
    for run in pull.get("ci", {}).get("workflow_runs", []) if isinstance(pull.get("ci"), dict) else []:
        if not isinstance(run, dict):
            continue
        runs.append({
            "name": _clean(run.get("name"), 120),
            "status": _clean(run.get("status"), 40),
            "conclusion": _clean(run.get("conclusion"), 40),
        })
    return {
        "number": number,
        "title": _clean(pull.get("title") or "未命名 PR", 240),
        "state": _clean(pull.get("state") or "unknown", 40),
        "draft": bool(pull.get("draft")),
        "merged": bool(pull.get("merged")),
        "branch": _clean(pull.get("branch") or "unknown", 160),
        "base": _clean(pull.get("base") or "unknown", 80),
        "head_sha": _clean(pull.get("head_sha") or "unknown", 80),
        "updated_at": _clean(pull.get("updated_at") or "unknown", 40),
        "ci": {
            "state": _clean(ci.get("state") or "unknown", 40),
            "status_count": _number(ci.get("status_count")) or 0,
            "workflow_runs": runs[:20],
        },
        "url": _github_url(repository, "pull", number),
    }


def _dispatch_label(target, status):
    labels = {
        "server": {"PENDING": "服务器待执行", "CLAIMED": "服务器已领取", "RUNNING": "服务器执行中"},
        "computer": {"PENDING": "待电脑领取", "CLAIMED": "电脑已领取", "RUNNING": "电脑执行中"},
        "mobile": {"PENDING": "待手机领取", "CLAIMED": "手机已领取", "RUNNING": "手机执行中"},
        "unknown": {"PENDING": "待分配"},
    }
    target = target if target in labels else "unknown"
    return labels[target].get(status, {
        "REVIEW": "待复审",
        "DONE": "已完成",
        "FAILED": "执行失败",
        "BLOCKED": "已阻断",
    }.get(status, status or "未知"))


def _dispatch_projection(dispatch):
    if not isinstance(dispatch, dict):
        return None
    target = _clean(dispatch.get("target") or "unknown", 40).lower()
    if target not in ("server", "computer", "mobile", "unknown"):
        target = "unknown"
    status = _clean(dispatch.get("status") or "PENDING", 40).upper()
    if status not in ("PENDING", "CLAIMED", "RUNNING", "REVIEW", "DONE", "FAILED", "BLOCKED"):
        status = "PENDING"
    return {
        "task_id": _clean(dispatch.get("task_id") or "unknown", 160),
        "target": target,
        "target_source": _clean(dispatch.get("target_source") or "unknown", 80),
        "target_verified": bool(dispatch.get("target_verified")),
        "status": status,
        "label": _dispatch_label(target, status),
        "claimant": _clean(dispatch.get("claimant") or "", 120),
        "claimed_at": _clean(dispatch.get("claimed_at") or "", 40),
        "heartbeat_at": _clean(dispatch.get("heartbeat_at") or "", 40),
        "attempt": max(0, _number(dispatch.get("attempt")) or 0),
        "result": _bounded_json(dispatch.get("result")) if isinstance(dispatch.get("result"), dict) else {},
        "planner": _bounded_json(dispatch.get("planner")) if isinstance(dispatch.get("planner"), dict) else {},
        "commit": _clean(dispatch.get("commit") or "", 120),
        "pr": _github_pull_url(dispatch.get("pr")),
        "log_summary": _clean(dispatch.get("log_summary") or "", 1000),
        "updated_at": _clean(dispatch.get("updated_at") or "", 40),
    }


def _client_handoff_projection(task):
    event = task.get("client_handoff_event")
    if not isinstance(event, dict):
        return None
    try:
        projected = coordination.validate_event(event)
    except (TypeError, ValueError, coordination.HandoffValidationError):
        return None
    if projected["task_id"] != _clean(task.get("issue_ref"), 240):
        return None
    return projected


def _task_projection(repository, task, pulls_by_number, dispatch=None):
    number = _number(task.get("issue"))
    pull_cards = []
    for pull_number in task.get("pull_requests", []) or []:
        pull_number = _number(pull_number)
        if not pull_number:
            continue
        pull = pulls_by_number.get(pull_number)
        pull_cards.append({
            "number": pull_number,
            "title": pull.get("title", "") if pull else "未读取",
            "ci_state": pull.get("ci", {}).get("state", "unknown") if pull else "unknown",
            "url": _github_url(repository, "pull", pull_number),
        })
    projected = {
        "issue": number,
        "issue_ref": _clean(task.get("issue_ref") or "unknown", 240),
        "title": _clean(task.get("title") or "未命名任务", 240),
        "state": _clean(task.get("state") or "unknown", 40),
        "owner": _clean(task.get("owner") or "未指定", 120),
        "status": _clean(task.get("status") or "未知", 240),
        "next_owner": _clean(task.get("next_owner") or "未指定", 120),
        "approval": _clean(task.get("approval") or "未知", 240),
        "updated_at": _clean(task.get("updated_at") or "unknown", 40),
        "handoff_updated_at": _clean(task.get("handoff_updated_at") or "unknown", 40),
        "owner_verified": bool(task.get("owner_verified")),
        "status_verified": bool(task.get("status_verified")),
        "approval_verified": bool(task.get("approval_verified")),
        "handoff_verified": bool(task.get("handoff_verified")),
        "status_kind": _status_kind(task),
        "pull_requests": pull_cards,
        "url": _github_url(repository, "issue", number),
    }
    client_handoff = _client_handoff_projection(task)
    if client_handoff:
        projected["client_handoff_event"] = client_handoff
    if projected["state"].lower() == "closed":
        # A closed Issue is not proof that the requested work shipped.
        projected["status"] = "已关闭"
    dispatch_projection = _dispatch_projection(dispatch)
    if dispatch_projection:
        target_labels = {
            "server": "服务器端",
            "computer": "电脑端",
            "mobile": "手机端",
            "unknown": "未分配",
        }
        projected["dispatch"] = dispatch_projection
        projected["dispatch_source_status"] = projected["status"]
        projected["dispatch_source_owner"] = projected["owner"]
        projected["owner"] = target_labels[dispatch_projection["target"]]
        projected["next_owner"] = target_labels[dispatch_projection["target"]]
        if projected["state"].lower() == "closed":
            projected["status"] = "已完成" if dispatch_projection["status"] == "DONE" else "已关闭"
        else:
            projected["status"] = dispatch_projection["label"]
            planner = dispatch_projection.get("planner") if isinstance(dispatch_projection.get("planner"), dict) else {}
            if _clean(planner.get("status"), 80).upper() == "PENDING_APPROVAL":
                projected["approval"] = "待维护者确认"
                projected["approval_verified"] = True
                projected["status"] = "待维护者确认"
        projected["status_kind"] = _status_kind(projected)
    return projected


def _empty_groups():
    return [{"id": key, "label": label, "count": 0, "tasks": []} for key, label in GROUPS]


def build_dashboard_payload(
    report,
    sync_state="fresh",
    sync_warning="",
    now=None,
    stale_after=DEFAULT_STALE_AFTER,
    dispatch_state=None,
    dispatch_warning="",
):
    """Project the monitor schema into a small, browser-safe dashboard schema."""
    now = time.time() if now is None else float(now)
    report = report if isinstance(report, dict) else {}
    generated_at = _clean(report.get("generated_at") or "unknown", 40)
    age = None
    generated_seconds = _iso_seconds(generated_at)
    if generated_seconds is not None:
        age = max(0, int(now - generated_seconds))
        if age > max(1, int(stale_after)) and sync_state == "fresh":
            sync_state = "stale"
    warnings = [_clean(item, 500) for item in report.get("warnings", []) if _clean(item, 500)]
    if sync_warning:
        warnings.append(_clean(sync_warning, 500))
    if dispatch_warning:
        warnings.append(_clean(dispatch_warning, 500))
    warnings = sorted(set(warnings))

    dispatch_by_id = {}
    if isinstance(dispatch_state, dict):
        for record in dispatch_state.get("tasks", {}).values() if isinstance(dispatch_state.get("tasks"), dict) else []:
            if isinstance(record, dict) and record.get("task_id"):
                dispatch_by_id[_clean(record.get("task_id"), 160)] = record

    repositories = []
    all_tasks = []
    all_pulls = []
    groups = _empty_groups()
    group_by_id = {item["id"]: item for item in groups}
    for repository_report in report.get("repositories", []) or []:
        if not isinstance(repository_report, dict):
            continue
        repository = _clean(repository_report.get("repository") or "unknown", 240)
        pulls_by_number = {}
        pulls = []
        for pull in repository_report.get("pull_requests", []) or []:
            if not isinstance(pull, dict):
                continue
            projected_pull = _pull_projection(repository, pull)
            pulls.append(projected_pull)
            if projected_pull["number"]:
                pulls_by_number[projected_pull["number"]] = projected_pull
        tasks = []
        for task in repository_report.get("tasks", []) or []:
            if not isinstance(task, dict):
                continue
            task_id = "%s#%s" % (repository, _number(task.get("issue")) or "0")
            projected_task = _task_projection(
                repository, task, pulls_by_number, dispatch_by_id.get(task_id)
            )
            tasks.append(projected_task)
            all_tasks.append(projected_task)
            group = group_by_id.get(projected_task["status_kind"], group_by_id["unknown"])
            group["tasks"].append(projected_task)
            group["count"] += 1
        all_pulls.extend(dict(pull, repository=repository) for pull in pulls)
        repositories.append({
            "repository": repository,
            "coordination_ref": _clean(repository_report.get("coordination_ref") or "unknown", 240),
            "coordination_state": _clean(repository_report.get("coordination_state") or "unknown", 40),
            "main_branch": _clean(repository_report.get("main_branch") or "main", 80),
            "current_main_sha": _clean(repository_report.get("current_main_sha") or "unknown", 80),
            "tasks": tasks,
            "pull_requests": pulls,
            "warnings": sorted(set(
                _clean(item, 500) for item in repository_report.get("warnings", []) if _clean(item, 500)
            )),
        })
    dispatch_summary = {
        "available": isinstance(dispatch_state, dict),
        "source": "local_persistent_dispatch_state" if isinstance(dispatch_state, dict) else "unavailable",
        "runtime_status": _clean(dispatch_state.get("runtime_status") or "QUEUE_ONLY", 40).upper()
        if isinstance(dispatch_state, dict) else "QUEUE_ONLY",
        "paused": bool(dispatch_state.get("paused")) if isinstance(dispatch_state, dict) else False,
        "pause_reason": _clean(dispatch_state.get("pause_reason") or "", 240) if isinstance(dispatch_state, dict) else "",
        "task_count": len(dispatch_by_id),
        "status_counts": {},
        "target_counts": {},
        "last_sync_at": _clean(dispatch_state.get("last_sync_at") or "", 40) if isinstance(dispatch_state, dict) else "",
        "source_generated_at": _clean(dispatch_state.get("source_generated_at") or "", 40) if isinstance(dispatch_state, dict) else "",
    }
    if isinstance(dispatch_state, dict):
        for record in dispatch_by_id.values():
            status = _clean(record.get("status") or "unknown", 40).upper()
            target = _clean(record.get("target") or "unknown", 40).lower()
            dispatch_summary["status_counts"][status] = dispatch_summary["status_counts"].get(status, 0) + 1
            dispatch_summary["target_counts"][target] = dispatch_summary["target_counts"].get(target, 0) + 1
    return {
        "schema_version": 1,
        "source": "read_only_monitor_report",
        "read_only": True,
        "generated_at": generated_at,
        "age_seconds": age,
        "repositories": repositories,
        "tasks": all_tasks,
        "pull_requests": all_pulls,
        "groups": groups,
        "dispatch": dispatch_summary,
        "summary": {
            "repository_count": len(repositories),
            "task_count": len(all_tasks),
            "pull_count": len(all_pulls),
            "warning_count": len(warnings),
        },
        "sync": {
            "state": sync_state,
            "last_successful_sync_at": generated_at if repositories else "unknown",
            "report_generated_at": generated_at,
            "age_seconds": age,
            "stale_after_seconds": int(stale_after),
            "warnings": warnings,
        },
    }


class DashboardStore(object):
    """Read a bounded monitor snapshot and retain the last good projection."""

    def __init__(self, report_path, stale_after=DEFAULT_STALE_AFTER, clock=None, dispatch_path=None):
        self.report_path = Path(report_path).expanduser()
        self.dispatch_path = Path(dispatch_path).expanduser() if dispatch_path else None
        self.stale_after = max(1, int(stale_after))
        self.clock = clock or time.time
        self._last_good = None
        self._last_good_dispatch = None

    def _dispatch_snapshot(self):
        if self.dispatch_path is None or not self.dispatch_path.exists():
            return None, ""
        try:
            if self.dispatch_path.stat().st_size > MAX_DISPATCH_BYTES:
                raise ValueError("dispatch state exceeds size limit")
            value = json.loads(self.dispatch_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("tasks"), dict):
                raise ValueError("dispatch state schema is invalid")
            self._last_good_dispatch = value
            return value, ""
        except (OSError, ValueError, TypeError) as error:
            if self._last_good_dispatch is not None:
                return self._last_good_dispatch, "调度状态读取失败，显示上一次成功快照"
            return None, "调度状态暂不可读：" + _clean(error, 240)

    def payload(self):
        dispatch_state, dispatch_warning = self._dispatch_snapshot()
        try:
            if self.report_path.stat().st_size > MAX_REPORT_BYTES:
                raise ValueError("monitor report exceeds size limit")
            report = json.loads(self.report_path.read_text(encoding="utf-8"))
            if not isinstance(report, dict) or not isinstance(report.get("repositories"), list):
                raise ValueError("monitor report schema is invalid")
            self._last_good = report
            return build_dashboard_payload(
                report,
                sync_state="fresh",
                now=self.clock(),
                stale_after=self.stale_after,
                dispatch_state=dispatch_state,
                dispatch_warning=dispatch_warning,
            )
        except (OSError, ValueError, TypeError) as error:
            if self._last_good is not None:
                return build_dashboard_payload(
                    self._last_good,
                    sync_state="stale",
                    sync_warning="本次读取失败，显示上一次成功快照",
                    now=self.clock(),
                    stale_after=self.stale_after,
                    dispatch_state=dispatch_state,
                    dispatch_warning=dispatch_warning,
                )
            return build_dashboard_payload(
                {"repositories": [], "generated_at": "unknown"},
                sync_state="unavailable",
                sync_warning="监测快照暂不可读：" + _clean(error, 240),
                now=self.clock(),
                stale_after=self.stale_after,
                dispatch_state=dispatch_state,
                dispatch_warning=dispatch_warning,
            )


class StatusHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, store, web_dir, auth_manager=None, approval_store=None, secure_cookies=True):
        self.store = store
        self.web_dir = Path(web_dir).resolve()
        self.auth_manager = auth_manager
        self.approval_store = approval_store
        self.secure_cookies = bool(secure_cookies)
        super(StatusHTTPServer, self).__init__(address, handler)


class StatusRequestHandler(BaseHTTPRequestHandler):
    server_version = "ProjectCollabStatus/1"
    MAX_BODY = 16 * 1024
    SESSION_COOKIE = "collab_session"

    def _headers(self, content_type, length):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")

    def _json(self, value, status=200, headers=None):
        body = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        for key, val in (headers or {}).items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return None
        if length <= 0 or length > self.MAX_BODY:
            return None
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _same_origin(self):
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        try:
            parsed = urlparse(origin)
        except (TypeError, ValueError):
            return False
        expected_scheme = "https" if self.server.secure_cookies else "http"
        return parsed.scheme == expected_scheme and parsed.netloc.lower() == host.lower()

    def _session(self):
        manager = self.server.auth_manager
        if manager is None:
            return None
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            token = jar[self.SESSION_COOKIE].value if self.SESSION_COOKIE in jar else ""
        except (http.cookies.CookieError, KeyError):
            token = ""
        return manager.read_session(token)

    def _authorized(self, maintainer=False, csrf=False):
        if self.server.auth_manager is None:
            return {"username": "local", "role": "maintainer", "csrf_token": ""}
        session = self._session()
        if not session:
            self._json({"error": "authentication_required"}, status=401)
            return None
        if maintainer and session.get("role") != "maintainer":
            self._json({"error": "maintainer_required"}, status=403)
            return None
        if csrf and (not self._same_origin() or not self.server.auth_manager.verify_csrf(
            session, self.headers.get("X-CSRF-Token", "")
        )):
            self._json({"error": "csrf_check_failed"}, status=403)
            return None
        return session

    def _static(self, path):
        names = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}
        name = names.get(path)
        if not name:
            self._json({"error": "not_found"}, status=404)
            return
        candidate = (self.server.web_dir / name).resolve()
        if self.server.web_dir not in candidate.parents or not candidate.is_file():
            self._json({"error": "asset_unavailable"}, status=404)
            return
        try:
            body = candidate.read_bytes()
        except OSError:
            self._json({"error": "asset_unavailable"}, status=404)
            return
        if len(body) > MAX_STATIC_BYTES:
            self._json({"error": "asset_too_large"}, status=500)
            return
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        self.send_response(200)
        self._headers(content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""), len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - stdlib handler interface
        path = urlparse(self.path).path
        if path == "/api/session":
            if self.server.auth_manager is None:
                self._json({"authenticated": True, "auth_enabled": False, "username": "local", "role": "maintainer"})
                return
            session = self._session()
            self._json({"authenticated": bool(session), "auth_enabled": True,
                        "username": session.get("username", "") if session else "",
                        "role": session.get("role", "") if session else "",
                        "csrf_token": session.get("csrf_token", "") if session else ""})
            return
        if path == "/api/approvals":
            session = self._authorized(maintainer=True)
            if session is None:
                return
            try:
                approvals = self.server.approval_store.pending() if self.server.approval_store else []
            except (OSError, ValueError):
                self._json({"error": "approvals_unavailable"}, status=503)
                return
            self._json({"approvals": approvals})
            return
        if path in ("/api/status", "/api/refresh"):
            if self._authorized() is None:
                return
            self._json(self.server.store.payload())
            return
        if path == "/healthz":
            payload = self.server.store.payload()
            self._json({"ok": payload["sync"]["state"] != "unavailable", "source": payload["source"]})
            return
        self._static(path)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/login":
            manager = self.server.auth_manager
            if manager is None:
                self._json({"error": "authentication_disabled"}, status=404)
                return
            if not self._same_origin():
                self._json({"error": "origin_check_failed"}, status=403)
                return
            value = self._body()
            if value is None:
                self._json({"error": "invalid_request"}, status=400)
                return
            try:
                source = status_auth.client_source(
                    self.client_address[0], self.headers.get("X-Real-IP", "")
                )
                user = manager.authenticate(value.get("username"), value.get("password"), source)
            except status_auth.AuthError:
                self._json({"error": "login_rate_limited"}, status=429)
                return
            if not user:
                self._json({"error": "invalid_credentials"}, status=401)
                return
            session = manager.create_session(user["username"])
            cookie = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (
                self.SESSION_COOKIE, session["token"], status_auth.SESSION_TTL_SECONDS
            )
            if self.server.secure_cookies:
                cookie += "; Secure"
            self._json({"authenticated": True, "username": session["username"], "role": session["role"],
                        "csrf_token": session["csrf_token"]}, headers={"Set-Cookie": cookie})
            return
        if path == "/api/logout":
            session = self._authorized(csrf=True)
            if session is None:
                return
            self._json({"authenticated": False}, headers={
                "Set-Cookie": "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict%s" % (
                    self.SESSION_COOKIE, "; Secure" if self.server.secure_cookies else "")
            })
            return
        match = re.fullmatch(r"/api/approvals/([A-Za-z0-9_-]{16,64})/decision", path)
        if match:
            session = self._authorized(maintainer=True, csrf=True)
            if session is None:
                return
            value = self._body()
            if value is None or value.get("decision") not in ("approve", "reject"):
                self._json({"error": "invalid_request"}, status=400)
                return
            if self.server.approval_store is None:
                self._json({"error": "approvals_unavailable"}, status=503)
                return
            try:
                result = self.server.approval_store.decide(match.group(1), value["decision"], session["username"])
            except task_approvals.ApprovalError:
                self._json({"error": "approval_unavailable"}, status=409)
                return
            self._json({"approval": result})
            return
        if path in ("/api/status", "/api/refresh"):
            self._json({"error": "method_not_allowed"}, status=405)
            return
        self._json({"error": "not_found"}, status=404)

    def log_message(self, format_string, *args):
        # Keep access logs bounded and free of headers/query strings.
        message = _clean(format_string % args, 300)
        sys.stderr.write("status_web: " + message + "\n")


def serve(
    report_path,
    web_dir,
    host=DEFAULT_HOST,
    port=DEFAULT_PORT,
    stale_after=DEFAULT_STALE_AFTER,
    dispatch_path=DEFAULT_DISPATCH_FILE,
    auth_file="",
    approval_file="",
    secure_cookies=True,
):
    store = DashboardStore(report_path, stale_after=stale_after, dispatch_path=dispatch_path)
    auth_manager = None
    if auth_file:
        config_path = Path(auth_file).expanduser()
        if os.name != "nt" and config_path.stat().st_mode & 0o077:
            raise status_auth.AuthError("auth config must not be accessible by group or others")
        auth_manager = status_auth.AuthManager(json.loads(config_path.read_text(encoding="utf-8")))
    approval_store = task_approvals.TaskApprovalStore(approval_file) if approval_file else None
    server = StatusHTTPServer((host, int(port)), StatusRequestHandler, store, web_dir,
                              auth_manager, approval_store, secure_cookies)
    print("status_web: listening http://%s:%d" % (host, int(port)))
    print("status_web: source read-only monitor report %s" % report_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-file", default=DEFAULT_REPORT_FILE)
    parser.add_argument(
        "--web-dir",
        default=str(Path(__file__).resolve().parents[1] / "web" / "status"),
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="default: 127.0.0.1; do not expose publicly")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--stale-after", type=int, default=DEFAULT_STALE_AFTER)
    parser.add_argument("--dispatch-state-file", default=DEFAULT_DISPATCH_FILE)
    parser.add_argument("--auth-file", default="")
    parser.add_argument("--approval-file", default="")
    parser.add_argument("--insecure-cookies", action="store_true", help="for local HTTP tests only")
    args = parser.parse_args(argv)
    return serve(
        args.report_file,
        args.web_dir,
        args.host,
        args.port,
        args.stale_after,
        args.dispatch_state_file,
        args.auth_file,
        args.approval_file,
        not args.insecure_cookies,
    )


if __name__ == "__main__":
    sys.exit(main())
