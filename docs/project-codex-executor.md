# Real-project Codex delivery

`tools/project_codex_executor.py` is the opt-in delivery layer for coordination issue.
It builds on the isolated `codex_executor.py` adapter and the existing
`DispatchStore`; it does not replace the read-only monitor, queue, dashboard,
or fixed `server_health_check`.

## Trust and write boundaries

The runner selects only an exact `target=server` queue row that also has a
matching `trusted=true` local plan. The plan fixes the repository, `main` base,
branch name, prompt, changed-file allowlist, local unittest argv, commit text,
and Draft PR text. GitHub Issue bodies, comments, and arbitrary task fields are
never passed to a shell or to Codex.

The Codex child receives the service-owned `CODEX_HOME`, a private repository
worktree, and managed proxy/TLS routing only. It receives no `GH_TOKEN_*`,
monitor token, server password, or deployment credential. The host-side git and
`gh` wrapper uses one dedicated `*_WRITE` variable only for the target
repository; it verifies the remote host and plan repository before push/PR.

## Flow

For one selected task the host:

1. fetches the configured repository mirror at `origin/main`;
2. creates or resumes a task-specific private clone and deterministic
   `codex/...` branch;
3. runs the non-interactive Codex CLI with timeout, stop-switch, and a single
   process lock;
4. rejects any changed path outside the exact allowlist and runs the configured
   `python3 -m unittest` argv without shell interpolation;
5. commits with a fixed service identity, pushes the branch, creates a Draft
   PR, and waits briefly for CI evidence; and
6. records branch, commit, PR, tests, checks, and bounded logs as `REVIEW` in
   the existing queue state, which the current status site already projects.

An existing PR for the deterministic branch is recovered instead of creating a
duplicate. Failed/paused/timeout runs do not retry automatically; a retry must
be explicit. Computer and mobile rows remain manual. No merge, release,
production deploy, firewall change, or permission expansion is performed.

## Commands

```bash
python3 tools/project_codex_executor.py validate-config \
  --config /etc/collaboration-hub/codex-project-executor.json

python3 tools/project_codex_executor.py --state-file /var/lib/collaboration-hub/dispatch/state.json \
  run-next --config /etc/collaboration-hub/codex-project-executor.json
```

The production timer is enabled only with a service-owned `CODEX_HOME`, a
dedicated repository write identity, and the reviewed config. Those deployment
inputs are intentionally absent from this example file.
