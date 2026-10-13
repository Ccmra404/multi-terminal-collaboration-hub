# Contributing

1. Start from the latest `main` and create a task-specific branch.
2. One task has one active writer. Computer and mobile-plugin agents review or
   read; they do not write the server branch.
3. Keep repository identifiers explicit (`owner/name#issue` and `owner/name#PR`).
   Do not scan all repositories or join same-number Issues across repositories.
4. Keep credentials and raw private GitHub text out of commits, reports, and tests.
5. Run the standard-library tests and syntax check before opening a Draft PR:

   ```bash
   python3 -m unittest discover -s tests -v
   python3 -m py_compile tools/github_coordination.py tools/once_verify.py \
     tools/authorization.py tools/policy_check.py tools/evidence.py \
     tools/workflow.py tools/workflow_demo.py tools/status_web.py \
     tools/task_dispatch.py
   python3 tools/workflow_demo.py --simulate-approval >/dev/null
   ```

   The repository's read-only GitHub Actions workflow repeats these checks on
   supported Python versions; it does not deploy or publish anything.

6. Include the base SHA, branch, HEAD SHA, test commands, known Python-version
   limitations, and remaining review questions in the GitHub handoff.
7. Do not merge, release, deploy, enable continuous watch, or create GitHub
   Projects unless a separate task explicitly authorizes it.

For the two-repository acceptance task, use `tools/once_verify.py` and its
credential gate. Never replace the named per-repository environment variables
with a token literal or a broad CLI login in a committed command.
