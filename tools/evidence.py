#!/usr/bin/env python3
"""Read-only evidence adapters for the local authorization workflow.

The policy engine deliberately accepts a normalized request, but callers must
not be allowed to self-report the facts that make a request safe.  This module
is the narrow seam that obtains paths, CI and review state from either an
explicitly marked fixture (for the local demo) or GitHub's GET-only API.
Neither adapter writes to GitHub or executes a requested operation.
"""

from __future__ import print_function

import copy
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import authorization as _authorization
except ImportError:  # pragma: no cover - exercised when run from repository root
    _tools = str(Path(__file__).resolve().parent)
    if _tools not in sys.path:
        sys.path.insert(0, _tools)
    import authorization as _authorization


class EvidenceError(ValueError):
    """Expected failure while collecting trusted facts."""
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_text(value, maximum=240):
    return " ".join(str(value or "").replace("\x00", " ").split())[:maximum]


def _repository(value):
    try:
        return _authorization._require_repository(value)
    except Exception as error:
        raise EvidenceError(str(error))


def _path(value):
    try:
        return _authorization._normalize_path(value)
    except Exception as error:
        raise EvidenceError(str(error))


def _paths(values, allow_empty=False):
    if not isinstance(values, list):
        raise EvidenceError("evidence paths must be a list")
    result = []
    for value in values:
        normalized = _path(value)
        if normalized not in result:
            result.append(normalized)
    if not allow_empty and not result:
        raise EvidenceError("evidence paths must not be empty")
    return result


def _sha(value):
    value = _safe_text(value, 80).lower()
    if not SHA_RE.fullmatch(value):
        raise EvidenceError("head_sha must be a 40-character SHA")
    return value


def _pull_number(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise EvidenceError("pull_number must be a positive integer")
    if number <= 0 or number > 100000000:
        raise EvidenceError("pull_number must be a positive bounded integer")
    return number


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _request_target(request):
    if not isinstance(request, dict):
        raise EvidenceError("request must be an object")
    repository = _repository(request.get("repository"))
    head_sha = _sha(request.get("head_sha"))
    pull_number = _pull_number(request.get("pull_number"))
    return repository, pull_number, head_sha


def _snapshot(repository, pull_number, head_sha, paths, ci_passed, independent_review,
              ci_state, review_state, source, provenance):
    paths = _paths(paths)
    facts = {
        "repository": repository,
        "pull_number": pull_number,
        "head_sha": head_sha,
        "paths": paths,
        "ci_passed": bool(ci_passed),
        "independent_review": bool(independent_review),
        "ci_state": _safe_text(ci_state, 40).lower() or "unknown",
        "review_state": _safe_text(review_state, 40).lower() or "unknown",
        "source": _safe_text(source, 80),
        "provenance": _safe_text(provenance, 80),
    }
    facts["evidence_digest"] = _digest(facts)
    facts["path_evidence"] = {
        "source": facts["source"],
        "complete": True,
        "paths": list(paths),
    }
    facts["observed_at"] = _utc_now()
    return facts


class EvidenceAdapter(object):
    """Interface implemented by a trusted fact collector."""

    def collect(self, request):
        raise NotImplementedError

    def enrich(self, request):
        """Replace caller-provided claims with adapter-produced facts."""
        repository, pull_number, head_sha = _request_target(request)
        snapshot = self.collect(request)
        if not isinstance(snapshot, dict):
            raise EvidenceError("evidence adapter returned an invalid snapshot")
        if (
            snapshot.get("repository") != repository
            or int(snapshot.get("pull_number", 0)) != pull_number
            or str(snapshot.get("head_sha", "")).lower() != head_sha
        ):
            raise EvidenceError("evidence target does not match request")
        enriched = copy.deepcopy(request)
        enriched["paths"] = list(snapshot["paths"])
        enriched["path_evidence"] = copy.deepcopy(snapshot["path_evidence"])
        enriched["ci_passed"] = bool(snapshot["ci_passed"])
        enriched["independent_review"] = bool(snapshot["independent_review"])
        enriched["pull_number"] = pull_number
        enriched["evidence_digest"] = snapshot["evidence_digest"]
        enriched["evidence_provenance"] = snapshot["provenance"]
        enriched["evidence_observed_at"] = snapshot["observed_at"]
        return enriched, snapshot


class FixtureEvidenceAdapter(EvidenceAdapter):
    """Explicit simulation adapter; fixtures are never inferred from requests."""

    def __init__(self, fixtures, repository=None):
        if not isinstance(fixtures, dict):
            raise EvidenceError("fixtures must be an object")
        self.fixtures = fixtures
        self.repository = _repository(repository) if repository else None

    def _fixture(self, repository, pull_number, head_sha):
        keys = [
            (repository, pull_number, head_sha),
            repository + "#" + str(pull_number) + "@" + head_sha,
            repository + "#" + str(pull_number),
            str(pull_number),
        ]
        for key in keys:
            try:
                candidate = self.fixtures.get(key)
            except TypeError:
                candidate = None
            if isinstance(candidate, dict):
                return candidate
        raise EvidenceError("no simulation evidence fixture for target")

    def collect(self, request):
        repository, pull_number, head_sha = _request_target(request)
        if self.repository and repository != self.repository:
            raise EvidenceError("fixture repository is out of scope")
        fixture = self._fixture(repository, pull_number, head_sha)
        # These values are read from the fixture only.  A request's paths,
        # ci_passed and review claims are intentionally ignored.
        paths = fixture.get("paths")
        ci_passed = fixture.get("ci_passed")
        independent_review = fixture.get("independent_review")
        if not isinstance(ci_passed, bool) or not isinstance(independent_review, bool):
            raise EvidenceError("fixture CI and review facts must be boolean")
        return _snapshot(
            repository,
            pull_number,
            head_sha,
            paths,
            ci_passed,
            independent_review,
            fixture.get("ci_state", "success" if ci_passed else "failure"),
            fixture.get("review_state", "approved" if independent_review else "unknown"),
            "trusted_diff",
            "simulation_fixture",
        )


class GithubEvidenceAdapter(EvidenceAdapter):
    """Read-only adapter backed by GitHub PR Files, checks and reviews."""

    def __init__(self, api, repository, max_pages=3, per_page=100):
        self.api = api
        self.repository = _repository(repository)
        self.max_pages = max(1, min(int(max_pages), 20))
        self.per_page = max(1, min(int(per_page), 100))

    def _path(self, suffix):
        return "/repos/" + self.repository + suffix

    def _get(self, path, params=None):
        try:
            return self.api.get(path, params)
        except EvidenceError:
            raise
        except Exception as error:
            raise EvidenceError("GitHub evidence read failed: " + _safe_text(error, 240))

    def _pages(self, path):
        rows = []
        for page in range(1, self.max_pages + 1):
            payload = self._get(
                path, {"per_page": self.per_page, "page": page}
            )
            if not isinstance(payload, list):
                raise EvidenceError("GitHub pagination response is not a list")
            rows.extend(payload)
            if len(payload) < self.per_page:
                return rows
        raise EvidenceError("GitHub evidence pagination is incomplete")

    @staticmethod
    def _ci(payload, runs_payload):
        states = []
        if isinstance(payload, dict):
            top = payload.get("state")
            if top:
                states.append(str(top).lower())
            for item in payload.get("statuses", []) or []:
                if isinstance(item, dict):
                    states.append(str(item.get("state") or "unknown").lower())
        runs = runs_payload.get("workflow_runs", []) if isinstance(runs_payload, dict) else []
        for item in runs:
            if isinstance(item, dict):
                states.append(str(item.get("conclusion") or item.get("status") or "unknown").lower())
        if not states:
            return False, "unknown"
        if any(state in ("failure", "failed", "error", "cancelled", "timed_out") for state in states):
            return False, "failure"
        if any(state in ("pending", "queued", "in_progress", "requested", "waiting") for state in states):
            return False, "pending"
        if all(state in ("success", "successful", "completed") for state in states):
            return True, "success"
        return False, "unknown"

    @staticmethod
    def _reviews(reviews):
        states = []
        for review in reviews:
            if isinstance(review, dict):
                states.append(str(review.get("state") or "unknown").upper())
        if not states:
            return False, "unknown"
        if "CHANGES_REQUESTED" in states:
            return False, "changes_requested"
        if "APPROVED" in states:
            return True, "approved"
        return False, "pending"

    def collect(self, request):
        repository, pull_number, head_sha = _request_target(request)
        if repository != self.repository:
            raise EvidenceError("GitHub evidence repository is out of scope")
        pull_path = self._path("/pulls/%d" % pull_number)
        pull = self._get(pull_path)
        if not isinstance(pull, dict):
            raise EvidenceError("GitHub PR metadata is not an object")
        head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
        observed_sha = str(head.get("sha") or pull.get("head_sha") or "").lower()
        if not SHA_RE.fullmatch(observed_sha) or observed_sha != head_sha:
            raise EvidenceError("GitHub PR HEAD does not match request")
        requested_branch = _safe_text(request.get("branch"), 160).strip()
        observed_branch = _safe_text(head.get("ref"), 160).strip()
        if requested_branch and observed_branch and requested_branch != observed_branch:
            raise EvidenceError("GitHub PR branch does not match request")

        files = self._pages(self._path("/pulls/%d/files" % pull_number))
        paths = []
        for item in files:
            if not isinstance(item, dict):
                raise EvidenceError("GitHub PR file record is invalid")
            filename = item.get("filename")
            if not filename:
                raise EvidenceError("GitHub PR file has no filename")
            paths.append(_path(filename))
            previous = item.get("previous_filename")
            if previous:
                paths.append(_path(previous))
        paths = _paths(paths)

        status = self._get(self._path("/commits/%s/status" % head_sha))
        runs = self._get(
            self._path("/actions/runs"), {"head_sha": head_sha, "per_page": self.per_page}
        )
        reviews = self._pages(self._path("/pulls/%d/reviews" % pull_number))
        ci_passed, ci_state = self._ci(status, runs)
        independent_review, review_state = self._reviews(reviews)
        return _snapshot(
            repository,
            pull_number,
            head_sha,
            paths,
            ci_passed,
            independent_review,
            ci_state,
            review_state,
            "github_pr_files",
            "github_read_only",
        )


__all__ = [
    "EvidenceAdapter",
    "EvidenceError",
    "FixtureEvidenceAdapter",
    "GithubEvidenceAdapter",
]
