# Computer and mobile handoff events

`tools/client_handoff.py` validates small handoff events exchanged with the
computer and mobile clients. It has no network, file, or process side effects.

An event contains `task_id` (`owner/repository#issue`), `target` (`computer` or
`mobile`), an uppercase queue `status`, and an `event_id` in the existing
`evt-` plus lowercase hexadecimal format. `pull_request_url` and `commit` are
optional. Pull request URLs must be HTTPS GitHub pull request URLs; commits are
7–64 hexadecimal characters.

```python
from tools.client_handoff import format_event, validate_event

event = {
    "task_id": "example/coordination-hub#12",
    "target": "mobile",
    "status": "REVIEW",
    "event_id": "evt-abcdef1234567890",
    "pull_request_url": "https://github.com/your-org/your-project/pull/1",
    "commit": "0123456789abcdef0123456789abcdef01234567",
}

validated = validate_event(event)
payload = format_event(event)
```

`validate_event` returns a new dictionary and lowercases a hexadecimal commit.
`format_event` performs the same validation and returns deterministic compact
JSON. Both raise `HandoffValidationError` for missing, unknown, malformed, or
unsupported fields. Supported statuses are `PENDING`, `CLAIMED`, `RUNNING`,
`REVIEW`, `DONE`, `FAILED`, and `BLOCKED`.
