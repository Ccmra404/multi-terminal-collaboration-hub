#!/usr/bin/env python3
"""Run the local, side-effect-free collaboration v2 sandbox demo."""

from __future__ import print_function

import argparse
import json
import sys
import tempfile
from pathlib import Path

TOOLS = str(Path(__file__).resolve().parent)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import authorization
import evidence
import workflow


DEMO_SHA = "a" * 40
DEMO_REPOSITORY = "example/coordination-hub"
DEMO_TIME = "2026-10-12T08:00:00Z"


def run(config_path, state_path=None, simulate_approval=False):
    policy = authorization.load_policy(config_path)
    adapter = evidence.FixtureEvidenceAdapter(
        {
            (DEMO_REPOSITORY, 8, DEMO_SHA): {
                "paths": ["tools/workflow.py"],
                "ci_passed": True,
                "independent_review": True,
            }
        },
        repository=DEMO_REPOSITORY,
    )
    authority = workflow.SimulationApprovalAuthority(
        enabled=simulate_approval, actor="simulated-maintainer"
    )

    temporary = None
    if state_path is None:
        temporary = tempfile.TemporaryDirectory()
        state_path = Path(temporary.name) / "workflow.json"
    try:
        store = workflow.WorkflowStore(state_path)
        coordinator = workflow.WorkflowCoordinator(
            authorization.PolicyEngine(policy),
            store,
            evidence_adapter=adapter,
            approval_authority=authority,
        )
        submitted = coordinator.submit_task(
            "collaboration-hub",
            DEMO_REPOSITORY,
            "8",
            "server",
            "computer",
            payload={"operation": "validate-next-stage", "execution": "sandbox-only"},
            dedupe_key="demo-task-8",
        )
        discovered = coordinator.discover("computer")
        report = coordinator.report(
            submitted["event_id"], "computer", "reported", "sandbox report ready"
        )
        request = {
            "project_id": "collaboration-hub",
            "repository": DEMO_REPOSITORY,
            "branch": "feat/issue-8-v2-workflow",
            "environment": "development",
            "action": "merge",
            "head_sha": DEMO_SHA,
            "pull_number": 8,
            "actor": "server",
            "executor": "server",
            "task_id": "8",
            # Deliberately forged claims: the fixture adapter replaces them.
            "paths": ["deploy/forged-by-caller"],
            "ci_passed": False,
            "independent_review": False,
            "reason": "demonstrate trusted evidence enrichment",
            "created_at": DEMO_TIME,
        }
        pending = coordinator.request_authorization(request, now=DEMO_TIME)
        approval_attempt = coordinator.decide(
            pending["request_id"],
            "approve",
            "simulated-maintainer" if simulate_approval else "unverified-user",
            reason="explicit sandbox demonstration",
            now=DEMO_TIME,
        )
        resumed = None
        replay = None
        if simulate_approval and approval_attempt.get("status") == "APPROVED":
            resumed = coordinator.resume(pending["request_id"], now=DEMO_TIME)
            replay = coordinator.resume(pending["request_id"], now=DEMO_TIME)
        return {
            "schema_version": 1,
            "mode": "simulation" if simulate_approval else "safe_pending",
            "steps": {
                "submitted_event": submitted,
                "discovered_by_computer": discovered,
                "reported_event": report,
                "authorization": pending,
                "approval_attempt": approval_attempt,
                "resumed_decision": resumed,
                "replay_decision": replay,
            },
            "safety": {
                "writes_github": False,
                "executes_task": False,
                "wakes_independent_session": False,
                "real_user_approval": False,
            },
        }
    finally:
        if temporary is not None:
            temporary.cleanup()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[1] / "config" / "project-authorization.example.json"),
    )
    parser.add_argument("--state-file", help="persist the demo state at this path")
    parser.add_argument(
        "--simulate-approval",
        action="store_true",
        help="enable the explicit simulation-only approval authority",
    )
    args = parser.parse_args(argv)
    result = run(args.config, args.state_file, args.simulate_approval)
    json.dump(result, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
