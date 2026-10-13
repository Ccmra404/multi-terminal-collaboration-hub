#!/usr/bin/env python3
"""Local authorization policy engine for multi-endpoint collaboration.

The module deliberately has no GitHub, network, or systemd adapter.  Its small
interface is :class:`PolicyEngine.evaluate`: a normalized request receives an
``ALLOW``, ``PENDING_APPROVAL``, or ``DENY`` decision plus a bounded audit
record.  A trusted approval verifier is an explicit seam; the default verifier
never treats GitHub comments or an actor's assertion as proof of approval.
"""

from __future__ import print_function

import fnmatch
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath


ALLOW = "ALLOW"
PENDING_APPROVAL = "PENDING_APPROVAL"
DENY = "DENY"

# Only this deliberately small set may ever be configured for autonomous
# execution.  Every other action must remain approval-gated; adding an action
# to this set is a code-reviewed policy change, not a JSON configuration tweak.
AUTONOMOUS_ACTIONS = frozenset(
    ("read", "run_tests", "code_change", "branch_push", "pull_request")
)
MANDATORY_APPROVAL_ACTIONS = frozenset(
    (
        "credential_change",
        "permissions_change",
        "production_deploy",
        "release",
        "delete",
        "force_push",
        "repository_visibility",
        "public_publish",
        "external_data_share",
        "security_policy_change",
        "branch_protection_change",
        "database_migration",
        "destructive_data_change",
        "bypass_protection",
        "force_merge",
    )
)
CODE_ACTIONS = frozenset(("code_change", "branch_push", "pull_request", "merge"))
TRUSTED_PATH_EVIDENCE_SOURCES = frozenset(("trusted_diff", "github_pr_files"))
# These paths are a policy invariant, not a project-level opt-out.  A project
# may add more sensitive patterns, but it can never remove this baseline.
MINIMUM_SENSITIVE_PATHS = (
    "deploy/**",
    "config/**",
    ".github/workflows/**",
    "SECURITY.md",
)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b")
_CREDENTIAL_RE = re.compile(
    r"(?i)(?P<key>\b(?:authorization|password|secret|private[_ -]?key|token)\b)"
    r"(?P<separator>\s*[:=]\s*)(?P<prefix>Bearer\s+)?(?P<value>[^\s,;]+)"
)


class AuthorizationError(ValueError):
    """Raised when the machine-readable policy itself is invalid."""


def _safe_text(value, max_chars=240):
    text = "" if value is None else str(value)
    text = _TOKEN_RE.sub("<redacted>", text)
    text = _CREDENTIAL_RE.sub(
        lambda match: match.group("key")
        + match.group("separator")
        + (match.group("prefix") or "")
        + "<redacted>",
        text,
    )
    text = _CONTROL_RE.sub(" ", text)
    return " ".join(text.split())[:max_chars]


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _utc(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip()
        result = _parse_iso8601(text)
    if result.tzinfo is None:
        raise AuthorizationError("timestamp must include a timezone")
    return result.astimezone(timezone.utc)


def _parse_iso8601(text):
    """Parse the bounded timestamp forms supported by Python 3.6+.

    ``datetime.fromisoformat`` was added in Python 3.7, while the project
    still supports Python 3.6 on the server.  Keep parsing deliberately
    narrow so malformed or ambiguous timestamps fail closed.
    """
    if not text:
        raise AuthorizationError("timestamp must be ISO-8601")
    if text.endswith("Z"):
        text = text[:-1] + "+0000"
    elif len(text) >= 6 and text[-6] in ("+", "-") and text[-3] == ":":
        text = text[:-3] + text[-2:]
    formats = (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S%z",
    )
    for date_format in formats:
        try:
            return datetime.strptime(text, date_format)
        except (TypeError, ValueError):
            continue
    raise AuthorizationError("timestamp must be ISO-8601")


def _iso(value):
    return _utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _require_id(value, field):
    value = _safe_text(value, 120)
    if not _ID_RE.fullmatch(value):
        raise AuthorizationError(field + " must be a bounded identifier")
    return value


def _require_repository(value):
    value = _safe_text(value, 240)
    if not _REPOSITORY_RE.fullmatch(value):
        raise AuthorizationError("repository must be an explicit owner/name")
    return value


def _normalize_path(value):
    path = _safe_text(value, 240).strip()
    if not path or "\\" in path or path.startswith("/"):
        raise AuthorizationError("paths must be relative POSIX paths")
    parts = PurePosixPath(path).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise AuthorizationError("paths cannot contain traversal")
    return "/".join(parts)


def _normalize_sensitive_pattern(value):
    pattern = _safe_text(value, 240).strip()
    if not pattern or "\\" in pattern or pattern.startswith("/"):
        raise AuthorizationError("sensitive_paths must be relative patterns")
    parts = PurePosixPath(pattern).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise AuthorizationError("sensitive_paths cannot contain traversal")
    return "/".join(parts)


def _bounded_list(values, field, normalizer, allow_empty=True):
    if values is None:
        values = []
    if not isinstance(values, list):
        raise AuthorizationError(field + " must be a list")
    result = []
    for value in values:
        item = normalizer(value)
        if item not in result:
            result.append(item)
    if not allow_empty and not result:
        raise AuthorizationError(field + " must not be empty")
    return result


def _normalize_policy(data):
    if not isinstance(data, dict):
        raise AuthorizationError("policy must be a JSON object")
    if int(data.get("schema_version", 0)) != 1:
        raise AuthorizationError("unsupported policy schema_version")
    try:
        ttl = int(data.get("approval_ttl_seconds", 900))
    except (TypeError, ValueError):
        raise AuthorizationError("approval_ttl_seconds must be an integer")
    if ttl < 60 or ttl > 86400:
        raise AuthorizationError("approval_ttl_seconds must be between 60 and 86400")
    projects = data.get("projects")
    if not isinstance(projects, list) or not projects:
        raise AuthorizationError("projects must be a non-empty list")
    normalized = []
    seen = set()
    seen_repositories = {}
    for item in projects:
        if not isinstance(item, dict):
            raise AuthorizationError("each project must be an object")
        project_id = _require_id(item.get("project_id"), "project_id")
        if project_id in seen:
            raise AuthorizationError("duplicate project_id: " + project_id)
        seen.add(project_id)
        repositories = _bounded_list(
            item.get("repositories"), "repositories", _require_repository, allow_empty=False
        )
        for repository in repositories:
            previous = seen_repositories.get(repository)
            if previous:
                raise AuthorizationError(
                    "repository is assigned to multiple projects: " + repository
                )
            seen_repositories[repository] = project_id
        branches = _bounded_list(
            item.get("allowed_branches"),
            "allowed_branches",
            lambda value: _safe_text(value, 120),
            allow_empty=False,
        )
        if any(not branch or branch == "**" for branch in branches):
            raise AuthorizationError("allowed_branches contains an unsafe pattern")
        environments = _bounded_list(
            item.get("allowed_environments"),
            "allowed_environments",
            lambda value: _require_id(value, "environment"),
            allow_empty=False,
        )
        actors = _bounded_list(
            item.get("authorized_actors"),
            "authorized_actors",
            lambda value: _require_id(value, "actor"),
            allow_empty=False,
        )
        executors = _bounded_list(
            item.get("authorized_executors"),
            "authorized_executors",
            lambda value: _require_id(value, "executor"),
            allow_empty=False,
        )
        policies = item.get("action_policies")
        if not isinstance(policies, dict) or not policies:
            raise AuthorizationError("action_policies must be a non-empty object")
        action_policies = {}
        for action, risk in policies.items():
            action = _require_id(action, "action")
            risk = _safe_text(risk, 30).lower()
            if risk not in ("allow", "approval"):
                raise AuthorizationError("action policy must be allow or approval")
            if risk == "allow" and action not in AUTONOMOUS_ACTIONS:
                if action in MANDATORY_APPROVAL_ACTIONS:
                    raise AuthorizationError(
                        "mandatory approval action cannot be configured as allow: " + action
                    )
                raise AuthorizationError(
                    "only explicitly autonomous actions may be configured as allow: " + action
                )
            action_policies[action] = risk
        sensitive_paths = _bounded_list(
            item.get("sensitive_paths"),
            "sensitive_paths",
            _normalize_sensitive_pattern,
        )
        missing_sensitive_paths = [
            pattern for pattern in MINIMUM_SENSITIVE_PATHS if pattern not in sensitive_paths
        ]
        if missing_sensitive_paths:
            raise AuthorizationError(
                "sensitive_paths must include immutable baseline: "
                + ", ".join(missing_sensitive_paths)
            )
        task_writers = item.get("task_writers", {})
        if not isinstance(task_writers, dict):
            raise AuthorizationError("task_writers must be an object")
        normalized_writers = {}
        for task_id, executor in task_writers.items():
            normalized_task = _require_id(task_id, "task_id")
            normalized_executor = _require_id(executor, "executor")
            if normalized_executor not in executors:
                raise AuthorizationError("task writer is not an authorized executor")
            normalized_writers[normalized_task] = normalized_executor
        checks = item.get("required_checks", {})
        if not isinstance(checks, dict):
            raise AuthorizationError("required_checks must be an object")
        normalized_checks = {
            "ci": bool(checks.get("ci", True)),
            "independent_review": bool(checks.get("independent_review", True)),
        }
        normalized.append(
            {
                "project_id": project_id,
                "repositories": repositories,
                "allowed_branches": branches,
                "allowed_environments": environments,
                "authorized_actors": actors,
                "authorized_executors": executors,
                "action_policies": action_policies,
                "sensitive_paths": sensitive_paths,
                "task_writers": normalized_writers,
                "required_checks": normalized_checks,
            }
        )
    return {"schema_version": 1, "approval_ttl_seconds": ttl, "projects": normalized}


def load_policy(path):
    """Load and validate a local JSON policy; never performs network I/O."""
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AuthorizationError("cannot read policy: " + _safe_text(error, 200))
    return _normalize_policy(data)


class ApprovalVerifier(object):
    """Trusted approval seam; the default implementation intentionally fails closed."""

    def verify(self, approval, binding, now):
        del approval, binding, now
        return {
            "state": "unverified",
            "reason_code": "no_trusted_approval_verifier",
            "reason": "trusted product authentication is not connected",
        }


class ApprovalLedger(object):
    """Single-process consumption ledger used by the local/simulated gate."""

    def __init__(self):
        self._consumed = set()

    def consumed(self, approval_id):
        return approval_id in self._consumed

    def consume(self, approval_id):
        if self.consumed(approval_id):
            return False
        self._consumed.add(approval_id)
        return True


class ApprovalQueue(object):
    """Optional local pending queue; it has no approval or network authority."""

    MAX_ITEMS = 500

    def __init__(self, path):
        self.path = Path(path).expanduser()

    def _load(self):
        if not self.path.exists():
            return {"schema_version": 1, "items": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise AuthorizationError("cannot read approval queue: " + _safe_text(error, 200))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise AuthorizationError("approval queue has an unsupported schema")
        items = value.get("items")
        if not isinstance(items, list) or len(items) > self.MAX_ITEMS:
            raise AuthorizationError("approval queue is invalid or too large")
        return {"schema_version": 1, "items": items}

    def enqueue(self, result):
        """Add one pending result, returning False when the request is already queued."""
        if not isinstance(result, dict) or result.get("decision") != PENDING_APPROVAL:
            raise AuthorizationError("only PENDING_APPROVAL results may enter the queue")
        request_id = _safe_text(result.get("request_id"), 80)
        approval_request = result.get("approval_request")
        if not request_id or not isinstance(approval_request, dict):
            raise AuthorizationError("pending result has no approval request")
        item = {
            "request_id": request_id,
            "project_id": _safe_text(result.get("project_id"), 120),
            "task_id": _safe_text(result.get("task_id"), 120),
            "executor": _safe_text(result.get("executor"), 120),
            "decision": PENDING_APPROVAL,
            "approval_request": approval_request,
        }
        queue = self._load()
        for existing in queue["items"]:
            if not isinstance(existing, dict) or existing.get("request_id") != request_id:
                continue
            if _canonical(existing) != _canonical(item):
                raise AuthorizationError("approval queue request_id collision")
            return False
        if len(queue["items"]) >= self.MAX_ITEMS:
            raise AuthorizationError("approval queue is full")
        queue["items"].append(item)
        self._write(queue)
        return True

    def pending(self, now=None):
        """Return unexpired pending entries without making them executable."""
        current = _utc(now)
        queue = self._load()
        active = []
        for item in queue["items"]:
            if not isinstance(item, dict) or item.get("decision") != PENDING_APPROVAL:
                continue
            approval_request = item.get("approval_request", {})
            try:
                expires_at = _utc(approval_request.get("expires_at"))
            except AuthorizationError:
                continue
            if current < expires_at:
                active.append(item)
        return active

    def _write(self, value):
        parent = self.path.parent
        created_parent = False
        if not parent.exists():
            parent.mkdir(parents=True, mode=0o700)
            created_parent = True
        if created_parent:
            try:
                os.chmod(str(parent), 0o700)
            except OSError:
                pass
        fd, temp_name = tempfile.mkstemp(prefix=".authorization-", dir=str(parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, str(self.path))
        finally:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


class PolicyEngine(object):
    """Deep policy module with one evaluation interface and replaceable verifier."""

    CODE_ACTIONS = frozenset(("code_change", "branch_push", "pull_request", "merge"))

    def __init__(self, policy, verifier=None, ledger=None):
        self.policy = _normalize_policy(policy)
        self.projects = {item["project_id"]: item for item in self.policy["projects"]}
        self.verifier = verifier or ApprovalVerifier()
        self.ledger = ledger or ApprovalLedger()

    def evaluate(self, request, approval=None, now=None):
        now = _utc(now)
        normalized, error = self._normalize_request(request, now)
        if error:
            return self._invalid_result(request, now, error)
        project = self.projects.get(normalized["project_id"])
        if project is None:
            return self._result(
                normalized, now, DENY, "unknown_project", "project is not authorized"
            )
        scope_error = self._scope_error(project, normalized)
        if scope_error:
            return self._result(normalized, now, DENY, scope_error[0], scope_error[1])
        if normalized.get("path_evidence_error"):
            return self._result(
                normalized,
                now,
                DENY,
                "path_evidence_required",
                normalized["path_evidence_error"],
                approval_status="not_effective",
            )
        if now > normalized["expires_at"]:
            return self._result(
                normalized, now, DENY, "request_expired", "authorization request has expired"
            )
        action_policy = project["action_policies"][normalized["action"]]
        sensitive_matches = [
            path
            for path in normalized["paths"]
            if any(fnmatch.fnmatchcase(path, pattern) for pattern in project["sensitive_paths"])
        ]
        missing_checks = self._missing_checks(project, normalized)
        requires_approval = (
            action_policy == "approval"
            or normalized["action"] in MANDATORY_APPROVAL_ACTIONS
            or normalized["action"] not in AUTONOMOUS_ACTIONS
            or bool(sensitive_matches)
        )
        if missing_checks:
            requires_approval = True
        if not requires_approval:
            return self._result(
                normalized,
                now,
                ALLOW,
                "within_scope",
                "ordinary action is authorized",
                approval_status="not_required",
            )
        if missing_checks and approval is not None:
            return self._result(
                normalized,
                now,
                DENY,
                "required_checks_missing",
                "required CI or independent review is not complete",
                approval_status="not_effective",
                extra={"missing_checks": missing_checks},
            )
        if approval is None:
            reason_code = "sensitive_path" if sensitive_matches else "approval_required"
            reason = (
                "sensitive path requires maintenance approval"
                if sensitive_matches
                else "action is classified as approval-required"
            )
            return self._result(
                normalized,
                now,
                PENDING_APPROVAL,
                reason_code,
                reason,
                approval_status="required",
                extra={"sensitive_paths": sensitive_matches, "missing_checks": missing_checks},
            )
        verification = self.verifier.verify(approval, normalized["binding"], now)
        return self._apply_verification(normalized, now, verification)

    def _normalize_request(self, request, now):
        if not isinstance(request, dict):
            return None, "request must be an object"
        try:
            project_id = _require_id(request.get("project_id"), "project_id")
            repository = _require_repository(request.get("repository"))
            branch = _safe_text(request.get("branch"), 160).strip()
            environment = _require_id(request.get("environment"), "environment")
            action = _require_id(request.get("action"), "action")
            actor = _require_id(request.get("actor"), "actor")
            executor = _require_id(request.get("executor"), "executor")
            head_sha = _safe_text(request.get("head_sha"), 80).lower()
            if not _SHA_RE.fullmatch(head_sha):
                raise AuthorizationError("head_sha must be a 40-character SHA")
            version = _safe_text(request.get("version"), 160)
            task_id = _safe_text(request.get("task_id"), 120)
            issue_ref = _safe_text(request.get("issue_ref"), 240)
            reason = _safe_text(request.get("reason"), 500) or "no reason supplied"
            evidence_digest = _safe_text(request.get("evidence_digest"), 128).lower()
            if evidence_digest and not re.fullmatch(r"[0-9a-f]{64}", evidence_digest):
                raise AuthorizationError("evidence_digest must be a SHA-256 digest")
            raw_paths = request.get("paths", [])
            paths = _bounded_list(raw_paths, "paths", _normalize_path)
            path_evidence_source = ""
            path_evidence_complete = False
            path_evidence_error = None
            if action in CODE_ACTIONS:
                try:
                    if not paths:
                        raise AuthorizationError(
                            "code actions require a non-empty trusted path evidence list"
                        )
                    evidence = request.get("path_evidence")
                    if not isinstance(evidence, dict):
                        raise AuthorizationError(
                            "code actions require path_evidence from a trusted diff adapter"
                        )
                    path_evidence_source = _safe_text(evidence.get("source"), 80)
                    if path_evidence_source not in TRUSTED_PATH_EVIDENCE_SOURCES:
                        raise AuthorizationError(
                            "path_evidence source is not a trusted diff or PR Files adapter"
                        )
                    if evidence.get("complete") is not True:
                        raise AuthorizationError("path_evidence must be marked complete")
                    evidence_paths = _bounded_list(
                        evidence.get("paths"),
                        "path_evidence.paths",
                        _normalize_path,
                        allow_empty=False,
                    )
                    if evidence_paths != paths:
                        raise AuthorizationError(
                            "path_evidence.paths must exactly match paths"
                        )
                    path_evidence_complete = True
                except AuthorizationError as error:
                    path_evidence_error = _safe_text(error, 240)
            created_at = _utc(request.get("created_at")) if request.get("created_at") else now
            if created_at > now + timedelta(seconds=60):
                raise AuthorizationError("created_at is too far in the future")
            ci_passed = request.get("ci_passed")
            independent_review = request.get("independent_review")
            if ci_passed is not None and not isinstance(ci_passed, bool):
                raise AuthorizationError("ci_passed must be boolean")
            if independent_review is not None and not isinstance(independent_review, bool):
                raise AuthorizationError("independent_review must be boolean")
            expires_at = created_at + timedelta(seconds=self.policy["approval_ttl_seconds"])
            binding = {
                "project_id": project_id,
                "repository": repository,
                "branch": branch,
                "environment": environment,
                "action": action,
                "head_sha": head_sha,
                "version": version,
                "task_id": task_id,
                "actor": actor,
                "executor": executor,
                "paths": paths,
                "path_evidence": {
                    "source": path_evidence_source,
                    "complete": path_evidence_complete,
                    "paths": paths,
                },
                "evidence_digest": evidence_digest,
                "issue_ref": issue_ref,
                "created_at": _iso(created_at),
                "expires_at": _iso(expires_at),
            }
            return (
                {
                    "project_id": project_id,
                    "repository": repository,
                    "branch": branch,
                    "environment": environment,
                    "action": action,
                    "actor": actor,
                    "executor": executor,
                    "head_sha": head_sha,
                    "version": version,
                    "task_id": task_id,
                    "issue_ref": issue_ref,
                    "reason": reason,
                    "paths": paths,
                    "path_evidence": {
                        "source": path_evidence_source,
                        "complete": path_evidence_complete,
                        "paths": paths,
                    },
                    "evidence_digest": evidence_digest,
                    "path_evidence_error": path_evidence_error,
                    "created_at": created_at,
                    "expires_at": expires_at,
                    "ci_passed": ci_passed,
                    "independent_review": independent_review,
                    "binding": binding,
                    "binding_digest": _digest(binding),
                },
                None,
            )
        except AuthorizationError as error:
            return None, _safe_text(error, 240)

    def _scope_error(self, project, request):
        if request["repository"] not in project["repositories"]:
            return "repository_out_of_scope", "repository is not in this project allowlist"
        if not any(fnmatch.fnmatchcase(request["branch"], pattern) for pattern in project["allowed_branches"]):
            return "branch_out_of_scope", "branch is not authorized for this project"
        if request["environment"] not in project["allowed_environments"]:
            return "environment_out_of_scope", "environment is not authorized for this project"
        if request["actor"] not in project["authorized_actors"]:
            return "actor_not_authorized", "actor is not authorized for this project"
        if request["executor"] not in project["authorized_executors"]:
            return "executor_not_authorized", "executor is not authorized for this project"
        if request["action"] not in project["action_policies"]:
            return "action_out_of_scope", "action is not listed in the project policy"
        if request["action"] in self.CODE_ACTIONS:
            if not request["task_id"]:
                return "task_writer_required", "code actions require a task_id and one active writer"
            assigned = project["task_writers"].get(request["task_id"])
            if not assigned:
                return "task_writer_unassigned", "task has no configured active writer"
            if assigned != request["executor"]:
                return "task_writer_conflict", "executor is not the task's active writer"
        return None

    @staticmethod
    def _missing_checks(project, request):
        if request["action"] != "merge":
            return []
        missing = []
        checks = project["required_checks"]
        if checks.get("ci") and request.get("ci_passed") is not True:
            missing.append("ci")
        if checks.get("independent_review") and request.get("independent_review") is not True:
            missing.append("independent_review")
        return missing

    def _apply_verification(self, request, now, verification):
        if not isinstance(verification, dict):
            verification = {}
        state = _safe_text(verification.get("state"), 40).lower()
        approval_id = _safe_text(verification.get("approval_id"), 160)
        verified_digest = _safe_text(verification.get("binding_digest"), 128)
        if state == "approved":
            if not approval_id or verified_digest != request["binding_digest"]:
                return self._result(
                    request,
                    now,
                    DENY,
                    "approval_binding_mismatch",
                    "approval is not bound to this exact target, SHA, and expiry",
                    approval_status="mismatch",
                )
            if self.ledger.consumed(approval_id):
                return self._result(
                    request,
                    now,
                    DENY,
                    "approval_already_used",
                    "approval token has already been consumed",
                    approval_status="consumed",
                    extra={"approval_id": approval_id},
                )
            try:
                approval_expires = _utc(verification.get("expires_at"))
            except AuthorizationError:
                return self._result(
                    request,
                    now,
                    DENY,
                    "approval_expiry_invalid",
                    "trusted approval expiry is invalid",
                    approval_status="invalid",
                )
            if now >= approval_expires or approval_expires != request["expires_at"]:
                return self._result(
                    request,
                    now,
                    DENY,
                    "approval_expired_or_rebound",
                    "approval is expired or bound to a different expiry",
                    approval_status="expired",
                    extra={"approval_id": approval_id},
                )
            if not self.ledger.consume(approval_id):
                return self._result(
                    request,
                    now,
                    DENY,
                    "approval_already_used",
                    "approval token has already been consumed",
                    approval_status="consumed",
                    extra={"approval_id": approval_id},
                )
            return self._result(
                request,
                now,
                ALLOW,
                "trusted_approval",
                "trusted approval is valid for this exact operation",
                approval_status="approved",
                extra={"approval_id": approval_id},
            )
        if state in ("rejected", "revoked", "expired", "mismatch", "consumed"):
            return self._result(
                request,
                now,
                DENY,
                "approval_" + state,
                "approval did not authorize this operation",
                approval_status=state,
                extra={"approval_id": approval_id} if approval_id else None,
            )
        return self._result(
            request,
            now,
            PENDING_APPROVAL,
            "approval_unverified",
            "approval source is not a trusted product authorization",
            approval_status="unverified",
        )

    def _invalid_result(self, request, now, error):
        fallback = {
            "project_id": _safe_text(request.get("project_id"), 120) if isinstance(request, dict) else "unknown",
            "repository": _safe_text(request.get("repository"), 240) if isinstance(request, dict) else "unknown",
            "branch": _safe_text(request.get("branch"), 160) if isinstance(request, dict) else "unknown",
            "environment": _safe_text(request.get("environment"), 120) if isinstance(request, dict) else "unknown",
            "action": _safe_text(request.get("action"), 120) if isinstance(request, dict) else "unknown",
            "actor": _safe_text(request.get("actor"), 120) if isinstance(request, dict) else "unknown",
            "executor": _safe_text(request.get("executor"), 120) if isinstance(request, dict) else "unknown",
            "head_sha": _safe_text(request.get("head_sha"), 80) if isinstance(request, dict) else "unknown",
            "version": "",
            "task_id": "",
            "issue_ref": "",
            "reason": "invalid request",
            "paths": [],
            "path_evidence": {"source": "", "complete": False, "paths": []},
            "evidence_digest": "",
            "path_evidence_error": "invalid request",
            "created_at": now,
            "expires_at": now,
            "binding": {},
            "binding_digest": "",
        }
        return self._result(fallback, now, DENY, "invalid_request", error)

    @staticmethod
    def _result(request, now, decision, reason_code, reason, approval_status="not_required", extra=None):
        target = {
            "repository": request["repository"],
            "branch": request["branch"],
            "environment": request["environment"],
            "head_sha": request["head_sha"],
            "version": request.get("version", ""),
        }
        result = {
            "schema_version": 1,
            "decision": decision,
            "request_id": "authz-" + (_digest(request["binding"])[:32] if request.get("binding") else "invalid"),
            "project_id": request["project_id"],
            "action": request["action"],
            "actor": request["actor"],
            "executor": request["executor"],
            "task_id": request.get("task_id", ""),
            "issue_ref": request.get("issue_ref", ""),
            "target": target,
            "paths": request.get("paths", []),
            "path_evidence": request.get(
                "path_evidence", {"source": "", "complete": False, "paths": []}
            ),
            "evidence_digest": request.get("evidence_digest", ""),
            "binding_digest": request.get("binding_digest", ""),
            "reason_code": reason_code,
            "reason": _safe_text(reason, 500),
            "created_at": _iso(request.get("created_at", now)),
            "expires_at": _iso(request.get("expires_at", now)),
            "approval": {
                "required": decision == PENDING_APPROVAL or approval_status not in ("not_required", "approved"),
                "status": approval_status,
            },
            "audit": {
                "event": "authorization_decision",
                "recorded_at": _iso(now),
                "source": "local_policy_engine",
            },
        }
        if decision == PENDING_APPROVAL:
            result["approval_request"] = {
                "request_id": result["request_id"],
                "action": result["action"],
                "target": target,
                "head_sha": request["head_sha"],
                "paths": request.get("paths", []),
                "path_evidence": request.get("path_evidence", {}),
                "reason": result["reason"],
                "expires_at": result["expires_at"],
                "revoke_if": ["head_sha changes", "target or scope changes", "expiry reached", "maintainer revokes"],
            }
        if extra:
            result.update(extra)
        return result
