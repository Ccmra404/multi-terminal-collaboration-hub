import json
import sys
import unittest
from pathlib import Path


TOOLS = str(Path(__file__).resolve().parents[1] / "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import client_handoff


class ClientHandoffTests(unittest.TestCase):
    def test_validates_minimal_computer_event_without_mutating_input(self):
        event = {
            "task_id": "example/coordination-hub#12",
            "target": "computer",
            "status": "RUNNING",
            "event_id": "evt-1234567890abcdef",
        }
        validated = client_handoff.validate_event(event)
        self.assertEqual(validated, event)
        self.assertIsNot(validated, event)

    def test_formats_mobile_event_with_delivery_details(self):
        event = {
            "task_id": "example/coordination-hub#12",
            "target": "mobile",
            "status": "REVIEW",
            "event_id": "evt-abcdef1234567890",
            "pull_request_url": "https://github.com/example/coordination-hub/pull/34",
            "commit": "ABCDEF1234567",
        }
        formatted = client_handoff.format_event(event)
        self.assertNotIn(" ", formatted)
        parsed = json.loads(formatted)
        self.assertEqual(parsed["commit"], "abcdef1234567")
        self.assertEqual(parsed["pull_request_url"], event["pull_request_url"])

    def test_rejects_invalid_required_fields(self):
        base = {
            "task_id": "owner/repository#1",
            "target": "computer",
            "status": "DONE",
            "event_id": "evt-1234567890abcdef",
        }
        invalid_values = {
            "task_id": "repository#1",
            "target": "server",
            "status": "finished",
            "event_id": "event-1",
        }
        for field, value in invalid_values.items():
            with self.subTest(field=field):
                event = dict(base)
                event[field] = value
                with self.assertRaises(client_handoff.HandoffValidationError):
                    client_handoff.validate_event(event)

    def test_rejects_bad_optional_and_unknown_fields(self):
        base = {
            "task_id": "owner/repository#1",
            "target": "mobile",
            "status": "FAILED",
            "event_id": "evt-1234567890abcdef",
        }
        for extra in (
            {"pull_request_url": "http://example.com/pull/1"},
            {"commit": "not-a-commit"},
            {"summary": "silently ignored?"},
        ):
            with self.subTest(extra=extra):
                event = dict(base)
                event.update(extra)
                with self.assertRaises(client_handoff.HandoffValidationError):
                    client_handoff.validate_event(event)

    def test_rejects_non_object_or_empty_optional_value(self):
        with self.assertRaises(client_handoff.HandoffValidationError):
            client_handoff.validate_event([])
        with self.assertRaises(client_handoff.HandoffValidationError):
            client_handoff.validate_event({
                "task_id": "owner/repository#1",
                "target": "computer",
                "status": "DONE",
                "event_id": "evt-1234567890abcdef",
                "commit": "",
            })


if __name__ == "__main__":
    unittest.main()
