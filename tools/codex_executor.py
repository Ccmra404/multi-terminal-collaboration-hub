#!/usr/bin/env python3
"""Bounded, opt-in Codex CLI executor for explicitly assigned server tasks.

This module is an on-demand adapter for :mod:`task_dispatch`.  It deliberately
does not read GitHub Issue bodies.  A task can run only when its stable task ID
has an exact, maintainer-confirmed plan in a local allowlist.  The command is
not wired to the production timer; a future enablement must be approved once
as a separate deployment change.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux is the supported server
    fcntl = None

try:
    import resource
except ImportError:  # pragma: no cover - Windows does not provide resource
    resource = None

try:
    import task_dispatch
except ImportError:  # pragma: no cover - run-from-repository-root convenience
    _tools = str(Path(__file__).resolve().parent)
    if _tools not in sys.path:
        sys.path.insert(0, _tools)
    import task_dispatch


EXECUTOR_NAME = "codex_dev_task-v1"
CONFIG_SCHEMA_VERSION = 1
CONFIG_EXECUTOR = "codex_dev_task"
DEFAULT_CONFIG_FILE = "config/codex-executor.example.json"
DEFAULT_WORKSPACE_ROOT = "/var/lib/collaboration-hub/codex-workspaces"
DEFAULT_CODEX_PATH = "/www/server/nodejs/v24.15.0/bin/codex"
MAX_CONFIG_BYTES = 256 * 1024
MAX_PROMPT_CHARS = 4000
MAX_PLANS = 100
MAX_ALLOWED_FILES = 40
MAX_ARTIFACT_BYTES = 256 * 1024
MAX_LOG_CHARS = 5000
MAX_TIMEOUT_SECONDS = 600
MIN_TIMEOUT_SECONDS = 1
FIXED_TEST_COMMAND = ("python3", "-m", "unittest", "discover", "-v")
INTERNAL_FILES = frozenset(
    (
        "codex.stdout.log",
        "codex.stderr.log",
        "codex.last-message.txt",
        "test.stdout.log",
        "test.stderr.log",
    )
)
SENSITIVE_PROMPT = re.compile(
    r"(?i)(?:\b(?:token|password|secret|root|sudo|ssh|production|deploy|deployment|"
    r"firewall|release|merge|device|credential|authorization)\b|/etc/|/root/|"
    r"~/.ssh|public\s+internet|external\s+network|issue\s+body|shell\s+command)"
)
AUTH_FAILURE = re.compile(
    r"(?i)(?:not\s+logged\s+in|login\s+required|authentication|unauthorized|"
    r"invalid\s+(?:api\s+)?key|access\s+token)"
)
SECRET_VALUE = re.compile(
    r"(?i)(?:authorization|bearer|token|password|secret|api[_ -]?key|ghp_|github_pat_|sk-)"
    r"\s*[:=]\s*[^\s,;]+"
)


class ExecutorError(ValueError):
    """Expected plan, environment, or execution boundary failure."""


def _clean(value, maximum=240):
    try:
        return task_dispatch._clean(value, maximum)
    except Exception:
        text = "" if value is None else str(value)
        return text.replace("\x00", "")[:maximum]


def _iso(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return task_dispatch._iso(value)


def _bounded_summary(value, maximum=MAX_LOG_CHARS):
    text = _clean(value, maximum)
    return SECRET_VALUE.sub("<redacted>", text)


def _load_json(path):
    path = Path(path).expanduser()
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise ExecutorError("executor config exceeds size limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except ExecutorError:
        raise
    except (OSError, ValueError) as error:
        raise ExecutorError("cannot read executor config: " + _clean(error, 240))
    return value


def _safe_relative_file(value):
    value = _clean(value, 180).strip()
    if not value or "\x00" in value:
        raise ExecutorError("allowed_files contains an empty or NUL path")
    path = Path(value)
    if path.is_absolute() or value.startswith("~") or ".." in path.parts:
        raise ExecutorError("allowed_files must stay inside the workspace")
    if value.endswith("/") or path.name in ("", ".", ".."):
        raise ExecutorError("allowed_files must contain files, not directories")
    return "/".join(path.parts)


def _safe_base_ref(value):
    value = _clean(value, 120).strip()
    if not value or value.startswith("-") or ".." in value or any(char.isspace() for char in value):
        raise ExecutorError("plan base_ref is invalid")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", value):
        raise ExecutorError("plan base_ref contains unsupported characters")
    return value


def _validate_prompt(value):
    value = _clean(value, MAX_PROMPT_CHARS).strip()
    if not value:
        raise ExecutorError("trusted plan prompt is empty")
    if "\x00" in value:
        raise ExecutorError("trusted plan prompt contains NUL")
    if len(value) > MAX_PROMPT_CHARS:
        raise ExecutorError("trusted plan prompt is too long")
    if SENSITIVE_PROMPT.search(value):
        raise ExecutorError("trusted plan prompt contains a blocked sensitive action")
    return value


def _validate_codex_path(value):
    value = _clean(value, 400).strip()
    if not value or not Path(value).is_absolute():
        raise ExecutorError("codex_path must be an absolute path")
    path = Path(value)
    try:
        mode = path.stat().st_mode
    except OSError as error:
        raise ExecutorError("codex_path is unavailable: " + _clean(error, 200))
    if not stat.S_ISREG(mode) or not os.access(str(path), os.X_OK):
        raise ExecutorError("codex_path is not an executable file")
    return path


def _validate_plan(task_id, raw):
    if not isinstance(raw, dict):
        raise ExecutorError("plan must be an object")
    if raw.get("trusted") is not True:
        raise ExecutorError("plan requires trusted=true confirmation")
    try:
        normalized_id = task_dispatch._require_task_id(task_id)
    except Exception as error:
        raise ExecutorError("plan task ID is invalid: " + _clean(error, 200))
    repository, issue = normalized_id.rsplit("#", 1)
    if raw.get("repository") != repository:
        raise ExecutorError("plan repository does not match task ID")
    try:
        if int(raw.get("issue", issue)) != int(issue):
            raise ExecutorError("plan issue does not match task ID")
    except (TypeError, ValueError):
        # The issue field is optional; if present it must be numeric and exact.
        if "issue" in raw:
            raise ExecutorError("plan issue does not match task ID")
    plan_id = _clean(raw.get("plan_id"), 100).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", plan_id):
        raise ExecutorError("plan_id is invalid")
    prompt = _validate_prompt(raw.get("prompt"))
    if raw.get("workspace_kind") != "isolated":
        raise ExecutorError("only isolated workspace plans are supported")
    base_ref = _safe_base_ref(raw.get("base_ref"))
    allowed = raw.get("allowed_files")
    if not isinstance(allowed, list) or not allowed or len(allowed) > MAX_ALLOWED_FILES:
        raise ExecutorError("allowed_files must be a bounded non-empty list")
    allowed_files = []
    for item in allowed:
        normalized = _safe_relative_file(item)
        if normalized in INTERNAL_FILES or normalized.startswith(".cache/"):
            raise ExecutorError("allowed_files overlaps executor internals")
        if normalized not in allowed_files:
            allowed_files.append(normalized)
    if raw.get("run_local_unittest") is not True:
        raise ExecutorError("plan must opt into the fixed local unittest check")
    try:
        timeout = int(raw.get("timeout_seconds", 120))
    except (TypeError, ValueError):
        raise ExecutorError("plan timeout_seconds is invalid")
    timeout = max(MIN_TIMEOUT_SECONDS, min(timeout, MAX_TIMEOUT_SECONDS))
    return {
        "task_id": normalized_id,
        "repository": repository,
        "issue": int(issue),
        "plan_id": plan_id,
        "base_ref": base_ref,
        "workspace_kind": "isolated",
        "prompt": prompt,
        "allowed_files": tuple(allowed_files),
        "run_local_unittest": True,
        "timeout_seconds": timeout,
    }


def load_plans(path):
    """Load exact, locally reviewed task plans; never fetch or parse Issue text."""
    value = _load_json(path)
    if not isinstance(value, dict) or int(value.get("schema_version", 0)) != CONFIG_SCHEMA_VERSION:
        raise ExecutorError("executor config has an unsupported schema_version")
    if value.get("executor") != CONFIG_EXECUTOR:
        raise ExecutorError("executor config has an unsupported executor")
    try:
        codex_path = _validate_codex_path(value.get("codex_path", DEFAULT_CODEX_PATH))
    except ExecutorError:
        # Config validation is useful before the binary is installed.  Keep
        # the path in the returned metadata only when it is syntactically safe.
        raw_path = _clean(value.get("codex_path", DEFAULT_CODEX_PATH), 400).strip()
        if not raw_path or not Path(raw_path).is_absolute():
            raise
        codex_path = Path(raw_path)
    codex_home = _clean(value.get("codex_home"), 500).strip()
    if not codex_home or not Path(codex_home).is_absolute():
        raise ExecutorError("codex_home must be an absolute path")
    raw_plans = value.get("plans")
    if not isinstance(raw_plans, dict) or not raw_plans or len(raw_plans) > MAX_PLANS:
        raise ExecutorError("plans must be a bounded non-empty object")
    plans = {}
    for task_id, raw in raw_plans.items():
        plan = _validate_plan(task_id, raw)
        if plan["task_id"] in plans:
            raise ExecutorError("duplicate plan task ID")
        plans[plan["task_id"]] = plan
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "executor": CONFIG_EXECUTOR,
        "codex_path": codex_path,
        "codex_home": Path(codex_home),
        "plans": plans,
    }


def _ensure_owned_directory(path, label):
    path = Path(path).expanduser()
    if path == Path("/") or len(path.parts) < 3:
        raise ExecutorError(label + " is too broad")
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(str(path), 0o700)
        info = path.stat()
    except OSError as error:
        raise ExecutorError(label + " is unavailable: " + _clean(error, 200))
    if info.st_uid != os.geteuid():
        raise ExecutorError(label + " is not owned by the executor user")
    if info.st_mode & 0o077:
        raise ExecutorError(label + " must not be group/world accessible")
    return path


def _check_login_home(path):
    path = Path(path).expanduser()
    try:
        info = path.stat()
    except OSError:
        raise ExecutorError("codex_home_unavailable")
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ExecutorError("codex_home_unavailable")
    auth = path / "auth.json"
    try:
        auth_info = auth.stat()
    except OSError:
        raise ExecutorError("codex_home_unavailable")
    if not stat.S_ISREG(auth_info.st_mode) or auth_info.st_uid != os.geteuid() or auth_info.st_mode & 0o077:
        raise ExecutorError("codex_home_unavailable")
    return path


def _workspace_path(root, task_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id).strip("-")[:80]
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]
    return root / (safe + "-" + digest)


def _within(parent, child):
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def _validate_workspace_files(workspace, allowed_files):
    allowed = set(allowed_files)
    artifacts = []
    unexpected = []
    for path in workspace.rglob("*"):
        relative = path.relative_to(workspace).as_posix()
        if path.is_symlink():
            unexpected.append(relative + " (symlink)")
            continue
        if path.is_dir():
            if relative == ".cache" or relative.startswith(".cache/") or relative == "__pycache__":
                continue
            unexpected.append(relative + " (directory)")
            continue
        if relative in INTERNAL_FILES or relative.startswith(".cache/"):
            continue
        if relative == "__pycache__" or relative.startswith("__pycache__/"):
            if path.suffix == ".pyc":
                continue
            unexpected.append(relative + " (non-pyc cache artifact)")
            continue
        if relative not in allowed:
            unexpected.append(relative)
            continue
        try:
            size = path.stat().st_size
        except OSError as error:
            unexpected.append(relative + " (unreadable)")
            continue
        if size > MAX_ARTIFACT_BYTES:
            unexpected.append(relative + " (too large)")
            continue
        artifacts.append({"path": relative, "bytes": size})
    missing = sorted(allowed - set(item["path"] for item in artifacts))
    return sorted(artifacts, key=lambda item: item["path"]), sorted(unexpected), missing


def _resource_limits():
    if resource is None:
        return
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (MAX_TIMEOUT_SECONDS + 5, MAX_TIMEOUT_SECONDS + 5))
        resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))
        # Codex's Node/Rust code-mode host opens more descriptors and helper
        # processes than a tiny script.  Keep file output and CPU bounded, but
        # leave address-space/process-count policy to the service sandbox;
        # overly small RLIMIT_AS/NPROC values make the host abort with SIGTRAP
        # before it can run a harmless workspace command.
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    except (AttributeError, OSError, ValueError):
        pass


def _terminate(process):
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, AttributeError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=2)
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except Exception:
            pass


def _bounded_file(path, maximum=MAX_LOG_CHARS):
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(max(0, Path(path).stat().st_size - maximum * 2))
            return _bounded_summary(handle.read(), maximum)
    except OSError as error:
        return _bounded_summary(error, maximum)


def _executor_environment(codex_path, codex_home, workspace):
    cache = workspace / ".cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    env = {
        "PATH": str(codex_path.parent) + os.pathsep + os.defpath,
        "HOME": str(workspace),
        "CODEX_HOME": str(codex_home),
        "XDG_CACHE_HOME": str(cache),
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "PYTHONIOENCODING": "utf-8",
        "NO_COLOR": "1",
    }
    # Keep only network/TLS routing supplied by the managed environment.  Do
    # not pass tokens, GitHub variables, or the caller's general environment.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


@contextmanager
def _executor_lock(workspace_root):
    path = workspace_root / ".codex-executor.lock"
    handle = path.open("a+")
    try:
        os.chmod(str(path), 0o600)
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ExecutorError("executor_busy")
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _blocked_record(store, task_id, reason, detail=""):
    record = store.get(task_id)
    if record.get("target") != "server":
        raise ExecutorError("task target is not server")
    if record.get("status") in task_dispatch.ACTIVE_STATUSES:
        raise ExecutorError("active task will not be run again; recover it first")
    if record.get("status") not in ("PENDING", "FAILED"):
        raise task_dispatch.DispatchError("task status cannot be executed: " + str(record.get("status")))
    claimed = store.claim(task_id, "server", allow_retry=record.get("status") == "FAILED")
    store.start(task_id, "server")
    result = {
        "executor": EXECUTOR_NAME,
        "task_id": task_id,
        "reason": _clean(reason, 120),
    }
    if detail:
        result["detail"] = _bounded_summary(detail, 600)
    try:
        return store.submit(
            task_id,
            "server",
            "BLOCKED",
            result=result,
            log_summary="codex executor blocked: " + _bounded_summary(reason, 300),
        )
    finally:
        store.set_runtime_status("BLOCKED_EXECUTOR")


def _fixed_unittest(workspace, timeout):
    stdout_path = workspace / "test.stdout.log"
    stderr_path = workspace / "test.stderr.log"
    environment = _executor_environment(Path(sys.executable), Path(os.environ.get("CODEX_HOME", workspace)), workspace)
    # The test environment must not inherit the Codex login.  It only runs the
    # generated local files and receives the same bounded resource limits.
    environment.pop("CODEX_HOME", None)
    environment["PATH"] = os.defpath
    process = None
    started = time.monotonic()
    interrupted = ""
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        try:
            process = subprocess.Popen(
                list(FIXED_TEST_COMMAND),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                cwd=str(workspace),
                env=environment,
                close_fds=True,
                start_new_session=True,
                preexec_fn=_resource_limits if resource is not None else None,
            )
            while process.poll() is None:
                if time.monotonic() - started >= timeout:
                    interrupted = "test_timeout"
                    _terminate(process)
                    break
                time.sleep(0.05)
            process.wait(timeout=2)
        except Exception as error:
            interrupted = interrupted or "test_spawn_error"
            if process is not None:
                _terminate(process)
            return {
                "exit_code": process.returncode if process is not None else 127,
                "interrupted": interrupted,
                "stdout": _bounded_file(stdout_path),
                "stderr": _bounded_summary(error),
            }
    return {
        "exit_code": process.returncode if process is not None else 127,
        "interrupted": interrupted,
        "stdout": _bounded_file(stdout_path),
        "stderr": _bounded_file(stderr_path),
    }


def run_codex_task(
    store,
    task_id,
    config_path,
    workspace_root=DEFAULT_WORKSPACE_ROOT,
    timeout=None,
    codex_path=None,
    codex_home=None,
    allow_retry=False,
):
    """Run one exact server task through a non-interactive Codex CLI child."""
    task_id = task_dispatch._require_task_id(task_id)
    config = load_plans(config_path)
    plan = config["plans"].get(task_id)
    if not plan:
        raise ExecutorError("no exact trusted plan for task ID")
    record = store.get(task_id)
    if record.get("target") != "server" or record.get("target_verified") is not True:
        raise ExecutorError("task requires an explicitly verified server target")
    if os.geteuid() == 0:
        return _blocked_record(store, task_id, "root_user")
    try:
        executable = _validate_codex_path(codex_path or config["codex_path"])
    except ExecutorError as error:
        return _blocked_record(store, task_id, "codex_unavailable", error)
    try:
        login_home = _check_login_home(codex_home or config["codex_home"])
    except ExecutorError as error:
        return _blocked_record(store, task_id, "codex_home_unavailable", error)
    try:
        root = _ensure_owned_directory(workspace_root, "workspace_root")
    except ExecutorError as error:
        return _blocked_record(store, task_id, "workspace_unavailable", error)
    if timeout is None:
        timeout = plan["timeout_seconds"]
    try:
        timeout = max(MIN_TIMEOUT_SECONDS, min(int(timeout), MAX_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        raise ExecutorError("timeout must be an integer")
    workspace = _workspace_path(root, task_id)
    workspace = _ensure_owned_directory(workspace, "task workspace")
    if not _within(root, workspace):
        raise ExecutorError("task workspace escaped workspace_root")
    try:
        with _executor_lock(root):
            if store.is_paused():
                return _blocked_record(store, task_id, "paused")
            current = store.get(task_id)
            if current.get("status") in task_dispatch.ACTIVE_STATUSES:
                raise task_dispatch.DispatchError("active task will not be run again; recover it first")
            if current.get("status") not in ("PENDING", "FAILED"):
                raise task_dispatch.DispatchError("task status cannot be executed: " + str(current.get("status")))
            store.claim(task_id, "server", allow_retry=allow_retry and current.get("status") == "FAILED")
            store.start(task_id, "server")
            store.set_runtime_status("RUNNER_WORKING")
            started_at = _iso()
            stdout_path = workspace / "codex.stdout.log"
            stderr_path = workspace / "codex.stderr.log"
            last_message = workspace / "codex.last-message.txt"
            environment = _executor_environment(executable, login_home, workspace)
            command = [
                str(executable),
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--sandbox",
                "workspace-write",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--json",
                "--cd",
                str(workspace),
                "--output-last-message",
                str(last_message),
                plan["prompt"],
            ]
            process = None
            interrupted = ""
            started = time.monotonic()
            heartbeat_at = started
            with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
                try:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=stderr,
                        cwd="/",
                        env=environment,
                        close_fds=True,
                        start_new_session=True,
                        # Do not apply Python's pre-exec rlimits to Codex's
                        # Node/Rust host: its sandbox already bounds writes,
                        # while a host-level virtual-memory/process cap can
                        # abort before the model executes.  The parent keeps
                        # the wall-clock/stop-switch bound; the generated
                        # unittest below still uses _resource_limits.
                        preexec_fn=None,
                    )
                    while process.poll() is None:
                        elapsed = time.monotonic() - started
                        if elapsed >= timeout:
                            interrupted = "timeout"
                            _terminate(process)
                            break
                        if store.is_paused():
                            interrupted = "paused"
                            _terminate(process)
                            break
                        if time.monotonic() - heartbeat_at >= 10:
                            store.heartbeat(task_id, "server")
                            heartbeat_at = time.monotonic()
                        time.sleep(0.1)
                    process.wait(timeout=2)
                except Exception as error:
                    interrupted = interrupted or "spawn_error"
                    if process is not None:
                        _terminate(process)
                    spawn_error = _bounded_summary(error, 600)
                else:
                    spawn_error = ""
            code = process.returncode if process is not None else 127
            stdout_summary = _bounded_file(stdout_path)
            stderr_summary = _bounded_file(stderr_path)
            if spawn_error:
                stderr_summary = _bounded_summary(spawn_error + " " + stderr_summary)
            artifacts, unexpected, missing = _validate_workspace_files(workspace, plan["allowed_files"])
            result = {
                "executor": EXECUTOR_NAME,
                "task_id": task_id,
                "plan_id": plan["plan_id"],
                "repository": plan["repository"],
                "base_ref": plan["base_ref"],
                "attempt": store.get(task_id).get("attempt", 0),
                "workspace": str(workspace),
                "started_at": started_at,
                "finished_at": _iso(),
                "exit_code": code,
                "artifacts": artifacts,
                "stdout_summary": stdout_summary,
                "stderr_summary": stderr_summary,
            }
            status = "DONE"
            reason = ""
            test_result = None
            if interrupted:
                reason = interrupted
                status = "BLOCKED" if interrupted == "paused" else "FAILED"
            elif code != 0:
                reason = "codex_login_unavailable" if AUTH_FAILURE.search(stderr_summary + " " + stdout_summary) else "codex_exit_nonzero"
                status = "BLOCKED" if reason == "codex_login_unavailable" else "FAILED"
            elif unexpected:
                reason = "unexpected_artifact"
                status = "BLOCKED"
            elif missing:
                reason = "missing_allowlisted_artifact"
                status = "FAILED"
            else:
                test_result = _fixed_unittest(workspace, min(60, timeout))
                result["test_exit_code"] = test_result["exit_code"]
                result["test_stdout_summary"] = test_result["stdout"]
                result["test_stderr_summary"] = test_result["stderr"]
                if test_result.get("interrupted"):
                    reason = test_result["interrupted"]
                    status = "FAILED"
                elif test_result["exit_code"] != 0:
                    reason = "local_unittest_failed"
                    status = "FAILED"
            if reason:
                result["reason"] = reason
            log_summary = "Codex CLI " + status.lower()
            if reason:
                log_summary += ": " + reason
            if unexpected:
                result["unexpected_artifacts"] = unexpected[:20]
            if missing:
                result["missing_artifacts"] = missing[:20]
            try:
                return store.submit(
                    task_id,
                    "server",
                    status,
                    result=result,
                    log_summary=_bounded_summary(log_summary, 1000),
                )
            finally:
                store.set_runtime_status("BLOCKED_EXECUTOR" if status == "BLOCKED" else "QUEUE_ONLY")
    except ExecutorError:
        raise


def run_next_codex_task(store, config_path, workspace_root=DEFAULT_WORKSPACE_ROOT, timeout=None):
    """Select the first pending server row that has an exact trusted plan."""
    config = load_plans(config_path)
    candidates = store.list_tasks(target="server", status="PENDING")
    for candidate in candidates:
        if candidate["task_id"] not in config["plans"]:
            continue
        result = run_codex_task(store, candidate["task_id"], config_path, workspace_root, timeout=timeout)
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
        "reason": "no pending server task with an exact trusted plan",
    }


def _json_print(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", default=task_dispatch.DEFAULT_STATE_FILE)
    sub = parser.add_subparsers(dest="command")
    validate = sub.add_parser("validate-config", help="validate a local trusted-plan allowlist")
    validate.add_argument("--config", default=DEFAULT_CONFIG_FILE)
    run = sub.add_parser("run", help="run one exact server task through Codex CLI")
    run.add_argument("--task-id", required=True)
    run.add_argument("--config", default=DEFAULT_CONFIG_FILE)
    run.add_argument("--workspace-root", default=DEFAULT_WORKSPACE_ROOT)
    run.add_argument("--timeout", type=int)
    run.add_argument("--retry", action="store_true")
    next_run = sub.add_parser("run-next", help="run the first pending planned server task")
    next_run.add_argument("--config", default=DEFAULT_CONFIG_FILE)
    next_run.add_argument("--workspace-root", default=DEFAULT_WORKSPACE_ROOT)
    next_run.add_argument("--timeout", type=int)
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.error("a command is required")
    try:
        if args.command == "validate-config":
            value = load_plans(args.config)
            _json_print({"valid": True, "executor": value["executor"], "task_ids": sorted(value["plans"])})
        else:
            store = task_dispatch.DispatchStore(args.state_file)
            if args.command == "run":
                _json_print(
                    run_codex_task(
                        store,
                        args.task_id,
                        args.config,
                        args.workspace_root,
                        timeout=args.timeout,
                        allow_retry=args.retry,
                    )
                )
            elif args.command == "run-next":
                _json_print(run_next_codex_task(store, args.config, args.workspace_root, timeout=args.timeout))
        return 0
    except (ExecutorError, task_dispatch.DispatchError, OSError, ValueError) as error:
        sys.stderr.write("codex_executor: " + _clean(error, 500) + "\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
