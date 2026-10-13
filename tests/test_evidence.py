import sys
import unittest
from pathlib import Path


TOOLS = str(Path(__file__).resolve().parents[1] / "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import evidence


REPOSITORY = "example/hub"
SHA = "a" * 40


class GithubFixture(object):
    def __init__(self, head_sha=SHA, files=None, per_page=100):
        self.head_sha = head_sha
        self.files = files if files is not None else [{"filename": "tools/workflow.py", "status": "modified"}]
        self.per_page = per_page
        self.calls = []

    def get(self, path, params=None):
        params = params or {}
        self.calls.append((path, dict(params)))
        prefix = "/repos/" + REPOSITORY
        if path == prefix + "/pulls/7":
            return {"number": 7, "head": {"sha": self.head_sha}}
        if path == prefix + "/pulls/7/files":
            page = int(params.get("page", 1))
            if page == 1:
                return list(self.files)
            return []
        if path == prefix + "/commits/" + SHA + "/status":
            return {"state": "success", "statuses": [{"state": "success"}]}
        if path == prefix + "/actions/runs":
            return {"workflow_runs": [{"status": "completed", "conclusion": "success"}]}
        if path == prefix + "/pulls/7/reviews":
            return [{"state": "APPROVED"}]
        raise AssertionError("unexpected GET: %s %s" % (path, params))


def request(**overrides):
    value = {
        "repository": REPOSITORY,
        "pull_number": 7,
        "head_sha": SHA,
        "paths": ["deploy/forged"],
        "ci_passed": False,
        "independent_review": False,
    }
    value.update(overrides)
    return value


class EvidenceTests(unittest.TestCase):
    def test_github_adapter_replaces_forged_claims_with_get_only_facts(self):
        api = GithubFixture(files=[
            {"filename": "tools/workflow.py", "status": "modified"},
            {"filename": "src/new.py", "previous_filename": "src/old.py", "status": "renamed"},
        ])
        adapter = evidence.GithubEvidenceAdapter(api, REPOSITORY)
        enriched, snapshot = adapter.enrich(request())
        self.assertEqual(enriched["paths"], ["tools/workflow.py", "src/new.py", "src/old.py"])
        self.assertTrue(enriched["ci_passed"])
        self.assertTrue(enriched["independent_review"])
        self.assertEqual(enriched["path_evidence"]["source"], "github_pr_files")
        self.assertEqual(snapshot["provenance"], "github_read_only")
        self.assertEqual(snapshot["head_sha"], SHA)
        self.assertTrue(all(path.startswith("/repos/" + REPOSITORY) for path, _ in api.calls))

    def test_head_mismatch_fails_closed(self):
        api = GithubFixture(head_sha="b" * 40)
        adapter = evidence.GithubEvidenceAdapter(api, REPOSITORY)
        with self.assertRaises(evidence.EvidenceError):
            adapter.collect(request())

    def test_full_page_at_bound_is_incomplete(self):
        api = GithubFixture(files=[{"filename": "src/a.py"}])
        adapter = evidence.GithubEvidenceAdapter(api, REPOSITORY, max_pages=1, per_page=1)
        with self.assertRaises(evidence.EvidenceError):
            adapter.collect(request())

    def test_unsafe_filename_fails_closed(self):
        api = GithubFixture(files=[{"filename": "../secrets.txt"}])
        adapter = evidence.GithubEvidenceAdapter(api, REPOSITORY)
        with self.assertRaises(evidence.EvidenceError):
            adapter.collect(request())

    def test_fixture_adapter_does_not_use_request_facts(self):
        adapter = evidence.FixtureEvidenceAdapter(
            {
                (REPOSITORY, 7, SHA): {
                    "paths": ["src/real.py"],
                    "ci_passed": True,
                    "independent_review": True,
                }
            }
        )
        enriched, snapshot = adapter.enrich(request())
        self.assertEqual(enriched["paths"], ["src/real.py"])
        self.assertTrue(enriched["ci_passed"])
        self.assertNotEqual(enriched["paths"], request()["paths"])
        self.assertEqual(snapshot["provenance"], "simulation_fixture")


if __name__ == "__main__":
    unittest.main()
