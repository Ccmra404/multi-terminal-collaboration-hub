#!/usr/bin/env python3
"""Safe local task queue and allowlisted server runner.

The read-only GitHub monitor remains the source of task facts.  This module
turns that snapshot into a durable, local queue without writing to GitHub.  A
task is claimable only when its target is explicit (a verified handoff field or
an exact local override).  The server runner has one built-in action,
``server_health_check``; issue text is data and is never interpreted as a
command.
"""

from __future__ import print_function

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux is the supported server
    fcntl = None

try:
    import authorization as _authorization
except ImportError:  # pragma: no cover - run-from-repository-root convenience
    _tools = str(Path(__file__).resolve().parent)
    if _tools not in sys.path:
        sys.path.insert(0, _tools)
    import authorization as _authorization


SCHEMA_VERSION = 1
ALLOWED_REPOSITORIES = frozenset((
    "example/coordination-hub",
    "example/managed-project",
))
TARGETS = frozenset(("server", "computer", "mobile"))
ACTORS = TARGETS
STATUSES = frozenset(("PENDING", "CLAIMED", "RUNNING", "REVIEW", "DONE", "FAILED", "BLOCKED"))
ACTIVE_STATUSES = frozenset(("CLAIMED", "RUNNING"))
RESULT_STATUSES = frozenset(("REVIEW", "DONE", "FAILED", "BLOCKED"))
RUNTIME_STATUSES = frozenset(("RUNNER_WORKING", "QUEUE_ONLY", "BLOCKED_EXECUTOR"))
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_TASKS = 1000
MAX_AUDIT = 4000
MAX_RESULT_CHARS = 8000
DEFAULT_STATE_FILE = "/var/lib/collaboration-hub/dispatch/state.json"
DEFAULT_REPORT_FILE = "/var/lib/collaboration-hub/once/report.json"
DEFAULT_TARGET_CONFIG = "/etc/collaboration-hub/task-dispatch.json"
SERVER_TEST_ACTION = "server_health_check"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")


class DispatchError(ValueError):
    """Expected queue, state, or executor failure."""


def _clean(value, maximum=240):
    return _authorization._safe_text(value, maximum)


def _iso(value=None):
    try:
        return _authorization._iso(value)
    except Exception as error:
        raise DispatchError("invalid timestamp: " + _clean(error, 160))


def _epoch(value):
    try:
        return _authorization._utc(value).timestamp()
    except Exception:
        return None


def _now_epoch(value=None):
    if value is None:
        return time.time()
    result = _epoch(value)
    if result is None:
        raise DispatchError("invalid timestamp")
    return result


def _sanitize(value, depth=0):
    """Bound values before they are persisted or returned to a browser."""
    if depth > 6:
        return "<truncated>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clean(value, MAX_RESULT_CHARS)
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth + 1) for item in list(value)[:100]]
    if isinstance(value, dict):
        result = {}
        for key in list(value.keys())[:100]:
            normalized = _clean(key, 100)
            if normalized:
                result[normalized] = _sanitize(value[key], depth + 1)
        return result
    return _clean(value, 240)


def _require_task_id(value):
    value = _clean(value, 160)
    if not TASK_ID_RE.fullmatch(value):
        raise DispatchError("task_id must be an explicit owner/name#issue")
    repository, number = value.rsplit("#", 1)
    if repository not in ALLOWED_REPOSITORIES:
        raise DispatchError("repository is outside the authorized dispatch scope")
    return repository + "#" + str(int(number))


def stable_task_id(repository, issue):
    repository = _clean(repository, 240)
    if repository not in ALLOWED_REPOSITORIES:
        raise DispatchError("repository is outside the authorized dispatch scope")
    try:
        issue = int(issue)
    except (TypeError, ValueError):
        raise DispatchError("issue number must be an integer")
    if issue <= 0:
        raise DispatchError("issue number must be positive")
    return "%s#%d" % (repository, issue)


def _normalize_target(value):
    text = _clean(value, 80).strip().lower()
    aliases = {
        "server": "server",
        "服务器": "server",
        "服务器端": "server",
        "computer": "computer",
        "电脑": "computer",
        "电脑端": "computer",
        "mobile": "mobile",
        "手机": "mobile",
        "手机端": "mobile",
        "移动": "mobile",
        "移动端": "mobile",
    }
    return aliases.get(text, "")


def _load_json(path, maximum=MAX_STATE_BYTES):
    path = Path(path).expanduser()
    try:
        if path.stat().st_size > maximum:
            raise DispatchError("JSON file exceeds size limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except DispatchError:
        raise
    except (OSError, ValueError) as error:
        raise DispatchError("cannot read JSON file: " + _clean(error, 240))
    return value


def load_target_overrides(path=None):
    """Load exact task-id target assignments; absent config means no guesses."""
    if not path:
        return {}
    path = Path(path).expanduser()
    if not path.exists():
        return {}
    value = _load_json(path, maximum=256 * 1024)
    if not isinstance(value, dict) or int(value.get("schema_version", 0)) != 1:
        raise DispatchError("target config has an unsupported schema_version")
    raw = value.get("target_overrides", {})
    if not isinstance(raw, dict) or len(raw) > MAX_TASKS:
        raise DispatchError("target_overrides must be a bounded object")
    result = {}
    for task_id, target in raw.items():
        normalized_id = _require_task_id(task_id)
        normalized_target = _normalize_target(target)
        if normalized_target not in TARGETS:
            raise DispatchError("target override must be server, computer, or mobile")
        result[normalized_id] = normalized_target
    return result


def _target_from_task(task, task_id, overrides):
    if task_id in overrides:
        return overrides[task_id], "target_override", True

    candidates = []
    direct = _normalize_target(task.get("target"))
    if direct and task.get("target_verified") is True:
        candidates.append((direct, "verified_target"))
    for field in ("owner", "next_owner"):
        if task.get(field + "_verified") is not True:
            continue
        target = _normalize_target(task.get(field))
        if target:
            candidates.append((target, "verified_" + field))
    if not candidates:
        return "unknown", "unassigned", False
    unique = set(item[0] for item in candidates)
    if len(unique) != 1:
        return "unknown", "conflicting_verified_targets", False
    return candidates[0][0], candidates[0][1], True


def _default_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "paused": False,
        "pause_reason": "",
        "runtime_status": "QUEUE_ONLY",
        "tasks": {},
        "audit": [],
        "last_sync_at": "",
        "source_generated_at": "",
    }


def _validate_state(value):
    if not isinstance(value, dict) or int(value.get("schema_version", 0)) != SCHEMA_VERSION:
        raise DispatchError("dispatch state has an unsupported schema_version")
    tasks = value.get("tasks")
    audit = value.get("audit")
    if not isinstance(tasks, dict) or len(tasks) > MAX_TASKS:
        raise DispatchError("dispatch tasks are invalid or too large")
    if not isinstance(audit, list) or len(audit) > MAX_AUDIT:
        raise DispatchError("dispatch audit is invalid or too large")
    runtime_status = _clean(value.get("runtime_status") or "QUEUE_ONLY", 40).upper()
    if runtime_status not in RUNTIME_STATUSES:
        raise DispatchError("dispatch runtime_status is invalid")
    for task_id, record in tasks.items():
        normalized = _require_task_id(task_id)
        if not isinstance(record, dict) or record.get("task_id") != normalized:
            raise DispatchError("dispatch task identity is invalid")
        if record.get("target") not in TARGETS and record.get("target") != "unknown":
            raise DispatchError("dispatch task target is invalid")
        if record.get("status") not in STATUSES:
            raise DispatchError("dispatch task status is invalid")
    return {
        "schema_version": SCHEMA_VERSION,
        "paused": bool(value.get("paused")),
        "pause_reason": _clean(value.get("pause_reason"), 240),
        "runtime_status": runtime_status,
        "tasks": tasks,
        "audit": audit,
        "last_sync_at": _clean(value.get("last_sync_at"), 40),
        "source_generated_at": _clean(value.get("source_generated_at"), 40),
    }


class DispatchStore(object):
    """Atomic 0600 local queue state shared by sync, runner, and web read."""

    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.lock_path = Path(str(self.path) + ".lock")

    def _ensure_parent(self):
        parent = self.path.parent
        if not parent.exists():
            parent.mkdir(parents=True, mode=0o700)
        try:
            os.chmod(str(parent), 0o700)
        except OSError:
            pass

    def _read_unlocked(self):
        if not self.path.exists():
            return _default_state()
        try:
            if self.path.stat().st_size > MAX_STATE_BYTES:
                raise DispatchError("dispatch state exceeds size limit")
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except DispatchError:
            raise
        except (OSError, ValueError) as error:
            raise DispatchError("cannot read dispatch state: " + _clean(error, 240))
        return _validate_state(value)

    def _write_unlocked(self, value):
        self._ensure_parent()
        fd, temporary = tempfile.mkstemp(prefix=".dispatch-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, str(self.path))
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    @contextmanager
    def _locked(self):
        self._ensure_parent()
        handle = open(str(self.lock_path), "a+")
        try:
            try:
                os.chmod(str(self.lock_path), 0o600)
            except OSError:
                pass
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            state = self._read_unlocked()
            yield state
            self._write_unlocked(state)
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _audit(self, state, event, task_id="", actor="", **details):
        record = {
            "event": _clean(event, 80),
            "task_id": _clean(task_id, 160),
            "actor": _clean(actor, 120),
            "recorded_at": _iso(),
        }
        record.update({key: _sanitize(value) for key, value in details.items()})
        state["audit"].append(record)
        if len(state["audit"]) > MAX_AUDIT:
            del state["audit"][:-MAX_AUDIT]

    @staticmethod
    def _record_copy(record):
        return copy.deepcopy(record)

    @staticmethod
    def _task_record(task_id, task, target, target_source, target_verified, now):
        return {
            "task_id": task_id,
            "repository": task_id.rsplit("#", 1)[0],
            "issue": int(task_id.rsplit("#", 1)[1]),
            "title": _clean(task.get("title") or "未命名任务", 240),
            "source_state": _clean(task.get("state") or "unknown", 40),
            "source_status": _clean(task.get("status") or "unknown", 500),
            "source_updated_at": _clean(task.get("updated_at") or "unknown", 40),
            "target": target,
            "target_source": target_source,
            "target_verified": bool(target_verified),
            "status": "PENDING",
            "claimant": "",
            "claimed_at": "",
            "heartbeat_at": "",
            "attempt": 0,
            "result": {},
            "commit": "",
            "pr": "",
            "log_summary": "",
            "created_at": now,
            "updated_at": now,
            "source_seen": True,
        }

    def sync(self, report, target_overrides=None, now=None):
        if not isinstance(report, dict) or not isinstance(report.get("repositories"), list):
            raise DispatchError("monitor report schema is invalid")
        target_overrides = dict(target_overrides or {})
        timestamp = _iso(now)
        rows = []
        warnings = []
        seen = set()
        for repository_report in report.get("repositories", []):
            if not isinstance(repository_report, dict):
                continue
            repository = _clean(repository_report.get("repository"), 240)
            if repository not in ALLOWED_REPOSITORIES:
                warnings.append("ignored repository outside dispatch scope: " + repository)
                continue
            for task in repository_report.get("tasks", []) or []:
                if not isinstance(task, dict):
                    continue
                try:
                    task_id = stable_task_id(repository, task.get("issue"))
                except DispatchError as error:
                    warnings.append("ignored task: " + _clean(error, 180))
                    continue
                if task_id in seen:
                    warnings.append("duplicate task ignored: " + task_id)
                    continue
                seen.add(task_id)
                target, source, verified = _target_from_task(task, task_id, target_overrides)
                rows.append((task_id, task, target, source, verified))

        with self._locked() as state:
            for record in state["tasks"].values():
                if isinstance(record, dict):
                    record["source_seen"] = False
            for task_id, task, target, source, verified in rows:
                existing = state["tasks"].get(task_id)
                if not existing:
                    existing = self._task_record(task_id, task, target, source, verified, timestamp)
                    state["tasks"][task_id] = existing
                else:
                    existing["title"] = _clean(task.get("title") or existing.get("title"), 240)
                    existing["source_state"] = _clean(task.get("state") or "unknown", 40)
                    existing["source_status"] = _clean(task.get("status") or "unknown", 500)
                    existing["source_updated_at"] = _clean(task.get("updated_at") or "unknown", 40)
                    # Do not silently reassign an active or completed task.  A
                    # newly explicit target may fill an unassigned PENDING row.
                    if existing.get("status") == "PENDING" and (
                        existing.get("target") == "unknown" or source == "target_override"
                    ):
                        existing["target"] = target
                        existing["target_source"] = source
                        existing["target_verified"] = bool(verified)
                    existing["source_seen"] = True
                    existing["updated_at"] = timestamp
            state["last_sync_at"] = timestamp
            state["source_generated_at"] = _clean(report.get("generated_at") or "unknown", 40)
            self._audit(
                state,
                "sync",
                actor="monitor",
                task_count=len(rows),
                warning_count=len(warnings),
                generated_at=state["source_generated_at"],
            )
            summary = self._summary_unlocked(state)
            summary["warnings"] = sorted(set(warnings))
            return summary

    def _get_unlocked(self, state, task_id):
        task_id = _require_task_id(task_id)
        record = state["tasks"].get(task_id)
        if not record:
            raise DispatchError("task is not in the local queue: " + task_id)
        return record

    def get(self, task_id):
        with self._locked() as state:
            return self._record_copy(self._get_unlocked(state, task_id))

    def list_tasks(self, target=None, status=None):
        target = _normalize_target(target) if target else ""
        if target and target not in TARGETS:
            raise DispatchError("target must be server, computer, or mobile")
        status = _clean(status, 40).upper() if status else ""
        if status and status not in STATUSES:
            raise DispatchError("status is invalid")
        with self._locked() as state:
            rows = []
            for record in state["tasks"].values():
                if target and record.get("target") != target:
                    continue
                if status and record.get("status") != status:
                    continue
                rows.append(self._record_copy(record))
            return sorted(rows, key=lambda item: item.get("task_id", ""))

    def _assert_actor_target(self, record, actor):
        actor = _normalize_target(actor)
        if actor not in ACTORS:
            raise DispatchError("actor must be server, computer, or mobile")
        if record.get("target") != actor:
            raise DispatchError("task target does not match actor")
        return actor

    @staticmethod
    def _active_lease(record, now, lease_seconds):
        heartbeat = _epoch(record.get("heartbeat_at")) or _epoch(record.get("claimed_at"))
        return heartbeat is not None and now - heartbeat < lease_seconds

    def claim(self, task_id, actor, now=None, lease_seconds=900, allow_retry=False):
        task_id = _require_task_id(task_id)
        now_iso = _iso(now)
        now_epoch = _now_epoch(now_iso)
        try:
            lease_seconds = max(30, min(int(lease_seconds), 86400))
        except (TypeError, ValueError):
            raise DispatchError("lease_seconds must be an integer")
        with self._locked() as state:
            if state.get("paused"):
                raise DispatchError("dispatcher is paused")
            record = self._get_unlocked(state, task_id)
            actor = self._assert_actor_target(record, actor)
            current = record.get("status")
            if current in ACTIVE_STATUSES:
                if record.get("claimant") == actor and self._active_lease(record, now_epoch, lease_seconds):
                    return self._record_copy(record)
                raise DispatchError("task is already claimed; recover stale work before retrying")
            if current == "PENDING":
                pass
            elif current == "FAILED" and allow_retry:
                record["result"] = {}
                record["commit"] = ""
                record["pr"] = ""
                record["log_summary"] = ""
            else:
                raise DispatchError("task status cannot be claimed: " + str(current))
            record["status"] = "CLAIMED"
            record["claimant"] = actor
            record["claimed_at"] = now_iso
            record["heartbeat_at"] = now_iso
            record["attempt"] = int(record.get("attempt") or 0) + 1
            record["updated_at"] = now_iso
            self._audit(state, "claimed", task_id, actor, attempt=record["attempt"])
            return self._record_copy(record)

    def start(self, task_id, actor, now=None):
        task_id = _require_task_id(task_id)
        now_iso = _iso(now)
        with self._locked() as state:
            record = self._get_unlocked(state, task_id)
            actor = self._assert_actor_target(record, actor)
            if record.get("claimant") != actor:
                raise DispatchError("only the claimant may start the task")
            if record.get("status") == "RUNNING":
                return self._record_copy(record)
            if record.get("status") != "CLAIMED":
                raise DispatchError("task must be CLAIMED before RUNNING")
            record["status"] = "RUNNING"
            record["heartbeat_at"] = now_iso
            record["updated_at"] = now_iso
            self._audit(state, "started", task_id, actor)
            return self._record_copy(record)

    def heartbeat(self, task_id, actor, now=None):
        task_id = _require_task_id(task_id)
        now_iso = _iso(now)
        with self._locked() as state:
            record = self._get_unlocked(state, task_id)
            actor = self._assert_actor_target(record, actor)
            if record.get("claimant") != actor or record.get("status") not in ACTIVE_STATUSES:
                raise DispatchError("task is not actively claimed by actor")
            record["heartbeat_at"] = now_iso
            record["updated_at"] = now_iso
            return self._record_copy(record)

    def submit(self, task_id, actor, status, result=None, commit="", pr="", log_summary="", now=None):
        task_id = _require_task_id(task_id)
        status = _clean(status, 40).upper()
        if status not in RESULT_STATUSES:
            raise DispatchError("result status must be REVIEW, DONE, FAILED, or BLOCKED")
        now_iso = _iso(now)
        with self._locked() as state:
            record = self._get_unlocked(state, task_id)
            actor = self._assert_actor_target(record, actor)
            if record.get("claimant") != actor or record.get("status") not in ACTIVE_STATUSES:
                raise DispatchError("only the active claimant may submit a result")
            record["status"] = status
            record["result"] = _sanitize(result or {})
            record["commit"] = _clean(commit, 120)
            record["pr"] = _clean(pr, 120)
            record["log_summary"] = _clean(log_summary, 1000)
            record["heartbeat_at"] = now_iso
            record["updated_at"] = now_iso
            self._audit(
                state,
                "submitted",
                task_id,
                actor,
                status=status,
                commit=record["commit"],
                pr=record["pr"],
            )
            return self._record_copy(record)

    def pause(self, reason="", now=None):
        now_iso = _iso(now)
        with self._locked() as state:
            state["paused"] = True
            state["pause_reason"] = _clean(reason or "manual stop switch", 240)
            self._audit(state, "paused", actor="operator", reason=state["pause_reason"])
            state["last_sync_at"] = state.get("last_sync_at") or now_iso
            return {"paused": True, "reason": state["pause_reason"]}

    def resume(self, now=None):
        now_iso = _iso(now)
        with self._locked() as state:
            state["paused"] = False
            state["pause_reason"] = ""
            self._audit(state, "resumed", actor="operator")
            state["last_sync_at"] = state.get("last_sync_at") or now_iso
            return {"paused": False}

    def recover(self, stale_after=900, now=None):
        try:
            stale_after = max(30, min(int(stale_after), 86400))
        except (TypeError, ValueError):
            raise DispatchError("stale_after must be an integer")
        now_iso = _iso(now)
        now_epoch = _now_epoch(now_iso)
        recovered = []
        with self._locked() as state:
            for task_id, record in state["tasks"].items():
                if record.get("status") not in ACTIVE_STATUSES:
                    continue
                heartbeat = _epoch(record.get("heartbeat_at")) or _epoch(record.get("claimed_at"))
                if heartbeat is None or now_epoch - heartbeat < stale_after:
                    continue
                record["status"] = "FAILED"
                record["result"] = {"reason": "stale_claim_recovered", "recovered_at": now_iso}
                record["log_summary"] = "stale active claim recovered; explicit retry required"
                record["updated_at"] = now_iso
                recovered.append(task_id)
                self._audit(state, "stale_recovered", task_id, record.get("claimant", ""))
            return {"recovered": recovered, "count": len(recovered)}

    def is_paused(self):
        with self._locked() as state:
            return bool(state.get("paused"))

    def set_runtime_status(self, status):
        status = _clean(status, 40).upper()
        if status not in RUNTIME_STATUSES:
            raise DispatchError("runtime_status is invalid")
        with self._locked() as state:
            state["runtime_status"] = status
            return status

    def set_planner_metadata(self, task_id, metadata):
        """Record bounded planner evidence without changing task ownership."""
        task_id = _require_task_id(task_id)
        if not isinstance(metadata, dict):
            raise DispatchError("planner metadata must be an object")
        now_iso = _iso()
        with self._locked() as state:
            record = self._get_unlocked(state, task_id)
            record["planner"] = _sanitize(metadata)
            record["updated_at"] = now_iso
            self._audit(state, "planner_update", task_id, "server", planner=record["planner"])
            return self._record_copy(record.get("planner", {}))

    @staticmethod
    def _summary_unlocked(state):
        counts = {status: 0 for status in sorted(STATUSES)}
        targets = {target: 0 for target in sorted(TARGETS)}
        unknown = 0
        for record in state["tasks"].values():
            status = record.get("status")
            if status in counts:
                counts[status] += 1
            target = record.get("target")
            if target in targets:
                targets[target] += 1
            else:
                unknown += 1
        return {
            "schema_version": SCHEMA_VERSION,
            "paused": bool(state.get("paused")),
            "pause_reason": _clean(state.get("pause_reason"), 240),
            "runtime_status": _clean(state.get("runtime_status") or "QUEUE_ONLY", 40),
            "task_count": len(state["tasks"]),
            "status_counts": counts,
            "target_counts": targets,
            "unassigned_count": unknown,
            "last_sync_at": _clean(state.get("last_sync_at"), 40),
            "source_generated_at": _clean(state.get("source_generated_at"), 40),
        }

    def summary(self):
        with self._locked() as state:
            return self._summary_unlocked(state)


def _read_report(path):
    value = _load_json(path, maximum=MAX_STATE_BYTES)
    if not isinstance(value, dict) or not isinstance(value.get("repositories"), list):
        raise DispatchError("monitor report schema is invalid")
    return value


def _terminate_process(process):
    try:
        process.terminate()
        process.wait(timeout=2)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=2)
        except Exception:
            pass


def _child_health_check(report_file, task_id):
    """The only command the server runner can execute."""
    try:
        import resource

        # The child only reads a small JSON snapshot.  Keep the controlled
        # action bounded even if a future implementation bug loops or writes.
        resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    except (ImportError, AttributeError, OSError, ValueError):
        # The fixed action remains safe on platforms without resource limits;
        # the parent timeout and no-shell environment still apply.
        pass
    report = _read_report(report_file)
    found = None
    for repository_report in report.get("repositories", []):
        if not isinstance(repository_report, dict):
            continue
        repository = _clean(repository_report.get("repository"), 240)
        for task in repository_report.get("tasks", []) or []:
            if not isinstance(task, dict):
                continue
            try:
                candidate = stable_task_id(repository, task.get("issue"))
            except DispatchError:
                continue
            if candidate == task_id:
                found = task
                break
        if found is not None:
            break
    if found is None:
        raise DispatchError("task is absent from the monitor snapshot")
    result = {
        "executor": SERVER_TEST_ACTION + "-v1",
        "task_id": task_id,
        "report_generated_at": _clean(report.get("generated_at") or "unknown", 40),
        "repository_count": len(report.get("repositories", [])),
        "task_count": sum(
            len(item.get("tasks", []) or [])
            for item in report.get("repositories", [])
            if isinstance(item, dict)
        ),
        "result": "healthy",
    }
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


def run_server_test(store, task_id, report_file, timeout=30):
    """Run the fixed health-check child and persist its real result."""
    task_id = _require_task_id(task_id)
    record = store.get(task_id)
    if record.get("target") != "server":
        raise DispatchError("server runner accepts only explicitly server-targeted tasks")
    if record.get("status") in ACTIVE_STATUSES:
        raise DispatchError("active task will not be run again; recover it first if stale")
    try:
        timeout = max(1, min(int(timeout), 120))
    except (TypeError, ValueError):
        raise DispatchError("timeout must be an integer")
    claimed = store.claim(task_id, "server", allow_retry=record.get("status") == "FAILED")
    store.start(task_id, "server")
    store.set_runtime_status("RUNNER_WORKING")
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child-health-check",
        "--report-file",
        str(Path(report_file).expanduser()),
        "--task-id",
        task_id,
    ]
    environment = {
        "PATH": os.defpath,
        "LC_ALL": "C",
        "PYTHONIOENCODING": "utf-8",
    }
    process = None
    stdout = ""
    stderr = ""
    interrupted = ""
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            close_fds=True,
            env=environment,
            cwd="/",
        )
        while process.poll() is None:
            if time.monotonic() - started >= timeout:
                interrupted = "timeout"
                _terminate_process(process)
                break
            if store.is_paused():
                interrupted = "paused"
                _terminate_process(process)
                break
            time.sleep(0.05)
        stdout, stderr = process.communicate(timeout=2)
    except Exception as error:
        interrupted = interrupted or "spawn_error"
        stderr = _clean(error, 500)
        if process is not None and process.poll() is None:
            _terminate_process(process)
    code = process.returncode if process is not None else 127
    log = _clean((stdout or "") + (" " + stderr if stderr else ""), 1000)
    parsed = {}
    if stdout:
        try:
            value = json.loads(stdout.strip().splitlines()[-1])
            if isinstance(value, dict):
                parsed = _sanitize(value)
        except (ValueError, IndexError):
            pass
    if interrupted:
        parsed.update({"reason": interrupted})
    runtime_status = "BLOCKED_EXECUTOR" if interrupted == "spawn_error" else "QUEUE_ONLY"
    try:
        if code == 0 and not interrupted:
            return store.submit(
                task_id,
                "server",
                "DONE",
                result=parsed,
                log_summary=log or "server health check completed",
            )
        return store.submit(
            task_id,
            "server",
            "FAILED",
            result=dict(parsed, exit_code=code),
            log_summary=log or ("server health check failed: " + (interrupted or "nonzero exit")),
        )
    finally:
        store.set_runtime_status(runtime_status)


def run_next_server_test(store, report_file, timeout=30):
    """Select one explicit server task and run only the fixed health check.

    Selection is deterministic and limited to PENDING server rows.  A failed
    row is never retried by this helper; an operator must pass ``--retry`` to
    ``claim`` after reviewing the recorded failure.
    """
    candidates = store.list_tasks(target="server", status="PENDING")
    if not candidates:
        return {
            "runtime_status": "QUEUE_ONLY",
            "selected_task_id": "",
            "action": SERVER_TEST_ACTION,
            "reason": "no pending server task",
        }
    selected = candidates[0]
    result = run_server_test(store, selected["task_id"], report_file, timeout)
    return {
        "runtime_status": store.summary()["runtime_status"],
        "selected_task_id": selected["task_id"],
        "action": SERVER_TEST_ACTION,
        "execution": "completed",
        "task": result,
    }


def _json_print(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    sub = parser.add_subparsers(dest="command")

    sync = sub.add_parser("sync", help="ingest the read-only monitor snapshot")
    sync.add_argument("--report-file", default=DEFAULT_REPORT_FILE)
    sync.add_argument("--target-config", default=DEFAULT_TARGET_CONFIG)

    listing = sub.add_parser("list", help="list durable queue records")
    listing.add_argument("--target", choices=sorted(TARGETS))
    listing.add_argument("--status", choices=sorted(STATUSES))

    claim = sub.add_parser("claim", help="claim a pending task for one endpoint")
    claim.add_argument("--task-id", required=True)
    claim.add_argument("--actor", choices=sorted(ACTORS), required=True)
    claim.add_argument("--retry", action="store_true")
    claim.add_argument("--lease-seconds", type=int, default=900)

    start = sub.add_parser("start", help="move a claimed task to RUNNING")
    start.add_argument("--task-id", required=True)
    start.add_argument("--actor", choices=sorted(ACTORS), required=True)

    heartbeat = sub.add_parser("heartbeat", help="refresh a claim lease")
    heartbeat.add_argument("--task-id", required=True)
    heartbeat.add_argument("--actor", choices=sorted(ACTORS), required=True)

    submit = sub.add_parser("submit", help="record a manual result/handoff")
    submit.add_argument("--task-id", required=True)
    submit.add_argument("--actor", choices=sorted(ACTORS), required=True)
    submit.add_argument("--status", choices=sorted(RESULT_STATUSES), required=True)
    submit.add_argument("--result-json", default="{}")
    submit.add_argument("--commit", default="")
    submit.add_argument("--pr", default="")
    submit.add_argument("--log-summary", default="")

    runner = sub.add_parser("run-server-test", help="run the fixed server health check")
    runner.add_argument("--task-id", required=True)
    runner.add_argument("--report-file", default=DEFAULT_REPORT_FILE)
    runner.add_argument("--timeout", type=int, default=30)
    next_runner = sub.add_parser("run-server-next", help="auto-claim the next pending server task")
    next_runner.add_argument("--report-file", default=DEFAULT_REPORT_FILE)
    next_runner.add_argument("--timeout", type=int, default=30)

    recover = sub.add_parser("recover", help="mark stale active claims FAILED")
    recover.add_argument("--stale-after", type=int, default=900)
    pause = sub.add_parser("pause", help="activate the persistent stop switch")
    pause.add_argument("--reason", default="manual stop switch")
    sub.add_parser("resume", help="clear the persistent stop switch")
    sub.add_parser("status", help="show queue/stop-switch summary")
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.error("a command is required")
    try:
        store = DispatchStore(args.state_file)
        if args.command == "sync":
            report = _read_report(args.report_file)
            overrides = load_target_overrides(args.target_config)
            _json_print(store.sync(report, overrides))
        elif args.command == "list":
            _json_print({"tasks": store.list_tasks(args.target, args.status)})
        elif args.command == "claim":
            _json_print(store.claim(args.task_id, args.actor, lease_seconds=args.lease_seconds, allow_retry=args.retry))
        elif args.command == "start":
            _json_print(store.start(args.task_id, args.actor))
        elif args.command == "heartbeat":
            _json_print(store.heartbeat(args.task_id, args.actor))
        elif args.command == "submit":
            result = json.loads(args.result_json)
            if not isinstance(result, dict):
                raise DispatchError("result-json must be an object")
            _json_print(store.submit(args.task_id, args.actor, args.status, result, args.commit, args.pr, args.log_summary))
        elif args.command == "run-server-test":
            _json_print(run_server_test(store, args.task_id, args.report_file, args.timeout))
        elif args.command == "run-server-next":
            _json_print(run_next_server_test(store, args.report_file, args.timeout))
        elif args.command == "recover":
            _json_print(store.recover(args.stale_after))
        elif args.command == "pause":
            _json_print(store.pause(args.reason))
        elif args.command == "resume":
            _json_print(store.resume())
        elif args.command == "status":
            _json_print(store.summary())
        return 0
    except (DispatchError, OSError, ValueError) as error:
        sys.stderr.write("task_dispatch: " + _clean(error, 500) + "\n")
        return 2


if __name__ == "__main__":
    if "--child-health-check" in sys.argv:
        child_parser = argparse.ArgumentParser()
        child_parser.add_argument("--child-health-check", action="store_true")
        child_parser.add_argument("--report-file", required=True)
        child_parser.add_argument("--task-id", required=True)
        child_args = child_parser.parse_args()
        try:
            sys.exit(_child_health_check(child_args.report_file, _require_task_id(child_args.task_id)))
        except (DispatchError, OSError, ValueError) as error:
            sys.stderr.write("task_dispatch child: " + _clean(error, 500) + "\n")
            sys.exit(2)
    sys.exit(main())
