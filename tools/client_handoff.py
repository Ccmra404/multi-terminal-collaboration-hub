#!/usr/bin/env python3
"""Validate and format computer/mobile task handoff events."""

from __future__ import print_function

import json
import re


TARGETS = frozenset(("computer", "mobile"))
STATUSES = frozenset(("PENDING", "CLAIMED", "RUNNING", "REVIEW", "DONE", "FAILED", "BLOCKED"))

_REQUIRED_FIELDS = ("task_id", "target", "status", "event_id")
_OPTIONAL_FIELDS = ("pull_request_url", "commit")
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")
_EVENT_ID_RE = re.compile(r"^evt-[0-9a-f]{16,64}$")
_PULL_REQUEST_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*$"
)
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


class HandoffValidationError(ValueError):
    """A client handoff event is malformed or unsupported."""


def _required_string(event, field):
    value = event.get(field)
    if not isinstance(value, str) or not value:
        raise HandoffValidationError("%s must be a non-empty string" % field)
    if value != value.strip():
        raise HandoffValidationError("%s must not have surrounding whitespace" % field)
    return value


def validate_event(event):
    """Return a validated, normalized copy of a client handoff event.

    Unknown fields are rejected so a sender cannot accidentally rely on data
    that a receiver will silently discard.
    """
    if not isinstance(event, dict):
        raise HandoffValidationError("event must be an object")

    allowed = set(_REQUIRED_FIELDS + _OPTIONAL_FIELDS)
    unknown = sorted(set(event) - allowed)
    if unknown:
        raise HandoffValidationError("unknown field: %s" % unknown[0])

    task_id = _required_string(event, "task_id")
    if not _TASK_ID_RE.fullmatch(task_id):
        raise HandoffValidationError("task_id must be owner/repository#issue")

    target = _required_string(event, "target")
    if target not in TARGETS:
        raise HandoffValidationError("target must be computer or mobile")

    status = _required_string(event, "status")
    if status not in STATUSES:
        raise HandoffValidationError("status is unsupported")

    event_id = _required_string(event, "event_id")
    if not _EVENT_ID_RE.fullmatch(event_id):
        raise HandoffValidationError("event_id must be evt- followed by 16 to 64 lowercase hex characters")

    result = {
        "task_id": task_id,
        "target": target,
        "status": status,
        "event_id": event_id,
    }

    if "pull_request_url" in event:
        pull_request_url = _required_string(event, "pull_request_url")
        if not _PULL_REQUEST_URL_RE.fullmatch(pull_request_url):
            raise HandoffValidationError("pull_request_url must be a GitHub pull request URL")
        result["pull_request_url"] = pull_request_url

    if "commit" in event:
        commit = _required_string(event, "commit")
        if not _COMMIT_RE.fullmatch(commit):
            raise HandoffValidationError("commit must be a 7 to 64 character hexadecimal object ID")
        result["commit"] = commit.lower()

    return result


def format_event(event):
    """Validate *event* and return deterministic, compact JSON."""
    return json.dumps(validate_event(event), sort_keys=True, separators=(",", ":"))
