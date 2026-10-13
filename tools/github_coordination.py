#!/usr/bin/env python3
"""Read-only GitHub coordination monitor for authorized private repositories.

The monitor reads Issues, pull requests, reviews, commit status and Actions
metadata. It never sends a GitHub write request and never executes text read
from GitHub as a command. A configuration contains one or more explicit
repositories; the default mode performs one bounded collection and writes a
local report/state file; ``--watch`` adds controlled polling.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from tools.client_handoff import HandoffValidationError, validate_event
except ImportError:  # Support direct execution from the tools directory.
    from client_handoff import HandoffValidationError, validate_event


DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_MAX_PAGES = 3
DEFAULT_MAX_BODY_CHARS = 6000
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 1.0
MAX_BACKOFF = 30.0
DEFAULT_ALLOWED_API_HOSTS = ("api.github.com",)
SHA_RE = re.compile(r"\b[0-9a-fA-F]{40}\b")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b")
CREDENTIAL_RE = re.compile(
    r"(?i)(?P<key>\b(?:authorization|password|secret|private[_ -]?key|token)\b)"
    r"(?P<separator>\s*[:=]\s*)(?P<prefix>Bearer\s+)?(?P<value>[^\s,;]+)"
)
STANDARD_TASK_BLOCK_RE = re.compile(r"```codex-task\s*\n(.*?)```", re.IGNORECASE | re.DOTALL)
HANDOFF_EVENT_BLOCK_RE = re.compile(
    r"```handoff-event\s*\n(.*?)```", re.IGNORECASE | re.DOTALL
)
MAX_HANDOFF_EVENT_BLOCKS = 20
ADMIN_AUTH_RE = re.compile(
    r"(?i)\badmin\.auth(?:\s*[:=]\s*(?:Bearer\s+)?[^\s,;]+)?"
)


class CoordinationError(Exception):
    """Expected monitor/configuration failure."""


class ApiError(CoordinationError):
    def __init__(self, path, status=None, message="GitHub API request failed"):
        self.path = path
        self.status = status
        self.message = message
        suffix = " status=" + str(status) if status is not None else ""
        super(ApiError, self).__init__(message + suffix + " path=" + path)

    @property
    def public_message(self):
        # Never include response bodies, URLs with query values, or credentials.
        suffix = " status=" + str(self.status) if self.status is not None else ""
        return self.message + suffix + " path=" + self.path.split("?", 1)[0]


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def redact_sensitive(value):
    text = "" if value is None else str(value)
    text = ADMIN_AUTH_RE.sub("<redacted>", text)
    text = TOKEN_RE.sub("<redacted>", text)
    text = CREDENTIAL_RE.sub(
        lambda match: match.group("key")
        + match.group("separator")
        + (match.group("prefix") or "")
        + "<redacted>",
        text,
    )
    return text


def clean_text(value, max_chars=DEFAULT_MAX_BODY_CHARS):
    text = redact_sensitive(value)
    text = CONTROL_RE.sub(" ", text)
    text = " ".join(text.split())
    return text[:max_chars]


def _field_value(value, max_chars=240):
    value = clean_text(value, max_chars).strip(" *_`\t")
    return value[:max_chars]


FIELD_LABELS = {
    "owner": ["工作端/唯一写入者", "唯一主动写入者", "工作端/唯一负责人", "工作端"],
    "status": ["当前状态", "任务状态", "状态"],
    "task_baseline_sha": ["origin/main 基线完整 SHA", "基线完整 SHA", "基线"],
    "branch": ["分支/PR、最新 HEAD SHA", "工作分支 / PR / 最新 HEAD SHA", "分支/PR", "分支"],
    "approval": ["是否需要维护者审批", "需维护者审批", "待审批动作"],
    "next_owner": ["下一个工作端", "下一负责人", "下一步由谁负责"],
}


def parse_handoff_fields(text, max_chars=DEFAULT_MAX_BODY_CHARS):
    """Extract bounded handoff fields; all other comment text is discarded."""
    limited = str(text or "")[:max_chars]
    fields = {}
    for key, labels in FIELD_LABELS.items():
        labels = sorted(labels, key=len, reverse=True)
        expression = r"^\s*(?:" + "|".join(re.escape(label) for label in labels) + r")\s*[:：]\s*(.*?)\s*$"
        matches = list(re.finditer(expression, limited, re.MULTILINE))
        if not matches:
            continue
        match = matches[-1]
        value = _field_value(match.group(1))
        if key == "task_baseline_sha":
            sha = SHA_RE.search(value)
            if sha:
                value = sha.group(0).lower()
            else:
                value = "unknown"
        elif key == "branch":
            sha = SHA_RE.search(value)
            if sha:
                fields["head_sha"] = sha.group(0).lower()
        if value:
            fields[key] = value
    return fields


def _author_login(item):
    user = item.get("user") if isinstance(item, dict) else None
    if isinstance(user, dict):
        return clean_text(user.get("login") or "", 120)
    return ""


def handoff_records(issue, comments, authorized_users=()):
    """Parse each issue/comment independently and return chronological records.

    GitHub text is untrusted.  A record is marked verified only when its author
    is explicitly listed in ``authorized_users``; callers may retain unverified
    fields as bounded audit claims, but must not treat them as effective state.
    """
    authorized = set(clean_text(user, 120) for user in authorized_users if clean_text(user, 120))
    records = []
    sources = [(issue, "issue", 0)]
    sources.extend((comment, "comment", index + 1) for index, comment in enumerate(comments or []))
    for item, source, order in sources:
        if not isinstance(item, dict):
            continue
        fields = parse_handoff_fields(item.get("body") or "")
        if not fields:
            continue
        author = _author_login(item)
        created_at = clean_text(item.get("created_at") or "", 40)
        records.append(
            {
                "fields": fields,
                "author": author or "unknown",
                "created_at": created_at,
                "source": source,
                "verified": bool(author and author in authorized),
                "order": order,
            }
        )
    def record_order(item):
        if item.get("source") == "issue":
            return (0, "", 0)
        created_at = item.get("created_at") or ""
        if created_at:
            return (1, created_at, item.get("order", 0))
        # GitHub normally supplies created_at.  If it is absent, keep the
        # API's bounded page order after timestamped records instead of
        # allowing an issue body to be overwritten by an unknown-order item.
        return (2, "", item.get("order", 0))

    records.sort(key=record_order)
    return records


def latest_client_handoff_events(comments, task_refs):
    """Return the latest unique validated event for each monitored task.

    These events are untrusted, self-reported metadata.  In particular, they
    are deliberately kept separate from the verified handoff fields used to
    determine effective ownership and status.  Each comment must carry the
    monitor-assigned ``source_issue_ref`` so a comment cannot report another
    monitored issue's state.
    """
    monitored = set(str(task_ref) for task_ref in task_refs)
    seen_event_ids = set()
    events = {}
    ordered_comments = []
    for order, comment in enumerate(comments or []):
        if not isinstance(comment, dict):
            continue
        source_issue_ref = clean_text(comment.get("source_issue_ref") or "", 240)
        if not source_issue_ref:
            continue
        created_at = clean_text(comment.get("created_at") or "", 40)
        sort_key = (0, created_at, order) if created_at else (1, "", order)
        ordered_comments.append((sort_key, source_issue_ref, comment))

    for _sort_key, source_issue_ref, comment in sorted(ordered_comments, key=lambda item: item[0]):
        body = str(comment.get("body") or "")[:DEFAULT_MAX_BODY_CHARS]
        for match in list(HANDOFF_EVENT_BLOCK_RE.finditer(body))[:MAX_HANDOFF_EVENT_BLOCKS]:
            try:
                raw_event = json.loads(match.group(1).strip())
                event = validate_event(raw_event)
            except (TypeError, ValueError, HandoffValidationError):
                continue
            task_ref = event["task_id"]
            event_id = event["event_id"]
            if task_ref != source_issue_ref or task_ref not in monitored or event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
            events[task_ref] = event
    return events


def _comments_with_source(comments, source_issue_ref):
    """Attach a bounded, monitor-owned source ref without retaining new text."""
    tagged = []
    for comment in comments or []:
        if not isinstance(comment, dict):
            continue
        item = dict(comment)
        item["source_issue_ref"] = source_issue_ref
        tagged.append(item)
    return tagged


def linked_references(text, repository):
    """Return issue/PR numbers linked to this repository only."""
    base = re.escape("https://github.com/" + repository)
    pattern = re.compile(base + r"/(issues|pull)/(\d+)")
    issues = set()
    pulls = set()
    for kind, number in pattern.findall(str(text or "")):
        (issues if kind == "issues" else pulls).add(int(number))
    return issues, pulls


def parse_standard_task_target(body, repository, task_label, labels=()):
    """Return a label-gated target claim without retaining untrusted body text."""
    label = clean_text(task_label or "", 100).strip().lower()
    label_names = {
        clean_text(item.get("name") if isinstance(item, dict) else item, 100).strip().lower()
        for item in (labels or [])
    }
    if not label or label not in label_names:
        return "", False
    matches = list(STANDARD_TASK_BLOCK_RE.finditer(str(body or "")[:12000]))
    if len(matches) != 1:
        return "", False
    try:
        value = json.loads(matches[0].group(1).strip())
    except (TypeError, ValueError):
        return "", False
    if not isinstance(value, dict):
        return "", False
    target = clean_text(value.get("target") or "", 80).strip().lower()
    claimed_repository = clean_text(value.get("repository") or "", 240).strip()
    if target not in ("server", "computer", "mobile") or claimed_repository != repository:
        return "", False
    return target, True


REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TOKEN_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_repository_name(repository):
    repository = str(repository or "").strip()
    if not REPOSITORY_RE.fullmatch(repository):
        raise CoordinationError("repository must be an explicit owner/name: " + clean_text(repository, 120))
    return repository


def validate_token_env(token_env):
    token_env = str(token_env or "").strip()
    if not TOKEN_ENV_RE.fullmatch(token_env):
        raise CoordinationError("token_env must be a non-empty environment variable name")
    return token_env


def validate_api_base(api_base, allowed_api_hosts=DEFAULT_ALLOWED_API_HOSTS):
    parsed = urllib.parse.urlparse(str(api_base or ""))
    allowed = set(str(host).lower().strip() for host in (allowed_api_hosts or ()) if str(host).strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in allowed:
        raise CoordinationError(
            "api_base host is not explicitly allowed; use HTTPS and allowed_api_hosts"
        )
    return str(api_base).rstrip("/")


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Block redirects that could disclose an Authorization header elsewhere."""

    def redirect_request(self, request, file_handle, code, message, headers, newurl):
        old = urllib.parse.urlparse(request.full_url)
        new = urllib.parse.urlparse(newurl)
        if old.scheme != new.scheme or old.hostname != new.hostname:
            raise urllib.error.URLError("cross-host or cross-scheme redirect blocked")
        return super(_SameHostRedirectHandler, self).redirect_request(
            request, file_handle, code, message, headers, newurl
        )


class GithubClient(object):
    """Small GET-only GitHub API client with bounded retry behavior."""

    def __init__(
        self,
        repository,
        token=None,
        api_base=DEFAULT_API_BASE,
        allowed_api_hosts=DEFAULT_ALLOWED_API_HOSTS,
        timeout=DEFAULT_TIMEOUT,
        max_retries=DEFAULT_RETRIES,
        opener=None,
        sleep=None,
        max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
    ):
        self.repository = validate_repository_name(repository)
        self.token = token
        self.api_base = validate_api_base(api_base, allowed_api_hosts)
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        if opener is None:
            self.opener = urllib.request.build_opener(_SameHostRedirectHandler()).open
        else:
            self.opener = opener
        self.sleep = sleep or time.sleep
        self.max_response_bytes = int(max_response_bytes)

    def get(self, path, params=None):
        if not path.startswith("/"):
            path = "/" + path
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = self.api_base + path + ("?" + query if query else "")
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Collaboration-Hub-Coordination/1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(url, headers=headers, method="GET")

        for attempt in range(self.max_retries + 1):
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    data = response.read(self.max_response_bytes + 1)
                if len(data) > self.max_response_bytes:
                    raise ApiError(path, message="GitHub response exceeds size limit")
                try:
                    return json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise ApiError(path, message="GitHub response is not valid JSON")
            except urllib.error.HTTPError as error:
                status = int(getattr(error, "code", 0) or 0)
                if status not in (403, 429, 500, 502, 503, 504) or attempt >= self.max_retries:
                    raise ApiError(path, status=status)
                self.sleep(self._retry_delay(error, attempt))
            except urllib.error.URLError:
                if attempt >= self.max_retries:
                    raise ApiError(path, message="GitHub network request failed")
                self.sleep(min(DEFAULT_BACKOFF * (2 ** attempt), MAX_BACKOFF))

    @staticmethod
    def _retry_delay(error, attempt):
        headers = getattr(error, "headers", None)
        retry_after = headers.get("Retry-After") if headers else None
        try:
            if retry_after is not None:
                return min(max(0.0, float(retry_after)), MAX_BACKOFF)
        except (TypeError, ValueError):
            pass
        return min(DEFAULT_BACKOFF * (2 ** attempt), MAX_BACKOFF)

    def pages(self, path, params=None, max_pages=DEFAULT_MAX_PAGES):
        base_params = dict(params or {})
        base_params.setdefault("per_page", 100)
        for page in range(1, max(1, int(max_pages)) + 1):
            request_params = dict(base_params)
            request_params["page"] = page
            payload = self.get(path, request_params)
            if not isinstance(payload, list):
                raise ApiError(path, message="GitHub pagination response is not a list")
            for item in payload:
                yield item
            if len(payload) < int(base_params["per_page"]):
                break


class StateStore(object):
    """Atomic local state used to suppress duplicate reports across restarts."""

    def __init__(self, path):
        self.path = Path(path).expanduser()

    def _load(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _stable_report(report):
        return {key: value for key, value in report.items() if key not in ("generated_at", "change_detected")}

    @staticmethod
    def _digest(report):
        stable = StateStore._stable_report(report)
        return hashlib.sha256(
            json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def record(self, report):
        digest = self._digest(report)
        previous = self._load()
        changed = previous.get("last_report_digest") != digest
        state = {
            "version": 1,
            "last_report_digest": digest,
            "last_generated_at": report.get("generated_at", utc_now()),
            "event_keys": list(report.get("event_keys", []))[:500],
        }
        self._write(state)
        return changed

    def record_collection(self, collection):
        """Persist isolated per-repository digests for a multi-repo report."""
        previous = self._load()
        old_repositories = previous.get("repositories", {}) if isinstance(previous, dict) else {}
        repositories = {}
        changed = False
        for report in collection.get("repositories", []):
            repository = validate_repository_name(report.get("repository"))
            digest = self._digest(report)
            prior = old_repositories.get(repository, {})
            if prior.get("last_report_digest") != digest:
                changed = True
            repositories[repository] = {
                "last_report_digest": digest,
                "last_generated_at": report.get("generated_at", utc_now()),
                "event_keys": list(report.get("event_keys", []))[:500],
            }
        if set(old_repositories) != set(repositories):
            changed = True
        state = {
            "version": 2,
            "last_generated_at": collection.get("generated_at", utc_now()),
            "repositories": repositories,
            "event_keys": list(collection.get("event_keys", []))[:1000],
        }
        self._write(state)
        return changed

    def _write(self, data):
        parent = self.path.parent
        if not parent.exists():
            parent.mkdir(parents=True, mode=0o700)
        try:
            os.chmod(str(parent), 0o700)
        except OSError:
            pass
        fd, temp_name = tempfile.mkstemp(prefix=".coordination-", dir=str(parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, str(self.path))
        finally:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


class Monitor(object):
    def __init__(
        self,
        api,
        repository,
        coordination_issue=9,
        task_issue_numbers=(),
        task_label="",
        max_pages=DEFAULT_MAX_PAGES,
        main_branch="main",
        authorized_handoff_users=(),
    ):
        self.api = api
        self.repository = repository
        self.coordination_issue = int(coordination_issue)
        self.task_issue_numbers = tuple(sorted(set(int(number) for number in task_issue_numbers)))
        self.task_label = clean_text(task_label or "", 100).strip()
        self.max_pages = max(1, int(max_pages))
        self.main_branch = clean_text(main_branch or "main", 120) or "main"
        self.authorized_handoff_users = tuple(
            sorted(set(clean_text(user, 120) for user in authorized_handoff_users if clean_text(user, 120)))
        )
        self.warnings = []

    def _path(self, suffix):
        return "/repos/" + self.repository + suffix

    def _pages(self, path, params=None):
        if hasattr(self.api, "pages"):
            return list(self.api.pages(path, params or {}, self.max_pages))
        # The fallback keeps the public Monitor seam easy to exercise with a fixture.
        rows = []
        base = dict(params or {})
        base.setdefault("per_page", 100)
        for page in range(1, self.max_pages + 1):
            query = dict(base)
            query["page"] = page
            payload = self.api.get(path, query)
            if not isinstance(payload, list):
                raise ApiError(path, message="GitHub pagination response is not a list")
            rows.extend(payload)
            if len(payload) < int(base["per_page"]):
                break
        return rows

    def _get(self, path):
        try:
            return self.api.get(path)
        except ApiError as error:
            self.warnings.append(error.public_message)
            return None

    def _comments(self, number):
        try:
            return self._pages(self._path("/issues/%d/comments" % number))
        except ApiError as error:
            self.warnings.append(error.public_message)
            return []

    def _issue_bundle(self, number):
        issue = self._get(self._path("/issues/%d" % number))
        if not isinstance(issue, dict):
            return None, []
        comments = self._comments(number)
        return issue, comments

    def _discover_task_numbers(self, coordination_text):
        tasks = set(self.task_issue_numbers)
        linked_issues, _ = linked_references(coordination_text, self.repository)
        tasks.update(linked_issues)
        try:
            for issue in self._pages(self._path("/issues"), {"state": "open", "sort": "updated", "direction": "desc"}):
                if not isinstance(issue, dict) or issue.get("pull_request"):
                    continue
                labels = issue.get("labels") if isinstance(issue.get("labels"), list) else []
                label_names = {
                    clean_text(item.get("name") if isinstance(item, dict) else item, 100).lower()
                    for item in labels
                }
                if self.task_label and self.task_label.lower() in label_names:
                    try:
                        tasks.add(int(issue.get("number")))
                    except (TypeError, ValueError):
                        self.warnings.append("ignored labeled issue with invalid number")
                    continue
                body = str(issue.get("body") or "")
                issue_links, _ = linked_references(body, self.repository)
                if self.coordination_issue in issue_links:
                    tasks.add(int(issue.get("number")))
        except ApiError as error:
            self.warnings.append(error.public_message)
        tasks.discard(self.coordination_issue)
        return sorted(tasks)

    def _discover_pull_numbers(self, issue_text, issue_number, all_pulls=None):
        _, linked_pulls = linked_references(issue_text, self.repository)
        if all_pulls is not None:
            issue_url = "https://github.com/%s/issues/%d" % (self.repository, issue_number)
            for pull in all_pulls:
                if not isinstance(pull, dict):
                    continue
                body = str(pull.get("body") or "")
                if issue_url in body:
                    linked_pulls.add(int(pull.get("number")))
        return sorted(linked_pulls)

    def _list_pulls(self):
        try:
            return self._pages(self._path("/pulls"), {"state": "all", "sort": "updated", "direction": "desc"})
        except ApiError as error:
            self.warnings.append(error.public_message)
            return []

    def _current_main_sha(self):
        branch = urllib.parse.quote(self.main_branch, safe="")
        payload = self._get(self._path("/branches/" + branch))
        commit = payload.get("commit") if isinstance(payload, dict) else None
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if SHA_RE.fullmatch(str(sha or "")):
            return str(sha).lower()
        self.warnings.append("GitHub main branch SHA is unavailable")
        return "unknown"

    def _ci_summary(self, sha):
        if not SHA_RE.fullmatch(str(sha or "")):
            return {"sha": "unknown", "state": "unknown", "sources": []}
        statuses = []
        runs = []
        try:
            payload = self.api.get(self._path("/commits/%s/status" % sha))
            statuses = payload.get("statuses", []) if isinstance(payload, dict) else []
        except ApiError as error:
            self.warnings.append(error.public_message)
        try:
            payload = self.api.get(self._path("/actions/runs"), {"head_sha": sha, "per_page": 100})
            runs = payload.get("workflow_runs", []) if isinstance(payload, dict) else []
        except ApiError as error:
            self.warnings.append(error.public_message)

        states = []
        for item in statuses:
            if isinstance(item, dict):
                states.append(str(item.get("state") or "unknown").lower())
        for item in runs:
            if isinstance(item, dict):
                conclusion = item.get("conclusion")
                status = item.get("status")
                states.append(str(conclusion or status or "unknown").lower())
        if any(state in ("failure", "failed", "error", "cancelled", "timed_out") for state in states):
            state = "failure"
        elif any(state in ("pending", "queued", "in_progress", "requested") for state in states):
            state = "pending"
        elif states and all(state in ("success", "successful", "completed") for state in states):
            state = "success"
        else:
            state = "unknown"
        return {
            "sha": str(sha).lower(),
            "state": state,
            "status_count": len(statuses),
            "workflow_runs": [
                {
                    "name": clean_text(item.get("name") or "unknown", 120),
                    "status": clean_text(item.get("status") or "unknown", 40),
                    "conclusion": clean_text(item.get("conclusion") or "unknown", 40),
                }
                for item in runs[:50]
                if isinstance(item, dict)
            ],
        }

    def _pull_summary(self, number):
        pull = self._get(self._path("/pulls/%d" % number))
        if not isinstance(pull, dict):
            return {
                "repository": self.repository,
                "pull_ref": "%s#%d" % (self.repository, number),
                "number": number,
                "state": "unknown",
                "head_sha": "unknown",
                "ci": {"state": "unknown"},
            }
        reviews = []
        try:
            reviews = self._pages(self._path("/pulls/%d/reviews" % number))
        except ApiError as error:
            self.warnings.append(error.public_message)
        head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
        base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
        head_sha = str(head.get("sha") or pull.get("head_sha") or "")
        return {
            "repository": self.repository,
            "pull_ref": "%s#%d" % (self.repository, number),
            "number": number,
            "title": clean_text(pull.get("title") or "unknown", 240),
            "state": clean_text(pull.get("state") or "unknown", 40),
            "draft": bool(pull.get("draft")),
            "merged": bool(pull.get("merged")),
            "branch": clean_text(head.get("ref") or pull.get("head_ref") or "unknown", 160),
            "base": clean_text(base.get("ref") or pull.get("base_ref") or "unknown", 80),
            "head_sha": head_sha.lower() if SHA_RE.fullmatch(head_sha) else "unknown",
            "updated_at": clean_text(pull.get("updated_at") or "unknown", 40),
            "reviews": [
                {
                    "state": clean_text(review.get("state") or "unknown", 40),
                    "submitted_at": clean_text(review.get("submitted_at") or "unknown", 40),
                }
                for review in reviews[-20:]
                if isinstance(review, dict)
            ],
            "ci": self._ci_summary(head_sha),
        }

    def _issue_summary(self, issue, comments, all_pulls):
        number = int(issue.get("number"))
        records = handoff_records(issue, comments, self.authorized_handoff_users)
        verified_fields = {}
        verified_field_records = {}
        unverified_claims = {}
        for record in records:
            for key, value in record.get("fields", {}).items():
                if record.get("verified"):
                    verified_fields[key] = value
                    verified_field_records[key] = record
                else:
                    # Keep the latest untrusted claim as audit metadata, but
                    # never allow it to become an effective task field.
                    unverified_claims[key] = value
        text = str(issue.get("body") or "")
        text += "\n" + "\n".join(
            str(comment.get("body") or "")
            for comment in comments
            if isinstance(comment, dict)
        )
        pull_numbers = self._discover_pull_numbers(text, number, all_pulls)
        status = verified_fields.get("status")
        if not status:
            status = "已关闭" if issue.get("state") == "closed" else "unknown"
        latest = records[-1] if records else {}
        unverified_fields = sorted(unverified_claims)
        latest_verified = bool(verified_field_records) and not unverified_claims
        approval = verified_fields.get("approval", "unknown")
        standard_target, standard_target_verified = parse_standard_task_target(
            issue.get("body") or "", self.repository, self.task_label, issue.get("labels", [])
        )
        return {
            "repository": self.repository,
            "issue_ref": "%s#%d" % (self.repository, number),
            "issue": number,
            "title": clean_text(issue.get("title") or "unknown", 240),
            "state": clean_text(issue.get("state") or "unknown", 40),
            "owner": verified_fields.get("owner", "unknown"),
            "status": status,
            "task_baseline_sha": verified_fields.get("task_baseline_sha", "unknown"),
            "branch": verified_fields.get("branch", "unknown"),
            "head_sha": verified_fields.get("head_sha", "unknown"),
            "approval": approval,
            "next_owner": verified_fields.get("next_owner", "unknown"),
            "target": standard_target or "unknown",
            "target_verified": standard_target_verified,
            "target_source": "standard_task" if standard_target_verified else "unknown",
            "handoff_verified": latest_verified,
            "unverified_fields": unverified_fields,
            "unverified_claims": unverified_claims,
            "status_verified": "status" in verified_fields,
            "owner_verified": "owner" in verified_fields,
            "approval_verified": "approval" in verified_fields,
            "handoff_author": latest.get("author", "unknown"),
            "handoff_updated_at": latest.get("created_at", "unknown"),
            "pull_requests": pull_numbers,
            "updated_at": clean_text(issue.get("updated_at") or "unknown", 40),
        }

    def collect(self):
        self.warnings = []
        coordination, coordination_comments = self._issue_bundle(self.coordination_issue)
        if not isinstance(coordination, dict):
            detail = self.warnings[-1] if self.warnings else "no response"
            raise CoordinationError(
                "coordination Issue #%d could not be read: %s" % (self.coordination_issue, detail)
            )
        coordination_text = str(coordination.get("body") or "")
        coordination_text += "\n" + "\n".join(
            str(comment.get("body") or "")
            for comment in coordination_comments
            if isinstance(comment, dict)
        )
        task_numbers = self._discover_task_numbers(coordination_text)
        all_pulls = self._list_pulls()
        coordination_pull_numbers = self._discover_pull_numbers(coordination_text, self.coordination_issue, all_pulls)
        current_main_sha = self._current_main_sha()

        tasks = []
        coordination_ref = "%s#%d" % (self.repository, self.coordination_issue)
        collected_comments = _comments_with_source(coordination_comments, coordination_ref)
        pull_numbers = set(coordination_pull_numbers)
        for number in task_numbers:
            issue, comments = self._issue_bundle(number)
            if issue is None:
                continue
            summary = self._issue_summary(issue, comments, all_pulls)
            tasks.append(summary)
            collected_comments.extend(_comments_with_source(comments, summary["issue_ref"]))
            pull_numbers.update(summary["pull_requests"])

        client_handoffs = latest_client_handoff_events(
            collected_comments, (task["issue_ref"] for task in tasks)
        )
        for task in tasks:
            event = client_handoffs.get(task["issue_ref"])
            if event is not None:
                task["client_handoff_event"] = event

        pull_summaries = [self._pull_summary(number) for number in sorted(pull_numbers)]
        event_keys = [
            "issue:%d:%s" % (self.coordination_issue, coordination.get("updated_at") or "unknown"),
            "main:%s:%s" % (self.main_branch, current_main_sha),
        ]
        event_keys.extend("issue:%d:%s" % (task["issue"], task["updated_at"]) for task in tasks)
        event_keys.extend("pull:%d:%s" % (pull["number"], pull["head_sha"]) for pull in pull_summaries)
        return {
            "schema_version": 2,
            "generated_at": utc_now(),
            "repository": self.repository,
            "coordination_ref": "%s#%d" % (self.repository, self.coordination_issue),
            "coordination_issue": self.coordination_issue,
            "main_branch": self.main_branch,
            "current_main_sha": current_main_sha,
            "coordination_state": clean_text(coordination.get("state") or "unknown", 40),
            "tasks": sorted(tasks, key=lambda item: item["issue"]),
            "pull_requests": pull_summaries,
            "event_keys": event_keys,
            "warnings": sorted(set(self.warnings)),
        }



class MultiRepositoryMonitor(object):
    """Collect explicit repository monitors without sharing their data."""

    def __init__(self, monitors):
        self.monitors = tuple(monitors)

    def collect(self):
        reports = []
        warnings = []
        event_keys = []
        for monitor in self.monitors:
            try:
                report = monitor.collect()
            except CoordinationError as error:
                report = {
                    "schema_version": 2,
                    "generated_at": utc_now(),
                    "repository": monitor.repository,
                    "coordination_ref": "%s#%d" % (monitor.repository, monitor.coordination_issue),
                    "coordination_issue": monitor.coordination_issue,
                    "main_branch": monitor.main_branch,
                    "current_main_sha": "unknown",
                    "coordination_state": "unknown",
                    "tasks": [],
                    "pull_requests": [],
                    "event_keys": [],
                    "warnings": [clean_text(error, 500)],
                }
            repository = report.get("repository", monitor.repository)
            namespaced_keys = [
                "%s:%s" % (repository, key) for key in report.get("event_keys", [])
            ]
            report["event_keys"] = namespaced_keys
            reports.append(report)
            event_keys.extend(namespaced_keys)
            warnings.extend(
                "%s: %s" % (repository, warning)
                for warning in report.get("warnings", [])
            )
        return {
            "schema_version": 3,
            "generated_at": utc_now(),
            "repository_count": len(reports),
            "repositories": reports,
            "event_keys": event_keys[:1000],
            "warnings": sorted(set(warnings)),
        }


def normalize_repository_specs(values):
    """Normalize the multi-repository schema and retain the old single schema."""
    values = dict(values or {})
    configured = values.get("repositories")
    new_schema = configured is not None
    common_keys = {
        "api_base",
        "allowed_api_hosts",
        "timeout_seconds",
        "max_retries",
        "max_pages",
        "poll_interval_seconds",
    }
    common = {key: values[key] for key in common_keys if key in values}
    if configured is None:
        if not values.get("repository"):
            raise CoordinationError("config must define repositories or repository")
        configured = [values]
    if not isinstance(configured, list) or not configured:
        raise CoordinationError("repositories must be a non-empty list")
    specs = []
    seen = set()
    token_envs = {}
    for item in configured:
        if isinstance(item, str):
            item = {"repository": item}
        if not isinstance(item, dict):
            raise CoordinationError("each repositories entry must be an object")
        spec = dict(common)
        spec.update(item)
        repository = validate_repository_name(spec.get("repository"))
        if repository in seen:
            raise CoordinationError("duplicate repository configuration: " + repository)
        seen.add(repository)
        if new_schema:
            if "token_env" not in item:
                raise CoordinationError(
                    "each repositories entry must explicitly define token_env"
                )
            spec["token_env"] = validate_token_env(item.get("token_env"))
            previous_repository = token_envs.get(spec["token_env"])
            if previous_repository and not values.get("allow_shared_token_env", False):
                raise CoordinationError(
                    "token_env is shared by repositories; explicitly set allow_shared_token_env: true: "
                    + spec["token_env"]
                )
            token_envs[spec["token_env"]] = repository
        else:
            spec["token_env"] = validate_token_env(spec.get("token_env", "GH_TOKEN"))
        spec["repository"] = repository
        spec["coordination_issue"] = int(spec.get("coordination_issue", 9))
        spec["task_issue_numbers"] = spec.get("task_issue_numbers", [])
        spec["task_label"] = clean_text(spec.get("task_label") or "", 100).strip()
        spec["main_branch"] = spec.get("main_branch", "main")
        spec["authorized_handoff_users"] = spec.get("authorized_handoff_users", [])
        specs.append(spec)
    return specs


def write_report(path, report):
    path = Path(path).expanduser()
    parent = path.parent
    if not parent.exists():
        parent.mkdir(parents=True, mode=0o700)
    try:
        os.chmod(str(parent), 0o700)
    except OSError:
        pass
    fd, temp_name = tempfile.mkstemp(prefix=".coordination-report-", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, str(path))
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def load_config(path):
    if not path:
        return {}
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CoordinationError("cannot read config: " + str(error))
    if not isinstance(data, dict):
        raise CoordinationError("config must be a JSON object")
    return data


def build_monitor(values):
    """Build one or more monitors from an already loaded configuration."""
    specs = normalize_repository_specs(values)
    monitors = []
    for spec in specs:
        token_env = spec.get("token_env", "GH_TOKEN")
        token = os.environ.get(token_env)
        api = GithubClient(
            spec["repository"],
            token=token,
            api_base=spec.get("api_base", DEFAULT_API_BASE),
            allowed_api_hosts=spec.get("allowed_api_hosts", DEFAULT_ALLOWED_API_HOSTS),
            timeout=float(spec.get("timeout_seconds", DEFAULT_TIMEOUT)),
            max_retries=int(spec.get("max_retries", DEFAULT_RETRIES)),
        )
        monitors.append(
            Monitor(
                api,
                spec["repository"],
                coordination_issue=spec.get("coordination_issue", 9),
                task_issue_numbers=spec.get("task_issue_numbers", []),
                task_label=spec.get("task_label", ""),
                max_pages=int(spec.get("max_pages", DEFAULT_MAX_PAGES)),
                main_branch=spec.get("main_branch", "main"),
                authorized_handoff_users=spec.get("authorized_handoff_users", []),
            )
        )
    return (MultiRepositoryMonitor(monitors) if len(monitors) > 1 else monitors[0]), specs


def text_report(report):
    if "repositories" in report:
        lines = [
            "repositories: " + str(report.get("repository_count", len(report.get("repositories", [])))),
            "generated_at: " + report.get("generated_at", "unknown"),
        ]
        for repository_report in report.get("repositories", []):
            lines.append(
                "repository {repository}; main={current_main_sha}; coordination={coordination_ref}".format(
                    **repository_report
                )
            )
            for task in repository_report.get("tasks", []):
                lines.append(
                    "  task {issue_ref}: {status}; owner={owner}; baseline={task_baseline_sha}; "
                    "status_verified={status_verified}; owner_verified={owner_verified}; "
                    "approval={approval}; approval_verified={approval_verified}; "
                    "branch={branch}; prs={pull_requests}".format(**task)
                )
                if task.get("unverified_claims"):
                    lines.append("    unverified_claims: " + json.dumps(task["unverified_claims"], ensure_ascii=False, sort_keys=True))
            for warning in repository_report.get("warnings", []):
                lines.append("  warning: " + warning)
        for warning in report.get("warnings", []):
            lines.append("warning: " + warning)
        return "\n".join(lines)
    lines = [
        "repository: " + report.get("repository", "unknown"),
        "generated_at: " + report.get("generated_at", "unknown"),
        "coordination_issue: #" + str(report.get("coordination_issue", "unknown")),
        "current_main_sha: " + report.get("current_main_sha", "unknown"),
    ]
    for task in report.get("tasks", []):
        lines.append(
            "task #{issue}: {status}; owner={owner}; baseline={task_baseline_sha}; "
            "status_verified={status_verified}; owner_verified={owner_verified}; "
            "approval={approval}; approval_verified={approval_verified}; "
            "branch={branch}; prs={pull_requests}".format(**task)
        )
        if task.get("unverified_claims"):
            lines.append("  unverified_claims: " + json.dumps(task["unverified_claims"], ensure_ascii=False, sort_keys=True))
    for warning in report.get("warnings", []):
        lines.append("warning: " + warning)
    return "\n".join(lines)


def run_once(monitor, state_store, report_path=None):
    report = monitor.collect()
    if "repositories" in report:
        report["change_detected"] = state_store.record_collection(report)
    else:
        report["change_detected"] = state_store.record(report)
    if report_path:
        write_report(report_path, report)
    return report


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="JSON configuration file")
    parser.add_argument("--once", action="store_true", help="collect one report (default)")
    parser.add_argument("--watch", action="store_true", help="poll at the configured interval")
    parser.add_argument("--interval", type=int, help="polling interval in seconds")
    parser.add_argument("--state-file", help="override the local state file")
    parser.add_argument("--report-file", help="write a JSON report with mode 0600")
    parser.add_argument("--text", action="store_true", help="render a concise text report")
    parser.add_argument(
        "--emit-unchanged",
        action="store_true",
        help="in --watch mode, print every unchanged report (default suppresses duplicates)",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.once and args.watch:
        raise SystemExit("choose only one of --once or --watch")
    values = load_config(args.config)
    monitor, specs = build_monitor(values)
    state_path = args.state_file or values.get(
        "state_file", "~/.local/state/collaboration-hub/github-coordination/state.json"
    )
    report_path = args.report_file or values.get("report_file")
    state = StateStore(state_path)
    interval = args.interval or int(values.get("poll_interval_seconds", 300))
    watch = args.watch

    while True:
        try:
            report = run_once(monitor, state, report_path)
            if not watch or report.get("change_detected") or args.emit_unchanged:
                print(text_report(report) if args.text else json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        except (CoordinationError, OSError) as error:
            print("github-coordination error: " + clean_text(error, 500), file=sys.stderr)
            if not watch:
                return 1
        if not watch:
            return 0
        try:
            time.sleep(max(1, interval))
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
