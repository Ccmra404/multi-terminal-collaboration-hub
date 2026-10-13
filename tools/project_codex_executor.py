#!/usr/bin/env python3
"""Real-repository Codex delivery adapter.

This is the second, opt-in layer on top of ``codex_executor``.  It turns one
exact, trusted server queue row into a clean repository branch, a bounded Codex
workspace, a fixed local test command, and a Draft PR.  The GitHub write token
is used only by the host-side git/gh wrapper; it is never included in the
Codex child environment and Issue text is never interpreted as a command.
"""

from __future__ import print_function

import argparse
import fnmatch
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

try:
    import task_dispatch
    import codex_executor
except ImportError:  # pragma: no cover - run-from-repository-root convenience
    _tools = str(Path(__file__).resolve().parent)
    if _tools not in sys.path:
        sys.path.insert(0, _tools)
    import task_dispatch
    import codex_executor


EXECUTOR_NAME = "codex_project_task-v1"
CONFIG_SCHEMA_VERSION = 1
CONFIG_EXECUTOR = "codex_project_task"
DEFAULT_CONFIG_FILE = "/etc/collaboration-hub/codex-project-executor.json"
DEFAULT_STATE_FILE = task_dispatch.DEFAULT_STATE_FILE
MAX_CONFIG_BYTES = 512 * 1024
MAX_PLAN_FILE_BYTES = 512 * 1024
MAX_PLANS = 100
MAX_REPOSITORIES = 2
MAX_PATHS = 40
MAX_PROMPT_CHARS = 6000
MAX_TEXT_CHARS = 6000
MAX_TIMEOUT_SECONDS = 900
MIN_TIMEOUT_SECONDS = 30
MAX_COMMAND_OUTPUT = 6000
CHECK_TIMEOUT_SECONDS = 180
CHECK_POLL_SECONDS = 5
CHECK_PENDING = frozenset(("queued", "in_progress", "pending"))
SAFE_TEST_PREFIXES = (("python3", "-m", "unittest"), ("node", "--test"))
DEFAULT_ALLOWED_PATH_PATTERNS = ("tools/**", "tests/**", "docs/**", "web/status/**", "README*", "LICENSE*")
INTERNAL_PATH_PREFIXES = ("__pycache__/", ".pytest_cache/", ".mypy_cache/")
INTERNAL_PATHS = frozenset((".codex-executor.lock",))
SAFE_PROMPT_BLOCK = re.compile(
    r"(?i)(?:\b(?:token|password|secret|root|sudo|ssh|production|deploy|deployment|"
    r"firewall|release|merge|device|credential|authorization)\b|/etc/|/root/|"
    r"~/.ssh|public\s+internet|external\s+network|issue\s+body|shell\s+command)"
)
SENSITIVE_PATH_RE = re.compile(
    r"(?i)(?:^|/)(?:config|deploy|deployment|secrets?|credentials?|\.github/workflows)(?:/|$)|"
    r"(?:^|/)(?:security\.md|\.env(?:\.|$))"
)
REMOTE_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\.git$")
BRANCH_RE = re.compile(r"^codex/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
PLAN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class ProjectExecutorError(ValueError):
    """Expected project executor/configuration boundary failure."""


def _clean(value, maximum=240):
    try:
        return codex_executor._clean(value, maximum)
    except Exception:
        text = "" if value is None else str(value)
        return text.replace("\x00", "")[:maximum]


def _summary(value, maximum=MAX_COMMAND_OUTPUT):
    try:
        return codex_executor._bounded_summary(value, maximum)
    except Exception:
        return _clean(value, maximum)


def _load_json(path):
    path = Path(path).expanduser()
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise ProjectExecutorError("project executor config exceeds size limit")
        return json.loads(path.read_text(encoding="utf-8"))
    except ProjectExecutorError:
        raise
    except (OSError, ValueError) as error:
        raise ProjectExecutorError("cannot read project executor config: " + _clean(error, 240))


def _safe_relative_path(value):
    value = _clean(value, 240).strip()
    path = Path(value)
    if not value or "\x00" in value or path.is_absolute() or value.startswith("~"):
        raise ProjectExecutorError("allowed_paths must be relative workspace paths")
    if ".." in path.parts or path.name in ("", ".", ".."):
        raise ProjectExecutorError("allowed_paths cannot escape the repository")
    normalized = "/".join(path.parts)
    if normalized.startswith(".git/") or normalized == ".git":
        raise ProjectExecutorError("allowed_paths cannot include .git")
    return normalized


def _safe_branch(value):
    value = _clean(value, 160).strip()
    if not BRANCH_RE.fullmatch(value) or ".." in value or value.endswith("/"):
        raise ProjectExecutorError("plan branch must be a codex namespace branch")
    return value


def _safe_text(value, label, maximum=MAX_TEXT_CHARS):
    value = _clean(value, maximum).strip()
    if not value or "\x00" in value:
        raise ProjectExecutorError(label + " is empty or contains NUL")
    if len(value) > maximum:
        raise ProjectExecutorError(label + " is too long")
    return value


def _safe_prompt(value):
    value = _safe_text(value, "trusted project prompt", MAX_PROMPT_CHARS)
    if SAFE_PROMPT_BLOCK.search(value):
        raise ProjectExecutorError("trusted project prompt contains a blocked sensitive action")
    return value


def _validate_test_command(value):
    if not isinstance(value, list) or len(value) < 2 or len(value) > 16:
        raise ProjectExecutorError("test_command must be a bounded argv list")
    normalized = [_clean(item, 120) for item in value]
    if not any(tuple(normalized[:len(prefix)]) == prefix for prefix in SAFE_TEST_PREFIXES):
        raise ProjectExecutorError("test_command must use an allowlisted Python or Node test runner")
    for item in normalized:
        if not item or any(token in item for token in (";", "&&", "||", "`", "$(", "|")):
            raise ProjectExecutorError("test_command contains shell syntax")
        if not re.fullmatch(r"[A-Za-z0-9_./*=-]+", item):
            raise ProjectExecutorError("test_command contains unsupported characters")
    return tuple(normalized)


def _validate_path_patterns(value):
    if value is None:
        return DEFAULT_ALLOWED_PATH_PATTERNS
    if not isinstance(value, list) or not value or len(value) > 20:
        raise ProjectExecutorError("allowed_path_patterns must be a bounded non-empty list")
    result = []
    for item in value:
        pattern = _clean(item, 160).replace("\\", "/").strip()
        if (not pattern or pattern.startswith("/") or ".." in pattern.split("/")
                or SENSITIVE_PATH_RE.search(pattern) or not re.fullmatch(r"[A-Za-z0-9_./*-]+", pattern)):
            raise ProjectExecutorError("allowed_path_patterns contains an unsafe pattern")
        if pattern not in result:
            result.append(pattern)
    return tuple(result)


def _validate_repository(repository, raw):
    if not isinstance(raw, dict):
        raise ProjectExecutorError("repository config must be an object")
    remote = _safe_text(raw.get("remote"), "repository remote", 400)
    match = REMOTE_RE.fullmatch(remote)
    if not match or match.group(1) != repository:
        raise ProjectExecutorError("repository remote must be the exact authorized GitHub URL")
    mirror = _safe_text(raw.get("mirror"), "repository mirror", 500)
    mirror_path = Path(mirror)
    if not mirror_path.is_absolute() or ".git" in mirror_path.parts:
        raise ProjectExecutorError("repository mirror path is invalid")
    main_branch = _safe_text(raw.get("main_branch"), "repository main_branch", 100)
    if main_branch != "main":
        raise ProjectExecutorError("only main is supported as the base branch")
    allowed_patterns = _validate_path_patterns(raw.get("allowed_path_patterns"))
    test_command = _validate_test_command(raw.get("test_command", [
        "python3", "-m", "unittest", "discover", "-s", "tests", "-v"
    ]))
    return {
        "remote": remote,
        "mirror": mirror_path,
        "main_branch": main_branch,
        "allowed_path_patterns": allowed_patterns,
        "test_command": test_command,
    }


def _validate_plan(task_id, raw, repositories):
    if not isinstance(raw, dict) or raw.get("trusted") is not True:
        raise ProjectExecutorError("plan requires trusted=true confirmation")
    normalized_id = task_dispatch._require_task_id(task_id)
    repository, issue = normalized_id.rsplit("#", 1)
    if repository not in repositories or raw.get("repository") != repository:
        raise ProjectExecutorError("plan repository is outside the configured scope")
    try:
        if int(raw.get("issue", issue)) != int(issue):
            raise ProjectExecutorError("plan issue does not match task ID")
    except (TypeError, ValueError):
        raise ProjectExecutorError("plan issue must match task ID")
    plan_id = _safe_text(raw.get("plan_id"), "plan_id", 100)
    if not PLAN_ID_RE.fullmatch(plan_id):
        raise ProjectExecutorError("plan_id is invalid")
    base_ref = _safe_text(raw.get("base_ref"), "plan base_ref", 100)
    if base_ref != "main":
        raise ProjectExecutorError("project plans must use main as base_ref")
    branch = _safe_branch(raw.get("branch"))
    prompt = _safe_prompt(raw.get("prompt"))
    paths = raw.get("allowed_paths")
    if not isinstance(paths, list) or not paths or len(paths) > MAX_PATHS:
        raise ProjectExecutorError("allowed_paths must be a bounded non-empty list")
    allowed_paths = []
    for item in paths:
        normalized = _safe_relative_path(item)
        if not any(fnmatch.fnmatchcase(normalized, pattern) for pattern in repositories[repository]["allowed_path_patterns"]):
            raise ProjectExecutorError("plan path is outside the configured safe path allowlist")
        if normalized not in allowed_paths:
            allowed_paths.append(normalized)
    test_command = _validate_test_command(raw.get("test_command"))
    try:
        timeout = max(MIN_TIMEOUT_SECONDS, min(int(raw.get("timeout_seconds", 300)), MAX_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        raise ProjectExecutorError("timeout_seconds is invalid")
    return {
        "task_id": normalized_id,
        "repository": repository,
        "issue": int(issue),
        "plan_id": plan_id,
        "base_ref": base_ref,
        "branch": branch,
        "prompt": prompt,
        "allowed_paths": tuple(allowed_paths),
        "test_command": test_command,
        "commit_message": _safe_text(raw.get("commit_message"), "commit_message", 180),
        "pr_title": _safe_text(raw.get("pr_title"), "pr_title", 180),
        "pr_body": _safe_text(raw.get("pr_body"), "pr_body", MAX_TEXT_CHARS),
        "timeout_seconds": timeout,
    }


def validate_plan(task_id, raw, repositories):
    """Validate one planner-produced plan at the execution boundary."""
    return _validate_plan(task_id, raw, repositories)


def _load_dynamic_plans(path, repositories):
    path = Path(path).expanduser()
    if not path.exists():
        return {}
    value = _load_json(path)
    if not isinstance(value, dict) or int(value.get("schema_version", 0)) != CONFIG_SCHEMA_VERSION:
        raise ProjectExecutorError("dynamic plan file has an unsupported schema_version")
    raw_plans = value.get("plans")
    if not isinstance(raw_plans, dict) or len(raw_plans) > MAX_PLANS:
        raise ProjectExecutorError("dynamic plan file plans are invalid or too large")
    plans = {}
    for task_id, raw in raw_plans.items():
        plan = _validate_plan(task_id, raw, repositories)
        if plan["task_id"] in plans:
            raise ProjectExecutorError("duplicate dynamic project plan task ID")
        plans[plan["task_id"]] = plan
    return plans


def load_project_config(path):
    value = _load_json(path)
    if not isinstance(value, dict) or int(value.get("schema_version", 0)) != CONFIG_SCHEMA_VERSION:
        raise ProjectExecutorError("project executor config has an unsupported schema_version")
    if value.get("executor") != CONFIG_EXECUTOR:
        raise ProjectExecutorError("project executor config has an unsupported executor")
    codex_path = _safe_text(value.get("codex_path"), "codex_path", 500)
    if not Path(codex_path).is_absolute():
        raise ProjectExecutorError("codex_path must be absolute")
    codex_home = _safe_text(value.get("codex_home"), "codex_home", 500)
    if not Path(codex_home).is_absolute():
        raise ProjectExecutorError("codex_home must be absolute")
    workspace_root = _safe_text(value.get("workspace_root"), "workspace_root", 500)
    if not Path(workspace_root).is_absolute():
        raise ProjectExecutorError("workspace_root must be absolute")
    token_env = _safe_text(value.get("write_token_env"), "write_token_env", 120)
    if not re.fullmatch(r"GH_TOKEN_[A-Z0-9_]+_WRITE", token_env):
        raise ProjectExecutorError("write_token_env must be a dedicated *_WRITE variable")
    raw_repositories = value.get("repositories")
    if not isinstance(raw_repositories, dict) or not raw_repositories or len(raw_repositories) > MAX_REPOSITORIES:
        raise ProjectExecutorError("repositories must be a bounded object")
    repositories = {}
    for repository, raw in raw_repositories.items():
        if repository not in task_dispatch.ALLOWED_REPOSITORIES:
            raise ProjectExecutorError("repository is outside the authorized scope")
        repositories[repository] = _validate_repository(repository, raw)
    raw_plans = value.get("plans", {})
    if not isinstance(raw_plans, dict) or len(raw_plans) > MAX_PLANS:
        raise ProjectExecutorError("plans must be a bounded object")
    plans = {}
    for task_id, raw in raw_plans.items():
        plan = _validate_plan(task_id, raw, repositories)
        if plan["task_id"] in plans:
            raise ProjectExecutorError("duplicate project plan task ID")
        plans[plan["task_id"]] = plan
    plans_file = value.get("plans_file", "")
    if plans_file:
        plans_file = _safe_text(plans_file, "plans_file", 500)
        if not Path(plans_file).is_absolute():
            raise ProjectExecutorError("plans_file must be absolute")
        dynamic_plans = _load_dynamic_plans(plans_file, repositories)
        for task_id, plan in dynamic_plans.items():
            if task_id in plans:
                raise ProjectExecutorError("static and dynamic plans overlap")
            plans[task_id] = plan
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "executor": CONFIG_EXECUTOR,
        "codex_path": Path(codex_path),
        "codex_home": Path(codex_home),
        "workspace_root": Path(workspace_root),
        "write_token_env": token_env,
        "plans_file": Path(plans_file) if plans_file else None,
        "repositories": repositories,
        "plans": plans,
    }


def validate_changed_paths(paths, allowed_paths):
    """Return changed paths outside the exact plan allowlist."""
    allowed = set(allowed_paths)
    unexpected = []
    for value in paths:
        path = _clean(value, 500).strip().replace("\\", "/")
        if not path or path in INTERNAL_PATHS:
            continue
        if any(path.startswith(prefix) for prefix in INTERNAL_PATH_PREFIXES) or any(
            component in ("__pycache__", ".pytest_cache", ".mypy_cache")
            for component in path.split("/")
        ):
            continue
        if path not in allowed and path not in unexpected:
            unexpected.append(path)
    return sorted(unexpected)


def _ensure_directory(path, label):
    path = Path(path).expanduser()
    if path == Path("/") or len(path.parts) < 3:
        raise ProjectExecutorError(label + " is too broad")
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(str(path), 0o700)
        info = path.stat()
    except OSError as error:
        raise ProjectExecutorError(label + " unavailable: " + _clean(error, 220))
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ProjectExecutorError(label + " must be private and service-owned")
    return path


def _host_env(token_env):
    token = os.environ.get(token_env)
    if not token:
        raise ProjectExecutorError("write_token_unavailable")
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/var/lib/collaboration-hub"),
        "GH_TOKEN": token,
        "GIT_TERMINAL_PROMPT": "0",
        "GH_PAGER": "cat",
        "GIT_PAGER": "cat",
        # Git delegates HTTPS credentials to the host-side gh helper.  This
        # environment is never used for the Codex child.
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "!gh auth git-credential",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _codex_env(executable, codex_home, workspace):
    # codex_executor intentionally copies no GH_* variables or monitor token;
    # it only passes the login selector, workspace, and managed proxy/TLS vars.
    return codex_executor._executor_environment(executable, codex_home, workspace)


def _terminate(process):
    try:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _run(command, cwd, env, timeout, input_text=None, preserve_stdout=False):
    process = None
    try:
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd) if cwd else "/",
            env=env,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            close_fds=True,
            start_new_session=True,
        )
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
        bounded_stdout = stdout[:MAX_COMMAND_OUTPUT] if preserve_stdout else _summary(stdout)
        return process.returncode, bounded_stdout, _summary(stderr)
    except subprocess.TimeoutExpired:
        if process is not None:
            _terminate(process)
        return 124, "", "timeout"
    except OSError as error:
        if process is not None:
            _terminate(process)
        return 127, "", _summary(error)


def _git(command, cwd, env, timeout=120, preserve_stdout=False):
    return _run(["git"] + list(command), cwd, env, timeout, preserve_stdout=preserve_stdout)


def _gh(command, cwd, env, timeout=60):
    return _run(["gh"] + list(command), cwd, env, timeout)


def _ensure_mirror(repo_config, env):
    mirror = repo_config["mirror"]
    mirror.parent.mkdir(parents=True, exist_ok=True)
    if not (mirror / ".git").is_dir():
        code, out, err = _git(
            ["clone", "--branch", "main", "--single-branch", repo_config["remote"], str(mirror)],
            mirror.parent,
            env,
            timeout=240,
        )
        if code != 0:
            raise ProjectExecutorError("mirror_clone_failed: " + _summary(err or out, 800))
    else:
        code, out, err = _git(["remote", "set-url", "origin", repo_config["remote"]], mirror, env)
        if code != 0:
            raise ProjectExecutorError("mirror_remote_failed: " + _summary(err or out, 800))
    code, out, err = _git(["fetch", "--prune", "origin", "main"], mirror, env, timeout=240)
    if code != 0:
        raise ProjectExecutorError("mirror_fetch_failed: " + _summary(err or out, 800))
    code, out, err = _git(["reset", "--hard", "origin/main"], mirror, env, timeout=60)
    if code != 0:
        raise ProjectExecutorError("mirror_reset_failed: " + _summary(err or out, 800))
    return mirror


def _workspace_for(root, task_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id).strip("-")[:80]
    return root / safe


def _prepare_workspace(plan, repo_config, root, env):
    workspace = _workspace_for(root, plan["task_id"])
    workspace = _ensure_directory(workspace, "project workspace")
    repo_dir = workspace / "repo"
    if not (repo_dir / ".git").is_dir():
        # Use a local mirror so the Codex child never needs GitHub credentials.
        code, out, err = _git(
            ["clone", "--no-hardlinks", "--branch", plan["base_ref"], "--single-branch", str(repo_config["mirror"]), str(repo_dir)],
            workspace,
            env,
            timeout=240,
        )
        if code != 0:
            raise ProjectExecutorError("workspace_clone_failed: " + _summary(err or out, 800))
        code, out, err = _git(["remote", "set-url", "origin", repo_config["remote"]], repo_dir, env)
        if code != 0:
            raise ProjectExecutorError("workspace_remote_failed: " + _summary(err or out, 800))
    else:
        code, out, err = _git(["status", "--porcelain", "-uall"], repo_dir, env)
        if code != 0:
            raise ProjectExecutorError("workspace_status_failed: " + _summary(err or out, 800))
    code, out, err = _git(["show-ref", "--verify", "--quiet", "refs/heads/" + plan["branch"]], repo_dir, env)
    if code != 0:
        code, out, err = _git(["switch", "-c", plan["branch"], "origin/" + plan["base_ref"]], repo_dir, env)
        if code != 0:
            raise ProjectExecutorError("branch_create_failed: " + _summary(err or out, 800))
    else:
        code, out, err = _git(["switch", plan["branch"]], repo_dir, env)
        if code != 0:
            raise ProjectExecutorError("branch_switch_failed: " + _summary(err or out, 800))
    return workspace, repo_dir


def _changed_paths(repo_dir, env):
    # Keep porcelain newlines intact; the normal bounded text sanitizer folds
    # control whitespace for persisted logs and would join separate paths.
    code, out, err = _git(
        ["status", "--porcelain", "-uall"], repo_dir, env, preserve_stdout=True
    )
    if code != 0:
        raise ProjectExecutorError("changed_paths_failed: " + _summary(err or out, 800))
    paths = []
    for line in (out or "").splitlines():
        if len(line) < 4:
            continue
        value = line[3:].strip()
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        paths.append(value)
    return paths


def _run_project_tests(repo_dir, plan, workspace, timeout):
    command = [sys.executable] + list(plan["test_command"])[1:]
    environment = {
        "PATH": os.defpath,
        "HOME": str(workspace),
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "PYTHONIOENCODING": "utf-8",
        "NO_COLOR": "1",
    }
    return _run(command, repo_dir, environment, min(120, timeout))


def _existing_pr(repo, branch, env):
    code, out, err = _gh(
        ["pr", "list", "--repo", repo, "--head", branch, "--state", "all", "--json", "number,url,state,isDraft"],
        None,
        env,
        timeout=60,
    )
    if code != 0:
        return None
    try:
        rows = json.loads(out or "[]")
    except ValueError:
        return None
    if isinstance(rows, list) and rows:
        return rows[0] if isinstance(rows[0], dict) else None
    return None


def _create_pr(repo, branch, plan, env):
    code, out, err = _gh(
        [
            "pr",
            "create",
            "--repo",
            repo,
            "--base",
            plan["base_ref"],
            "--head",
            branch,
            "--draft",
            "--title",
            plan["pr_title"],
            "--body",
            plan["pr_body"],
        ],
        None,
        env,
        timeout=90,
    )
    if code != 0:
        raise ProjectExecutorError("draft_pr_failed: " + _summary(err or out, 900))
    match = re.search(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+", out or "")
    if not match:
        raise ProjectExecutorError("draft_pr_url_missing")
    return match.group(0)


def _check_pr(repo, pr_url, env, timeout=CHECK_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    last = []
    while time.monotonic() <= deadline:
        code, out, err = _gh(
            ["pr", "checks", pr_url, "--repo", repo, "--json", "name,state,bucket"],
            None,
            env,
            timeout=60,
        )
        try:
            checks = json.loads(out or "[]")
        except ValueError:
            checks = []
        if isinstance(checks, list):
            last = checks
        if checks:
            buckets = [str(item.get("bucket") or "").lower() for item in checks if isinstance(item, dict)]
            if any(bucket in CHECK_PENDING for bucket in buckets):
                time.sleep(CHECK_POLL_SECONDS)
                continue
            return {"status": "passed" if all(bucket == "pass" for bucket in buckets) else "failed", "checks": checks}
        # A repository without checks is still reviewable; do not block PR delivery.
        if code == 0:
            return {"status": "no_checks", "checks": []}
        time.sleep(CHECK_POLL_SECONDS)
    return {"status": "pending_timeout", "checks": last}


def _blocked(store, task_id, reason, detail=""):
    record = store.get(task_id)
    if record.get("target") != "server":
        raise ProjectExecutorError("task target is not server")
    if record.get("status") in task_dispatch.ACTIVE_STATUSES:
        raise ProjectExecutorError("active task will not be run again")
    if record.get("status") not in ("PENDING", "FAILED"):
        raise task_dispatch.DispatchError("task status cannot be executed: " + str(record.get("status")))
    store.claim(task_id, "server", allow_retry=record.get("status") == "FAILED")
    store.start(task_id, "server")
    result = {"executor": EXECUTOR_NAME, "task_id": task_id, "reason": _clean(reason, 160)}
    if detail:
        result["detail"] = _summary(detail, 600)
    try:
        return store.submit(task_id, "server", "BLOCKED", result=result, log_summary="project executor blocked: " + _summary(reason, 300))
    finally:
        store.set_runtime_status("BLOCKED_EXECUTOR")


def run_project_task(store, task_id, config_path, allow_retry=False):
    task_id = task_dispatch._require_task_id(task_id)
    config = load_project_config(config_path)
    plan = config["plans"].get(task_id)
    if not plan:
        raise ProjectExecutorError("no exact trusted project plan for task ID")
    record = store.get(task_id)
    if record.get("target") != "server" or record.get("target_verified") is not True:
        raise ProjectExecutorError("task requires an explicitly verified server target")
    if os.geteuid() == 0:
        return _blocked(store, task_id, "root_user")
    try:
        executable = codex_executor._validate_codex_path(config["codex_path"])
        codex_home = codex_executor._check_login_home(config["codex_home"])
        root = _ensure_directory(config["workspace_root"], "project workspace root")
    except (codex_executor.ExecutorError, ProjectExecutorError) as error:
        return _blocked(store, task_id, "executor_environment_unavailable", error)
    repo_config = config["repositories"][plan["repository"]]
    try:
        host_env = _host_env(config["write_token_env"])
    except ProjectExecutorError as error:
        return _blocked(store, task_id, "write_token_unavailable", error)
    with codex_executor._executor_lock(root):
        if store.is_paused():
            return _blocked(store, task_id, "paused")
        current = store.get(task_id)
        if current.get("status") in task_dispatch.ACTIVE_STATUSES:
            raise task_dispatch.DispatchError("active task will not be run again")
        if current.get("status") not in ("PENDING", "FAILED"):
            raise task_dispatch.DispatchError("task status cannot be executed: " + str(current.get("status")))
        if current.get("status") == "FAILED" and not allow_retry:
            raise task_dispatch.DispatchError("failed project task requires explicit retry")
        store.claim(task_id, "server", allow_retry=allow_retry and current.get("status") == "FAILED")
        store.start(task_id, "server")
        store.set_runtime_status("RUNNER_WORKING")
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        workspace = None
        repo_dir = None
        try:
            _ensure_mirror(repo_config, host_env)
            workspace, repo_dir = _prepare_workspace(plan, repo_config, root, host_env)
            existing = _existing_pr(plan["repository"], plan["branch"], host_env)
            if existing and str(existing.get("state", "")).upper() in ("OPEN", "CLOSED"):
                # Recover a crash after push/PR creation without creating a duplicate.
                result = {
                    "executor": EXECUTOR_NAME,
                    "task_id": task_id,
                    "plan_id": plan["plan_id"],
                    "repository": plan["repository"],
                    "branch": plan["branch"],
                    "recovered_pr": existing,
                    "started_at": started_at,
                    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                status = "REVIEW" if str(existing.get("state", "")).upper() == "OPEN" else "FAILED"
                return store.submit(task_id, "server", status, result=result, pr=existing.get("url", ""), log_summary="recovered existing Draft PR")
            codex_env = _codex_env(executable, codex_home, repo_dir)
            prompt = plan["prompt"] + "\nOnly modify the files listed in the trusted plan; do not run git push or create a PR."
            command = [
                str(executable),
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--sandbox",
                "workspace-write",
                "--color",
                "never",
                "--json",
                "--cd",
                str(repo_dir),
                "--output-last-message",
                str(workspace / "codex.last-message.txt"),
                prompt,
            ]
            stdout_path = workspace / "codex.stdout.log"
            stderr_path = workspace / "codex.stderr.log"
            with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
                process = subprocess.Popen(
                    command,
                    cwd="/",
                    env=codex_env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    close_fds=True,
                    start_new_session=True,
                )
                deadline = time.monotonic() + plan["timeout_seconds"]
                interrupted = ""
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        interrupted = "timeout"
                        _terminate(process)
                        break
                    if store.is_paused():
                        interrupted = "paused"
                        _terminate(process)
                        break
                    time.sleep(0.2)
                process.wait(timeout=3)
            code = process.returncode
            changed = _changed_paths(repo_dir, host_env)
            unexpected = validate_changed_paths(changed, plan["allowed_paths"])
            result = {
                "executor": EXECUTOR_NAME,
                "task_id": task_id,
                "plan_id": plan["plan_id"],
                "repository": plan["repository"],
                "base_ref": plan["base_ref"],
                "branch": plan["branch"],
                "started_at": started_at,
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "codex_exit_code": code,
                "changed_paths": changed[:MAX_PATHS],
                "stdout_summary": _summary(stdout_path.read_text(encoding="utf-8", errors="replace"), 5000),
                "stderr_summary": _summary(stderr_path.read_text(encoding="utf-8", errors="replace"), 5000),
            }
            if interrupted:
                result["reason"] = interrupted
                return store.submit(task_id, "server", "BLOCKED" if interrupted == "paused" else "FAILED", result=result, log_summary="Codex project task: " + interrupted)
            if code != 0:
                result["reason"] = "codex_exit_nonzero"
                return store.submit(task_id, "server", "FAILED", result=result, log_summary="Codex project task exited nonzero")
            if unexpected or not changed:
                result["reason"] = "unexpected_or_empty_change"
                result["unexpected_paths"] = unexpected[:MAX_PATHS]
                return store.submit(task_id, "server", "BLOCKED", result=result, log_summary="project task change allowlist blocked")
            test_code, test_out, test_err = _run_project_tests(repo_dir, plan, workspace, plan["timeout_seconds"])
            result["test_exit_code"] = test_code
            result["test_stdout_summary"] = test_out
            result["test_stderr_summary"] = test_err
            if test_code != 0:
                result["reason"] = "project_tests_failed"
                return store.submit(task_id, "server", "FAILED", result=result, log_summary="project tests failed")
            code, out, err = _git(["add", "--"] + list(plan["allowed_paths"]), repo_dir, host_env)
            if code != 0:
                result["reason"] = "git_add_failed"
                result["git_error"] = _summary(err or out)
                return store.submit(task_id, "server", "FAILED", result=result, log_summary="git add failed")
            code, out, err = _git(
                ["-c", "user.name=Codex Server", "-c", "user.email=codex-server@users.noreply.github.com", "commit", "-m", plan["commit_message"]],
                repo_dir,
                host_env,
            )
            if code != 0:
                result["reason"] = "git_commit_failed"
                result["git_error"] = _summary(err or out)
                return store.submit(task_id, "server", "FAILED", result=result, log_summary="git commit failed")
            code, sha, err = _git(["rev-parse", "HEAD"], repo_dir, host_env)
            commit = (sha or "").strip()[:80]
            result["commit"] = commit
            if code != 0 or not commit:
                result["reason"] = "git_sha_failed"
                return store.submit(task_id, "server", "FAILED", result=result, log_summary="git sha lookup failed")
            code, out, err = _git(["push", "--set-upstream", "origin", plan["branch"]], repo_dir, host_env, timeout=180)
            if code != 0:
                result["reason"] = "git_push_failed"
                result["git_error"] = _summary(err or out)
                return store.submit(task_id, "server", "FAILED", result=result, commit=commit, log_summary="git push failed")
            pr_url = _create_pr(plan["repository"], plan["branch"], plan, host_env)
            result["pr"] = pr_url
            result["checks"] = _check_pr(plan["repository"], pr_url, host_env)
            return store.submit(task_id, "server", "REVIEW", result=result, commit=commit, pr=pr_url, log_summary="Codex project task delivered Draft PR")
        except (ProjectExecutorError, OSError, ValueError) as error:
            result = {
                "executor": EXECUTOR_NAME,
                "task_id": task_id,
                "plan_id": plan["plan_id"],
                "reason": "project_executor_error",
                "detail": _summary(error, 800),
                "workspace": str(workspace) if workspace else "",
            }
            return store.submit(task_id, "server", "FAILED", result=result, log_summary="project executor failed")
        finally:
            store.set_runtime_status("QUEUE_ONLY")


def run_next_project_task(store, config_path):
    config = load_project_config(config_path)
    for candidate in store.list_tasks(target="server", status="PENDING"):
        if candidate["task_id"] not in config["plans"]:
            continue
        result = run_project_task(store, candidate["task_id"], config_path)
        return {
            "runtime_status": store.summary()["runtime_status"],
            "selected_task_id": candidate["task_id"],
            "action": EXECUTOR_NAME,
            "execution": "completed",
            "task": result,
        }
    return {
        "runtime_status": store.summary()["runtime_status"],
        "selected_task_id": "",
        "action": EXECUTOR_NAME,
        "reason": "no pending server task with an exact trusted project plan",
    }


def _json_print(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    sub = parser.add_subparsers(dest="command")
    validate = sub.add_parser("validate-config")
    validate.add_argument("--config", default=DEFAULT_CONFIG_FILE)
    run = sub.add_parser("run")
    run.add_argument("--task-id", required=True)
    run.add_argument("--config", default=DEFAULT_CONFIG_FILE)
    run.add_argument("--retry", action="store_true")
    next_run = sub.add_parser("run-next")
    next_run.add_argument("--config", default=DEFAULT_CONFIG_FILE)
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.error("a command is required")
    try:
        if args.command == "validate-config":
            config = load_project_config(args.config)
            _json_print({"valid": True, "executor": config["executor"], "task_ids": sorted(config["plans"])})
        else:
            store = task_dispatch.DispatchStore(args.state_file)
            if args.command == "run":
                _json_print(run_project_task(store, args.task_id, args.config, allow_retry=args.retry))
            else:
                _json_print(run_next_project_task(store, args.config))
        return 0
    except (ProjectExecutorError, task_dispatch.DispatchError, OSError, ValueError) as error:
        sys.stderr.write("project_codex_executor: " + _clean(error, 600) + "\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
