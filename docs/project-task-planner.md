# Automatic task planning

`tools/project_task_planner.py` is the bounded planning layer between the
read-only GitHub monitor and `project_codex_executor.py`. It does not replace
either component and it never writes to GitHub.

## Standard task format

An issue becomes eligible only when it has the exact `codex-task` label and one
JSON block fenced as `codex-task`:

````text
```codex-task
{"target":"server","repository":"example/coordination-hub","goal":"Add a small pure helper and focused tests","allowed_paths":["tools/example.py","tests/test_example.py"],"test_command":["python3","-m","unittest","discover","-s","tests","-p","test_example.py","-v"],"timeout_seconds":180}
```
````

The host parser accepts only these fields. It rejects sensitive paths, shell
syntax, credentials, production/deployment work, unknown repositories, target
values other than `server`, and commands outside the fixed unittest argv
shape. Computer and mobile tasks remain manual.

## Planning and execution boundary

For one pending verified server row the planner fetches the issue with the
existing read-only repository token, clones a local mirror, and runs Codex in
`read-only` sandbox mode. The model must return JSON containing a low-risk
summary, a subset of the supplied file allowlist, and the unchanged test argv.
The host validates that output again and writes a private generated-plan file.
Only then does the existing project executor receive the plan and perform the
workspace/test/push/Draft-PR flow.

Plans and planner outcomes are bounded, private, and deterministic by task ID.
`PLANNED` or `BLOCKED` entries are not retried by the five-minute timer; an
operator must explicitly clear/retry them. Existing static plans remain under
the reviewed executor configuration. No Issue body, comment, model claim, or
planner output is treated as an approval or shell command.
