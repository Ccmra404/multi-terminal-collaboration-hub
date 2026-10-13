# 多端协作中心

电脑、手机与服务器之间的任务协调和状态看板。

This repository contains a read-only, explicitly configured GitHub monitor for
development and de-identified simulation. It does not deploy, watch continuously,
write GitHub Issues/PRs/Projects, or store credentials. Start with
[`docs/github-coordination.md`](docs/github-coordination.md) and copy the example
configuration only to a private server path.

The server is the sole writer for the current task. Computer and mobile-plugin
workspaces review and read handoffs; every later task gets its own branch and one
active writer.

Pull requests run the read-only test workflow in `.github/workflows/test.yml`; it
performs tests and validation only, with no deployment or publishing step.

For the authorized two-repository one-shot acceptance path, see
[`docs/once-verification.md`](docs/once-verification.md) and
`tools/once_verify.py`. The command stops at a missing credential gate and has no
watch mode.

The mobile-first read-only status dashboard lives in `tools/status_web.py` and
`web/status/`. It consumes the existing two-repository monitor report, never
loads a GitHub token in the browser, and binds to `127.0.0.1` by default. See
[`docs/status-dashboard.md`](docs/status-dashboard.md) for local launch and
the private SSH-tunnel boundary.

The local high-autonomy v2 workflow lives in `tools/evidence.py`,
`tools/workflow.py`, and `tools/workflow_demo.py`. It provides a durable local
event/approval flow and read-only GitHub evidence seam; it does not write GitHub,
wake another session, execute task payloads, merge, deploy, or change the coordination issue read-only monitor. See [`docs/authorization-policy.md`](docs/authorization-policy.md)
for the safety boundaries and the sandbox command.

The coordination issue dispatch MVP lives in `tools/task_dispatch.py`. It adds a durable
local queue over the existing monitor snapshot, manual computer/mobile handoff,
and one fixed server health-check executor. coordination issue adds an on-demand,
allowlisted Codex CLI adapter in `tools/codex_executor.py`; it does not turn
Issue text into commands, write GitHub, or alter the existing read-only timer.
See [`docs/task-dispatch.md`](docs/task-dispatch.md) and
[`docs/codex-executor.md`](docs/codex-executor.md).

coordination issue adds the opt-in real-repository delivery layer in
`tools/project_codex_executor.py`. It uses exact trusted plans to create a
worktree branch, run focused tests, push, and open a Draft PR while keeping
GitHub write credentials out of the Codex child. See
[`docs/project-codex-executor.md`](docs/project-codex-executor.md).

coordination issue adds the opt-in automatic planning layer in
`tools/project_task_planner.py`. A labelled, strict `codex-task` issue is
read with the existing monitor credential, planned in Codex read-only mode,
validated by the host, and then handed to the same real-project executor.
See [`docs/project-task-planner.md`](docs/project-task-planner.md).
