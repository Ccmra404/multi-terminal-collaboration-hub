#!/usr/bin/env python3
"""Local event, handoff and approval workflow for collaboration v2.

This module is intentionally a local coordinator.  It provides a durable
event inbox and an approval state machine, while keeping GitHub access behind
a GET-only handoff adapter.  It never executes task payloads, writes GitHub,
merges pull requests, wakes another process or treats a comment as approval.
"""

from __future__ import print_function

import copy
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import authorization as _authorization
except ImportError:  # pragma: no cover - run-from-root convenience
    _tools = str(Path(__file__).resolve().parent)
    if _tools not in sys.path:
        sys.path.insert(0, _tools)
    import authorization as _authorization


try:  # Linux is the supported server, but tests remain portable.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


try:
    from evidence import EvidenceError
except ImportError:  # pragma: no cover
    from .evidence import EvidenceError


class WorkflowError(ValueError):
    """Expected local workflow/state failure."""
SCHEMA_VERSION = 1
MAX_EVENTS = 2000
MAX_APPROVALS = 500
MAX_AUDIT = 4000
MAX_PAYLOAD_CHARS = 16000
EVENT_MARKER = "<!-- collaboration-hub:event:v1 -->"
EVENT_ID_RE = re.compile(r"^evt-[0-9a-f]{16,64}$")


def _safe(value, maximum=240):
    return _authorization._safe_text(value, maximum)


def _iso(value=None):
    return _authorization._iso(value)


def _utc(value=None):
    return _authorization._utc(value)


def _digest(value):
    return _authorization._digest(value)


def _sanitize(value, depth=0):
    """Bound data before it enters the durable state or a handoff envelope."""
    if depth > 6:
        return "<truncated>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _safe(value, MAX_PAYLOAD_CHARS)
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth + 1) for item in list(value)[:200]]
    if isinstance(value, dict):
        result = {}
        for key in list(value.keys())[:200]:
            normalized_key = _safe(key, 120)
            if not normalized_key:
                continue
            result[normalized_key] = _sanitize(value[key], depth + 1)
        return result
    return _safe(value, 240)


def _default_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "events": [],
        "approvals": {},
        "audit": [],
    }


class WorkflowStore(object):
    """Atomic, 0600, cross-process local workflow state."""

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
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise WorkflowError("cannot read workflow state: " + _safe(error, 240))
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise WorkflowError("workflow state has an unsupported schema")
        events = value.get("events")
        approvals = value.get("approvals")
        audit = value.get("audit")
        if not isinstance(events, list) or len(events) > MAX_EVENTS:
            raise WorkflowError("workflow events are invalid or too large")
        if not isinstance(approvals, dict) or len(approvals) > MAX_APPROVALS:
            raise WorkflowError("workflow approvals are invalid or too large")
        if not isinstance(audit, list) or len(audit) > MAX_AUDIT:
            raise WorkflowError("workflow audit is invalid or too large")
        return {
            "schema_version": SCHEMA_VERSION,
            "events": events,
            "approvals": approvals,
            "audit": audit,
        }

    def _write_unlocked(self, value):
        self._ensure_parent()
        fd, temporary = tempfile.mkstemp(prefix=".workflow-", dir=str(self.path.parent))
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

    def _audit(self, state, event, **details):
        record = {"event": _safe(event, 80), "recorded_at": _iso()}
        record.update({key: _sanitize(value) for key, value in details.items()})
        state["audit"].append(record)
        if len(state["audit"]) > MAX_AUDIT:
            del state["audit"][:-MAX_AUDIT]

    def append_event(
        self,
        kind,
        project_id,
        repository,
        task_id,
        actor,
        target,
        payload=None,
        created_at=None,
        event_id=None,
        dedupe_key=None,
    ):
        kind = _safe(kind, 80)
        if not kind:
            raise WorkflowError("event kind is required")
        project_id = _safe(project_id, 120)
        repository = _safe(repository, 240)
        task_id = _safe(task_id, 120)
        actor = _safe(actor, 120)
        target = _safe(target or "all", 120) or "all"
        payload = _sanitize(payload or {})
        created = _iso(created_at)
        dedupe_key = _safe(dedupe_key, 240) if dedupe_key else ""
        identity = {
            "kind": kind,
            "project_id": project_id,
            "repository": repository,
            "task_id": task_id,
            "actor": actor,
            "target": target,
            "payload": payload,
            "dedupe_key": dedupe_key,
        }
        generated_id = "evt-" + _digest(identity)[:32]
        event_id = _safe(event_id, 80).lower() if event_id else generated_id
        if not EVENT_ID_RE.fullmatch(event_id):
            raise WorkflowError("event_id is invalid")
        event = dict(identity)
        event.update({"schema_version": 1, "event_id": event_id, "created_at": created, "acked_by": []})
        with self._locked() as state:
            for existing in state["events"]:
                if not isinstance(existing, dict):
                    continue
                same_id = existing.get("event_id") == event_id
                same_key = dedupe_key and existing.get("dedupe_key") == dedupe_key
                if same_id or same_key:
                    comparable = dict(existing)
                    comparable.pop("acked_by", None)
                    comparable.pop("created_at", None)
                    candidate = dict(event)
                    candidate.pop("acked_by", None)
                    candidate.pop("created_at", None)
                    if _digest(comparable) != _digest(candidate):
                        raise WorkflowError("event dedupe collision")
                    return copy.deepcopy(existing)
            if len(state["events"]) >= MAX_EVENTS:
                raise WorkflowError("event inbox is full")
            state["events"].append(event)
            self._audit(state, "event_appended", event_id=event_id, kind=kind)
            return copy.deepcopy(event)

    def get_event(self, event_id):
        event_id = _safe(event_id, 80).lower()
        with self._locked() as state:
            for event in state["events"]:
                if isinstance(event, dict) and event.get("event_id") == event_id:
                    return copy.deepcopy(event)
        return None

    def discover(self, consumer, limit=100):
        consumer = _safe(consumer, 120)
        try:
            limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            raise WorkflowError("event limit must be an integer")
        with self._locked() as state:
            result = []
            for event in state["events"]:
                if not isinstance(event, dict):
                    continue
                if event.get("target") not in (consumer, "all"):
                    continue
                if consumer in (event.get("acked_by") or []):
                    continue
                result.append(copy.deepcopy(event))
                if len(result) >= limit:
                    break
            return result

    def ack(self, event_id, consumer):
        event_id = _safe(event_id, 80).lower()
        consumer = _safe(consumer, 120)
        with self._locked() as state:
            for event in state["events"]:
                if not isinstance(event, dict) or event.get("event_id") != event_id:
                    continue
                acks = event.setdefault("acked_by", [])
                if consumer not in acks:
                    acks.append(consumer)
                    self._audit(state, "event_acknowledged", event_id=event_id, consumer=consumer)
                    return True
                return False
        raise WorkflowError("event does not exist")

    def put_pending(self, request, result, evidence=None):
        if not isinstance(request, dict) or not isinstance(result, dict):
            raise WorkflowError("pending request and result must be objects")
        request_id = _safe(result.get("request_id"), 120)
        binding_digest = _safe(result.get("binding_digest"), 128).lower()
        if not request_id or not re.fullmatch(r"[0-9a-f]{64}", binding_digest):
            raise WorkflowError("pending result must include request_id and binding_digest")
        item = {
            "request_id": request_id,
            "status": _authorization.PENDING_APPROVAL,
            "request": _sanitize(request),
            "result": _sanitize(result),
            "binding_digest": binding_digest,
            "evidence": _sanitize(evidence or {}),
            "created_at": _safe(result.get("created_at"), 40),
            "expires_at": _safe(result.get("expires_at"), 40),
        }
        with self._locked() as state:
            existing = state["approvals"].get(request_id)
            if existing is not None:
                if existing.get("binding_digest") != binding_digest:
                    raise WorkflowError("approval request_id collision")
                return copy.deepcopy(existing)
            if len(state["approvals"]) >= MAX_APPROVALS:
                raise WorkflowError("approval inbox is full")
            state["approvals"][request_id] = item
            self._audit(state, "approval_pending", request_id=request_id, binding_digest=binding_digest)
            return copy.deepcopy(item)

    def get_approval(self, request_id):
        request_id = _safe(request_id, 120)
        with self._locked() as state:
            item = state["approvals"].get(request_id)
            return copy.deepcopy(item) if item is not None else None

    def pending_approvals(self, now=None):
        current = _utc(now)
        with self._locked() as state:
            result = []
            for item in state["approvals"].values():
                if not isinstance(item, dict) or item.get("status") != _authorization.PENDING_APPROVAL:
                    continue
                try:
                    if current < _utc(item.get("expires_at")):
                        result.append(copy.deepcopy(item))
                except Exception:
                    continue
            return result

    def decide(self, request_id, decision, actor, reason="", now=None, verified=False):
        if verified is not True:
            raise WorkflowError("approval decision requires a verified authority")
        request_id = _safe(request_id, 120)
        decision = _safe(decision, 30).lower()
        actor = _safe(actor, 120)
        reason = _safe(reason, 500) or "no reason supplied"
        if decision not in ("approve", "reject", "revoke"):
            raise WorkflowError("decision must be approve, reject or revoke")
        current = _iso(now)
        with self._locked() as state:
            item = state["approvals"].get(request_id)
            if not isinstance(item, dict):
                raise WorkflowError("approval request does not exist")
            status = item.get("status")
            if decision in ("approve", "reject") and status != _authorization.PENDING_APPROVAL:
                raise WorkflowError("only pending approvals can be decided")
            if decision == "revoke" and status not in (_authorization.PENDING_APPROVAL, "APPROVED"):
                raise WorkflowError("approval is not revocable in its current state")
            item["decision"] = decision
            item["decision_actor"] = actor
            item["decision_reason"] = reason
            item["decision_at"] = current
            if decision == "approve":
                item["status"] = "APPROVED"
                item["approval_id"] = "approval-" + _digest(
                    {"request_id": request_id, "binding_digest": item["binding_digest"], "actor": actor, "at": current}
                )[:32]
            elif decision == "reject":
                item["status"] = "REJECTED"
            else:
                item["status"] = "REVOKED"
            self._audit(
                state,
                "approval_decided",
                request_id=request_id,
                decision=decision,
                actor=actor,
            )
            return copy.deepcopy(item)

    def consume_approval(self, request_id, binding_digest, now=None):
        request_id = _safe(request_id, 120)
        binding_digest = _safe(binding_digest, 128).lower()
        current = _utc(now)
        with self._locked() as state:
            item = state["approvals"].get(request_id)
            if not isinstance(item, dict):
                return {"state": "mismatch", "reason": "approval request does not exist"}
            if item.get("binding_digest") != binding_digest:
                return {"state": "mismatch", "reason": "approval binding does not match"}
            status = item.get("status")
            if status == "CONSUMED":
                return {
                    "state": "consumed",
                    "approval_id": item.get("approval_id", ""),
                    "binding_digest": binding_digest,
                    "expires_at": item.get("expires_at", ""),
                }
            if status == "APPROVED":
                try:
                    expires = _utc(item.get("expires_at"))
                except Exception:
                    return {"state": "expired", "reason": "approval expiry is invalid"}
                if current >= expires:
                    item["status"] = "EXPIRED"
                    self._audit(state, "approval_expired", request_id=request_id)
                    return {"state": "expired", "reason": "approval has expired"}
                item["status"] = "CONSUMED"
                item["consumed_at"] = _iso(current)
                self._audit(state, "approval_consumed", request_id=request_id)
                return {
                    "state": "approved",
                    "approval_id": item.get("approval_id", ""),
                    "binding_digest": binding_digest,
                    "expires_at": item.get("expires_at", ""),
                }
            if status in ("REJECTED", "REVOKED", "EXPIRED"):
                return {"state": status.lower(), "reason": "approval is " + status.lower()}
            return {"state": "unverified", "reason": "approval is still pending"}

    def audit(self, limit=100):
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            raise WorkflowError("audit limit must be an integer")
        with self._locked() as state:
            return copy.deepcopy(state["audit"][-limit:])


class ApprovalAuthority(object):
    """Default authority: no product authentication is connected."""

    def verify(self, request_id, decision, actor, reason, record):
        del request_id, decision, actor, reason, record
        return {
            "verified": False,
            "reason_code": "no_trusted_approval_authority",
            "reason": "trusted user authentication is not connected",
        }


class SimulationApprovalAuthority(ApprovalAuthority):
    """Opt-in demo authority; never represents a real maintainer identity."""

    def __init__(self, enabled=False, actor="simulated-maintainer"):
        self.enabled = bool(enabled)
        self.actor = _safe(actor, 120)

    def verify(self, request_id, decision, actor, reason, record):
        del request_id, reason, record
        if not self.enabled or decision not in ("approve", "reject", "revoke") or actor != self.actor:
            return {
                "verified": False,
                "reason_code": "simulation_not_enabled",
                "reason": "simulation approval requires explicit demo authority",
            }
        return {"verified": True, "provenance": "simulation_only"}


class PersistentApprovalVerifier(_authorization.ApprovalVerifier):
    """Adapter from the durable approval record to PolicyEngine's verifier seam."""

    def __init__(self, store):
        self.store = store

    def verify(self, approval, binding, now):
        if not isinstance(approval, dict):
            return {"state": "unverified", "reason": "approval reference is invalid"}
        request_id = _safe(approval.get("request_id"), 120)
        return self.store.consume_approval(request_id, _digest(binding), now=now)


class GithubHandoffAdapter(object):
    """Read bounded machine envelopes from GitHub issues/PR comments."""

    def __init__(self, api, repository, authorized_users=(), max_pages=3, per_page=100):
        self.api = api
        self.repository = _authorization._require_repository(repository)
        self.authorized_users = set(_safe(item, 120) for item in authorized_users if _safe(item, 120))
        self.max_pages = max(1, min(int(max_pages), 20))
        self.per_page = max(1, min(int(per_page), 100))

    def _path(self, suffix):
        return "/repos/" + self.repository + suffix

    def _comments(self, number):
        rows = []
        for page in range(1, self.max_pages + 1):
            payload = self.api.get(
                self._path("/issues/%d/comments" % number),
                {"per_page": self.per_page, "page": page},
            )
            if not isinstance(payload, list):
                raise WorkflowError("GitHub handoff comments are not a list")
            rows.extend(payload)
            if len(payload) < self.per_page:
                return rows
        raise WorkflowError("GitHub handoff pagination is incomplete")

    @staticmethod
    def parse_envelope(body, author="", created_at="", source="comment", authorized_users=()):
        text = str(body or "")[:MAX_PAYLOAD_CHARS]
        marker_index = text.find(EVENT_MARKER)
        if marker_index < 0:
            return None
        remainder = text[marker_index + len(EVENT_MARKER):].lstrip()
        line = remainder.splitlines()[0][:MAX_PAYLOAD_CHARS]
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            return None
        if not isinstance(event, dict) or event.get("schema_version") != 1:
            return None
        if not re.fullmatch(r"evt-[0-9a-f]{16,64}", _safe(event.get("event_id"), 80).lower()):
            return None
        normalized_author = _safe(author, 120)
        authorized = set(_safe(item, 120) for item in authorized_users if _safe(item, 120))
        return {
            "event": _sanitize(event),
            "author": normalized_author or "unknown",
            "created_at": _safe(created_at, 40),
            "source": _safe(source, 40),
            "verified": bool(normalized_author and normalized_author in authorized),
        }

    def read_issue(self, issue_number):
        try:
            issue_number = int(issue_number)
        except (TypeError, ValueError):
            raise WorkflowError("issue number must be an integer")
        if issue_number <= 0:
            raise WorkflowError("issue number must be positive")
        issue = self.api.get(self._path("/issues/%d" % issue_number))
        comments = self._comments(issue_number)
        sources = []
        if isinstance(issue, dict):
            sources.append((issue, "issue"))
        sources.extend((comment, "comment") for comment in comments)
        records = []
        for item, source in sources:
            if not isinstance(item, dict):
                continue
            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            record = self.parse_envelope(
                item.get("body"),
                author=user.get("login", ""),
                created_at=item.get("created_at", ""),
                source=source,
                authorized_users=self.authorized_users,
            )
            if record:
                records.append(record)
        return {"issue": issue_number, "events": records, "write_capability": False}

    @staticmethod
    def render_event(event):
        clean = _sanitize(event)
        if not isinstance(clean, dict) or clean.get("schema_version") != 1:
            raise WorkflowError("event envelope must have schema_version 1")
        encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded) > 10000:
            raise WorkflowError("event envelope is too large")
        return EVENT_MARKER + "\n" + encoded


class WorkflowCoordinator(object):
    """Connect evidence, policy and durable handoffs without an executor."""

    def __init__(self, policy_engine, store, evidence_adapter=None, approval_authority=None):
        self.engine = policy_engine
        self.store = store
        self.evidence_adapter = evidence_adapter
        self.approval_authority = approval_authority or ApprovalAuthority()

    def submit_task(self, project_id, repository, task_id, actor, target, payload=None, dedupe_key=None):
        return self.store.append_event(
            "task_submitted",
            project_id,
            repository,
            task_id,
            actor,
            target,
            payload=payload or {},
            dedupe_key=dedupe_key or "task:" + _safe(repository, 240) + ":" + _safe(task_id, 120),
        )

    def discover(self, consumer, limit=100):
        return self.store.discover(consumer, limit=limit)

    def report(self, event_id, actor, status, summary=""):
        original = self.store.get_event(event_id)
        if original is None:
            raise WorkflowError("task event does not exist")
        report = self.store.append_event(
            "task_reported",
            original.get("project_id", ""),
            original.get("repository", ""),
            original.get("task_id", ""),
            actor,
            original.get("actor") or "all",
            payload={"status": status, "summary": summary, "source_event_id": event_id},
            dedupe_key="report:" + event_id + ":" + _safe(status, 80),
        )
        self.store.ack(event_id, actor)
        return report

    def _safe_evidence_failure(self, request, now, error):
        fallback = copy.deepcopy(request) if isinstance(request, dict) else {}
        fallback["paths"] = []
        fallback["path_evidence"] = {"source": "", "complete": False, "paths": []}
        fallback["ci_passed"] = False
        fallback["independent_review"] = False
        fallback.pop("evidence_digest", None)
        result = self.engine.evaluate(fallback, now=now)
        result["reason_code"] = "evidence_unavailable"
        result["reason"] = "trusted evidence unavailable: " + _safe(error, 400)
        result["evidence_error"] = _safe(error, 400)
        return result

    def request_authorization(self, request, now=None):
        now = _utc(now)
        enriched = copy.deepcopy(request)
        snapshot = {}
        action = _safe(request.get("action"), 120) if isinstance(request, dict) else ""
        if action in _authorization.CODE_ACTIONS:
            if self.evidence_adapter is None:
                return self._safe_evidence_failure(enriched, now, "no trusted evidence adapter configured")
            try:
                enriched, snapshot = self.evidence_adapter.enrich(enriched)
            except (EvidenceError, ValueError, TypeError) as error:
                return self._safe_evidence_failure(enriched, now, error)
        result = self.engine.evaluate(enriched, now=now)
        if snapshot:
            result["evidence"] = {
                "source": snapshot.get("source"),
                "provenance": snapshot.get("provenance"),
                "evidence_digest": snapshot.get("evidence_digest"),
                "ci_state": snapshot.get("ci_state"),
                "review_state": snapshot.get("review_state"),
                "observed_at": snapshot.get("observed_at"),
            }
        if result.get("decision") == _authorization.PENDING_APPROVAL:
            self.store.put_pending(enriched, result, evidence=snapshot)
            self.store.append_event(
                "approval_requested",
                result.get("project_id", ""),
                result.get("target", {}).get("repository", ""),
                result.get("task_id", ""),
                result.get("actor", ""),
                "maintainer",
                payload={"request_id": result.get("request_id"), "reason_code": result.get("reason_code")},
                dedupe_key="approval:" + _safe(result.get("request_id"), 120),
            )
        return result

    def decide(self, request_id, decision, actor, reason="", now=None):
        decision = _safe(decision, 30).lower()
        record = self.store.get_approval(request_id)
        if record is None:
            raise WorkflowError("approval request does not exist")
        verdict = self.approval_authority.verify(
            request_id, decision, actor, reason, record
        )
        if not isinstance(verdict, dict) or verdict.get("verified") is not True:
            return {
                "decision": _authorization.PENDING_APPROVAL,
                "request_id": _safe(request_id, 120),
                "reason_code": (verdict or {}).get("reason_code", "approval_unverified"),
                "reason": _safe((verdict or {}).get("reason", "approval authority is not trusted"), 400),
                "approval": {"required": True, "status": "unverified"},
            }
        item = self.store.decide(request_id, decision, actor, reason, now=now, verified=True)
        self.store.append_event(
            "approval_decided",
            item.get("request", {}).get("project_id", ""),
            item.get("request", {}).get("repository", ""),
            item.get("request", {}).get("task_id", ""),
            actor,
            "server",
            payload={"request_id": request_id, "decision": decision, "provenance": verdict.get("provenance", "")},
            dedupe_key="decision:" + _safe(request_id, 120) + ":" + _safe(decision, 30),
        )
        return item

    def resume(self, request_id, now=None):
        record = self.store.get_approval(request_id)
        if record is None:
            return {
                "decision": _authorization.DENY,
                "request_id": _safe(request_id, 120),
                "reason_code": "approval_not_found",
                "reason": "approval request does not exist",
            }
        request = copy.deepcopy(record.get("request"))
        if not isinstance(request, dict):
            return {
                "decision": _authorization.DENY,
                "request_id": _safe(request_id, 120),
                "reason_code": "approval_request_invalid",
                "reason": "persisted approval request is invalid",
            }
        if self.evidence_adapter is not None and request.get("action") in _authorization.CODE_ACTIONS:
            try:
                refreshed, snapshot = self.evidence_adapter.enrich(request)
            except (EvidenceError, ValueError, TypeError) as error:
                return self._safe_evidence_failure(request, _utc(now), error)
            if refreshed.get("evidence_digest") != request.get("evidence_digest"):
                return self._safe_evidence_failure(request, _utc(now), "evidence changed since approval request")
            request = refreshed
        verifier = PersistentApprovalVerifier(self.store)
        engine = _authorization.PolicyEngine(self.engine.policy, verifier=verifier)
        result = engine.evaluate(request, approval={"request_id": request_id}, now=now)
        if result.get("decision") in (_authorization.ALLOW, _authorization.DENY):
            self.store.append_event(
                "authorization_resumed",
                result.get("project_id", ""),
                result.get("target", {}).get("repository", ""),
                result.get("task_id", ""),
                result.get("actor", ""),
                result.get("executor", "server"),
                payload={"request_id": request_id, "decision": result.get("decision"), "reason_code": result.get("reason_code")},
                dedupe_key="resume:" + _safe(request_id, 120) + ":" + _safe(result.get("reason_code"), 80),
            )
        return result


__all__ = [
    "ApprovalAuthority",
    "EVENT_MARKER",
    "GithubHandoffAdapter",
    "PersistentApprovalVerifier",
    "SimulationApprovalAuthority",
    "WorkflowCoordinator",
    "WorkflowError",
    "WorkflowStore",
]
