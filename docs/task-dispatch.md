# Local task dispatch MVP

`tools/task_dispatch.py` is a small local queue on top of the existing
read-only monitor report. It only accepts the two explicitly authorized
repositories and never writes GitHub. The Issue/PR text stays data; it is not a
shell command.

## Target and state rules

The stable task ID is `owner/name#issue`. A target is accepted only from a
verified `target`, `owner`, or `next_owner` handoff field, or from an exact
operator-supplied override. Everything else remains `target=unknown` and is
not claimable. The queue uses:

`PENDING → CLAIMED → RUNNING → REVIEW/DONE/FAILED/BLOCKED`

There is one claimant, an attempt counter, claim/heartbeat timestamps, result,
commit, PR, and a bounded log summary. State is an atomic 0600 JSON file. A
stale active claim is changed to `FAILED`; retry requires an explicit
`--retry`, so a restart cannot silently execute work twice.

## Start, stop, and inspect

The shortest local run is:

```bash
python3 tools/task_dispatch.py --state-file /var/lib/collaboration-hub/dispatch/state.json \
  sync --report-file /var/lib/collaboration-hub/once/report.json \
  --target-config /etc/collaboration-hub/task-dispatch.json
python3 tools/task_dispatch.py --state-file /var/lib/collaboration-hub/dispatch/state.json status
python3 tools/task_dispatch.py --state-file /var/lib/collaboration-hub/dispatch/state.json list --target computer
```

For a stable JSON count of known queue statuses (with missing or unrecognized
values collected under `UNKNOWN`), run:

```bash
python3 tools/task_status_summary.py /var/lib/collaboration-hub/dispatch/state.json
```

For the equivalent stable JSON count of target labels (with missing or
unrecognized values collected under `unknown`), run:

```bash
python3 tools/task_target_summary.py /var/lib/collaboration-hub/dispatch/state.json
```

`pause --reason ...` is the persistent stop switch; `resume` clears it.
`recover` marks claims whose heartbeat has expired as failed. The existing
read-only monitor and dispatch timers are separate units. Their cadence is an
operator setting; adjust existing units only after checking API rate limits,
and do not add a duplicate timer.

The default server executor remains the fixed, harmless
`server_health_check` child. `run-server-next` deterministically auto-claims
one `PENDING` server row and starts that allowlisted action; it never retries a
failed row automatically. The separate, opt-in Codex CLI adapter is documented
in [`docs/codex-executor.md`](codex-executor.md). It is an on-demand command
only in this PR and does not alter this timer's command.
The child reads the bounded monitor snapshot, confirms the selected server task
is present, and records a JSON result. It has no shell, no GitHub token, a
bounded timeout, and checks the stop switch before completion:

```bash
python3 tools/task_dispatch.py --state-file /var/lib/collaboration-hub/dispatch/state.json \
  run-server-next \
  --report-file /var/lib/collaboration-hub/once/report.json --timeout 30
```

`run-server-test --task-id ...` is the explicit equivalent when an operator
needs to select a known task.

Computer and mobile workspaces use the same `claim`, `start`, `heartbeat`, and
`submit --status REVIEW` commands manually. The dashboard labels their pending
rows `待电脑领取` and `待手机领取`; there is no remote wake-up.

The queue also records verified source completion. If an authorized handoff
later marks a task `DONE`, the next sync resolves a stale local `BLOCKED` or
`FAILED` row to `DONE`; active local claims remain untouched.

`config/task-dispatch.example.json` demonstrates explicit local target
overrides. It is not installed automatically and must be reviewed before use;
an override is an assignment, not a guess made from a title.

## Dashboard integration and runtime boundary

The existing read-only status API optionally reads the local state file via
`--dispatch-state-file` (default `/var/lib/collaboration-hub/dispatch/state.json`).
It overlays queue status on the existing task cards and exposes a bounded
`dispatch` summary. The web process remains GET-only and does not receive a
GitHub credential. This PR does not deploy or enable a new service. The
repository/runtime status is `QUEUE_ONLY` (the runner is an on-demand command,
not an unattended daemon); while a child is executing it is transiently
`RUNNER_WORKING`, and an unavailable executable would be reported as
`BLOCKED_EXECUTOR`. A live child run is recorded as evidence rather than being
inferred from CI.

No automatic GitHub comments, merges, releases, production deploys, firewall
changes, or credential changes are part of this MVP. The Codex adapter also
does not copy or broaden a login token; it requires a pre-provisioned,
service-owned login directory for any future approved enablement.
