# Codex CLI executor (opt-in)

`tools/codex_executor.py` is the on-demand adapter for the existing local
queue. It reuses `DispatchStore`, the existing target verification, stop
switch, heartbeat/lease, result state, and dashboard projection. It is not
connected to `collaboration-hub-dispatch.timer` by this PR.

## Trust boundary

The executor never reads a GitHub Issue body and never turns Issue text into a
shell command. A task is eligible only when all of these are true:

1. The durable queue has the exact task ID and `target=server` with
   `target_verified=true`.
2. A local, reviewed JSON allowlist has the same task ID, repository, and
   issue number, with `trusted=true`.
3. The plan is an isolated workspace plan with a bounded prompt, an exact
   allowlist of expected files, and the fixed local `unittest` check.

The two repositories accepted by the queue remain the only repository scope.
Plans containing credentials, root/privileged actions, deployment, device or
network-opening requests are rejected. Computer and mobile rows are never
selected by this executor.

## Execution boundary

The child is launched with the server's installed Codex CLI using `codex exec
--ephemeral --ignore-user-config --sandbox workspace-write`; stdin is closed,
the prompt is an argv value, and a minimal environment passes only the
explicit `CODEX_HOME` login directory plus managed proxy/TLS routing. The
executor refuses to run as root and requires the login directory, workspace
root, and task workspace to be owned by the service user with mode `0700`.
The monitor read-only token, GitHub variables, deployment credentials, and the
caller's general environment are not passed to the child.

One non-blocking file lock enforces concurrency 1. The task workspace is
stable across restarts, and an active/finished queue record is not started a
second time. The child and the fixed local test have bounded time/resource
limits. Only allowlisted files may remain; symlinks and unexpected artifacts
produce `BLOCKED`. Results contain sanitized log summaries and artifact sizes,
so the existing status website displays the `DONE`, `FAILED`, or `BLOCKED`
state without a new web path.

## On-demand commands

Review and install a private copy of the example configuration first; it is
not installed automatically:

```bash
python3 tools/codex_executor.py validate-config \
  --config /etc/collaboration-hub/codex-executor.json

python3 tools/codex_executor.py --state-file /var/lib/collaboration-hub/dispatch/state.json \
  run-next \
  --config /etc/collaboration-hub/codex-executor.json \
  --workspace-root /var/lib/collaboration-hub/codex-workspaces
```

`run` accepts an exact task ID when an operator has reviewed the plan. A
failed task requires an explicit `--retry`; a paused queue is not resumed by
the executor. `run-next` returns without executing when no pending server row
has an exact trusted plan.

This PR does not install the config, copy a login token, change the existing
five-minute timers, create GitHub comments, push branches, open/merge PRs,
deploy code, or change permissions. Formal unattended enablement requires one
separate approval covering the service identity, login directory, config path,
timer command, and rollback.
