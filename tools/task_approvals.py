"""Persistent, one-time approvals for an exact bounded server task plan."""

from __future__ import print_function

import copy
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows test runner
    fcntl = None

MAX_FILE_BYTES = 512 * 1024
MAX_ENTRIES = 500
_LOCK = threading.RLock()


class ApprovalError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class TaskApprovalStore(object):
    def __init__(self, path, clock=None):
        self.path = Path(path)
        self.clock = clock or time.time

    def _load(self):
        if not self.path.exists():
            return {"schema_version": 1, "items": {}}
        if self.path.stat().st_size > MAX_FILE_BYTES:
            raise ApprovalError("approval store exceeds size limit")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        items = value.get("items") if isinstance(value, dict) else None
        if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(items, dict) or len(items) > MAX_ENTRIES:
            raise ApprovalError("approval store schema is invalid")
        return {"schema_version": 1, "items": items}

    def _save(self, value):
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            os.chmod(str(self.path.parent), 0o700)
        except OSError:
            pass
        fd, name = tempfile.mkstemp(prefix=".approvals-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
            os.chmod(name, 0o600)
            try:
                lock_path = self.path.with_suffix(self.path.suffix + ".lock")
                if lock_path.exists():
                    os.chmod(str(lock_path), 0o600)
            except OSError:
                pass
            os.replace(name, str(self.path))
            os.chmod(str(self.path), 0o600)
        finally:
            try:
                os.unlink(name)
            except OSError:
                pass

    def _lock(self):
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        handle = self.path.with_suffix(self.path.suffix + ".lock").open("a+b")
        if fcntl:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    @staticmethod
    def _public(item):
        return {key: copy.deepcopy(item.get(key)) for key in (
            "request_id", "task_id", "repository", "issue", "issue_url", "title", "summary",
            "allowed_paths", "test_command", "expires_at", "status", "created_at", "decided_at", "decided_by"
        )}

    def create_pending(self, request, ttl_seconds=24 * 60 * 60):
        if not isinstance(request, dict):
            raise ApprovalError("approval request is invalid")
        if not request.get("task_id") or not request.get("repository") or not request.get("issue_digest"):
            raise ApprovalError("approval request is missing task identity")
        ttl = max(60, min(int(ttl_seconds), 7 * 24 * 60 * 60))
        with _LOCK:
            lock = self._lock()
            try:
                state = self._load()
                request_id = secrets.token_urlsafe(18)
                now = int(self.clock())
                record = {"request_id": request_id, "request_digest": _digest(request),
                          "issue_digest": str(request["issue_digest"]), "request": copy.deepcopy(request),
                          "status": "PENDING", "created_at": now, "expires_at": now + ttl,
                          "decided_at": None, "decided_by": ""}
                state["items"][request_id] = record
                for key, item in list(state["items"].items()):
                    if int(item.get("expires_at", 0)) <= now or item.get("status") == "CONSUMED":
                        del state["items"][key]
                while len(state["items"]) > MAX_ENTRIES:
                    oldest = min(state["items"], key=lambda key: int(state["items"][key].get("created_at", 0)))
                    del state["items"][oldest]
                self._save(state)
                return self._public(record)
            finally:
                if fcntl:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                lock.close()

    def pending(self):
        with _LOCK:
            state = self._load()
        now = int(self.clock())
        return [self._public(item) for item in state["items"].values()
                if item.get("status") == "PENDING" and int(item.get("expires_at", 0)) > now]

    def decide(self, request_id, decision, actor):
        if decision not in ("approve", "reject"):
            raise ApprovalError("invalid approval decision")
        with _LOCK:
            lock = self._lock()
            try:
                state = self._load()
                item = state["items"].get(str(request_id))
                now = int(self.clock())
                if not item or item.get("status") != "PENDING" or int(item.get("expires_at", 0)) <= now:
                    raise ApprovalError("approval is unavailable or expired")
                item["status"] = "APPROVED" if decision == "approve" else "REJECTED"
                item["decided_at"] = now
                item["decided_by"] = str(actor or "")[:80]
                self._save(state)
                return self._public(item)
            finally:
                if fcntl:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                lock.close()

    def consume(self, request_id, issue_digest):
        with _LOCK:
            lock = self._lock()
            try:
                state = self._load()
                item = state["items"].get(str(request_id))
                now = int(self.clock())
                if not item:
                    return {"state": "missing", "plan": None}
                if int(item.get("expires_at", 0)) <= now:
                    return {"state": "expired", "plan": None}
                if item.get("status") == "REJECTED":
                    return {"state": "rejected", "plan": None}
                if item.get("status") == "CONSUMED":
                    return {"state": "consumed", "plan": None}
                if item.get("issue_digest") != str(issue_digest):
                    return {"state": "mismatch", "plan": None}
                if item.get("status") != "APPROVED":
                    return {"state": "pending", "plan": None}
                if _digest(item.get("request")) != item.get("request_digest"):
                    raise ApprovalError("approved plan was modified")
                item["status"] = "CONSUMED"
                self._save(state)
                return {"state": "approved", "plan": copy.deepcopy(item["request"].get("plan"))}
            finally:
                if fcntl:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                lock.close()
