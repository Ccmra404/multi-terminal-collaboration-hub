#!/usr/bin/env python3
"""Run one bounded, two-repository GitHub verification and print a safe summary.

This wrapper deliberately has no watch mode. It validates the exact authorized
repository set, checks that per-repository token environment variables are
present without printing their values, delegates collection to the existing
read-only monitor, and renders only bounded report fields.
"""

from __future__ import print_function

import argparse
import importlib.util
import os
from pathlib import Path
import stat
import sys


try:
    import github_coordination as coordination
except ImportError:
    _SPEC = importlib.util.spec_from_file_location(
        "github_coordination", Path(__file__).resolve().with_name("github_coordination.py")
    )
    coordination = importlib.util.module_from_spec(_SPEC)
    assert _SPEC.loader is not None
    _SPEC.loader.exec_module(coordination)


EXPECTED_REPOSITORIES = {
    "example/coordination-hub": 1,
    "example/managed-project": 9,
}


def _validate_config(values):
    specs = coordination.normalize_repository_specs(values)
    configured = {spec["repository"]: spec for spec in specs}
    if set(configured) != set(EXPECTED_REPOSITORIES):
        raise coordination.CoordinationError(
            "once verification requires exactly the two authorized repositories"
        )
    for repository, coordination_issue in EXPECTED_REPOSITORIES.items():
        spec = configured[repository]
        if spec["coordination_issue"] != coordination_issue:
            raise coordination.CoordinationError(
                "unexpected coordination Issue for " + repository
            )
        if not spec.get("authorized_handoff_users"):
            raise coordination.CoordinationError(
                "authorized_handoff_users must be explicit for " + repository
            )
    return [configured[repository] for repository in EXPECTED_REPOSITORIES]


def _mode(path):
    path = Path(path).expanduser()
    try:
        return "%04o" % stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return "missing"


def _task_line(task, pull_by_number):
    pulls = []
    for number in task.get("pull_requests", []):
        pull = pull_by_number.get(number)
        if not pull:
            pulls.append("%s#%s@unknown" % (task.get("repository", "unknown"), number))
            continue
        ci = pull.get("ci", {})
        pulls.append(
            "%s@%s/ci=%s"
            % (pull.get("pull_ref", "unknown"), pull.get("head_sha", "unknown"), ci.get("state", "unknown"))
        )
    return (
        "task={issue_ref}; owner={owner}; status={status}; status_verified={status_verified}; "
        "approval={approval}; approval_verified={approval_verified}; prs={prs}; updated={updated_at}"
    ).format(
        issue_ref=task.get("issue_ref", "unknown"),
        owner=task.get("owner", "unknown"),
        status=task.get("status", "unknown"),
        status_verified=task.get("status_verified", False),
        approval=task.get("approval", "unknown"),
        approval_verified=task.get("approval_verified", False),
        prs=",".join(pulls) if pulls else "none",
        updated_at=task.get("updated_at", "unknown"),
    )


def _repository_result(report):
    warnings = list(report.get("warnings", []))
    main_sha = report.get("current_main_sha", "unknown")
    complete_sha = bool(coordination.SHA_RE.fullmatch(str(main_sha)))
    pull_by_number = {
        pull.get("number"): pull
        for pull in report.get("pull_requests", [])
        if isinstance(pull, dict)
    }
    task_lines = [_task_line(task, pull_by_number) for task in report.get("tasks", [])]
    result = "PASS" if complete_sha and report.get("coordination_state") != "unknown" and not warnings else "PARTIAL"
    return {
        "repository": report.get("repository", "unknown"),
        "result": result,
        "main_sha": main_sha,
        "coordination_ref": report.get("coordination_ref", "unknown"),
        "task_count": len(report.get("tasks", [])),
        "pull_count": len(report.get("pull_requests", [])),
        "tasks": task_lines,
        "warnings": warnings,
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[1] / "config" / "two-repo-once.example.json"),
        help="two-repository JSON configuration",
    )
    parser.add_argument("--state-file", help="override the private local state file")
    parser.add_argument("--report-file", help="override the private local report file")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        values = coordination.load_config(args.config)
        specs = _validate_config(values)
        missing = [spec["token_env"] for spec in specs if not os.environ.get(spec["token_env"])]
        if missing:
            print("credential_gate: BLOCKED")
            print("missing_token_envs: " + ",".join(missing))
            print(
                "required_scope: each named repository, read-only Metadata/Issues/Pull requests/Actions; "
                "add Contents or commit-status read only only if the API requires it"
            )
            print("next_action: inject the named values through a secure server credential mechanism; do not paste tokens here")
            return 2

        monitor, _ = coordination.build_monitor(values)
        state_path = args.state_file or values.get(
            "state_file", "/var/lib/collaboration-hub/once/state.json"
        )
        report_path = args.report_file or values.get(
            "report_file", "/var/lib/collaboration-hub/once/report.json"
        )
        report = coordination.run_once(
            monitor, coordination.StateStore(state_path), report_path=report_path
        )
        results = [_repository_result(item) for item in report.get("repositories", [])]
        overall = "PASS" if results and all(item["result"] == "PASS" for item in results) else "PARTIAL"
        print("once_verification: " + overall)
        print("source: live GitHub API GET; mode=once; watch=disabled")
        print("generated_at: " + report.get("generated_at", "unknown"))
        for item in results:
            print(
                "repository={repository}; result={result}; main_sha={main_sha}; "
                "coordination={coordination_ref}; tasks={task_count}; pulls={pull_count}".format(**item)
            )
            for task in item["tasks"]:
                print("  " + task)
            for warning in item["warnings"]:
                print("  warning=" + warning)
        print("state_file_mode=" + _mode(state_path) + "; report_file_mode=" + _mode(report_path))
        return 0 if overall == "PASS" else 2
    except (coordination.CoordinationError, OSError) as error:
        print("once_verification: ERROR")
        print("error: " + coordination.clean_text(error, 500))
        return 2


if __name__ == "__main__":
    sys.exit(main())
