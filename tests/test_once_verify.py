import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "once_verify", Path(__file__).resolve().parents[1] / "tools" / "once_verify.py"
)
once_verify = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(once_verify)


class OnceVerifyTests(unittest.TestCase):
    def test_missing_per_repository_credentials_stop_before_network(self):
        config = {
            "repositories": [
                {
                    "repository": "example/coordination-hub",
                    "coordination_issue": 1,
                    "authorized_handoff_users": ["example-owner"],
                    "token_env": "TEST_PROJECT_COLLAB_HUB_TOKEN",
                },
                {
                    "repository": "example/managed-project",
                    "coordination_issue": 9,
                    "authorized_handoff_users": ["example-owner"],
                    "token_env": "TEST_ADBLOCK_PRO_TOKEN",
                },
            ]
        }
        names = ["TEST_PROJECT_COLLAB_HUB_TOKEN", "TEST_ADBLOCK_PRO_TOKEN"]
        saved = {name: os.environ.pop(name, None) for name in names}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                path.write_text(json.dumps(config), encoding="utf-8")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = once_verify.main(["--config", str(path)])
            self.assertEqual(result, 2)
            self.assertIn("credential_gate: BLOCKED", output.getvalue())
            self.assertIn("TEST_PROJECT_COLLAB_HUB_TOKEN", output.getvalue())
            self.assertNotIn("Bearer", output.getvalue())
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
