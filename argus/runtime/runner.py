"""Subprocess Test Runner with process tree cleanup, timeout protection, argument validation, and execution isolation."""

from __future__ import annotations

import collections
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, List, Optional, Set

from argus.utils.redaction import scrub_secrets


class CommandSecurityError(ValueError):
    """Raised when a test command violates security policies or contains disallowed syntax."""
    pass


class IsolationUnavailableError(RuntimeError):
    """Raised when container/sandbox isolation is required for untrusted execution but unavailable."""
    pass


class BoundedOutputCollector:
    """Collects stream output with strict memory bounds, preserving head and tail portions."""

    def __init__(self, max_bytes: int = 4 * 1024 * 1024, head_bytes: int = 512 * 1024) -> None:
        self.max_bytes = max_bytes
        self.head_bytes = min(head_bytes, max_bytes // 2)
        self.tail_bytes = max_bytes - self.head_bytes
        self.head_buf = bytearray()
        self.tail_buf: Deque[bytes] = collections.deque()
        self.tail_size = 0
        self.total_bytes = 0

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total_bytes += len(chunk)

        # 1. Fill head buffer if not full
        if len(self.head_buf) < self.head_bytes:
            needed = self.head_bytes - len(self.head_buf)
            head_part = chunk[:needed]
            self.head_buf.extend(head_part)
            chunk = chunk[needed:]

        if not chunk:
            return

        # 2. Append to tail deque
        self.tail_buf.append(chunk)
        self.tail_size += len(chunk)

        # 3. Maintain tail bound
        while self.tail_size > self.tail_bytes and self.tail_buf:
            popped = self.tail_buf.popleft()
            self.tail_size -= len(popped)
            if self.tail_size < self.tail_bytes:
                keep = self.tail_bytes - self.tail_size
                remainder = popped[-keep:]
                self.tail_buf.appendleft(remainder)
                self.tail_size += len(remainder)
                break

    def get_text(self, encoding: str = "utf-8", errors: str = "replace") -> str:
        if self.total_bytes <= len(self.head_buf) + self.tail_size:
            data = bytes(self.head_buf) + b"".join(self.tail_buf)
            return data.decode(encoding, errors=errors)

        dropped = self.total_bytes - (len(self.head_buf) + self.tail_size)
        head_text = bytes(self.head_buf).decode(encoding, errors=errors)
        tail_text = b"".join(self.tail_buf).decode(encoding, errors=errors)
        trunc_marker = f"\n\n... [OUTPUT TRUNCATED: {dropped} bytes omitted by Argus security limits] ...\n\n"
        return head_text + trunc_marker + tail_text


@dataclass
class TestExecutionResult:
    """Outcome of running the test command in a workspace."""
    __test__ = False
    passed: bool
    exit_code: int
    duration_ms: float
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    traceback_summary: str = ""
    isolation_used: str = "host"  # "host" or "container"


# Explicit whitelist of permitted test runners and compiler tools
PERMITTED_EXECUTABLES: Set[str] = {
    "pytest",
    "pytest.exe",
    "python",
    "python.exe",
    "python3",
    "python3.exe",
    "py",
    "py.exe",
    "node",
    "node.exe",
    "npm",
    "npm.cmd",
    "npx",
    "npx.cmd",
    "pnpm",
    "pnpm.cmd",
    "cargo",
    "cargo.exe",
    "go",
    "go.exe",
    "dotnet",
    "dotnet.exe",
    "mvn",
    "mvn.cmd",
    "gradle",
    "gradle.bat",
    "uv",
    "uv.exe",
}

# Forbidden shell chaining and redirection operators
SHELL_OPERATOR_TOKENS: Set[str] = {
    "&&", "||", "|", "&", ";", ">", ">>", "<", "<<", "`", "$(", "${"
}

# Substring patterns for sensitive environment variables to scrub
SCRUB_ENV_PATTERNS = (
    "KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH",
    "PRIVATE", "GROQ_", "OPENAI_", "ANTHROPIC_", "GEMINI_", "DEEPSEEK_",
    "AWS_", "AZURE_", "GITHUB_", "GITLAB_", "SLACK_", "DISCORD_",
)


def detect_project_virtualenv(cwd: Path) -> Optional[Path]:
    """Detect local project virtual environment (e.g. .venv, venv, env) in cwd or parents.
    
    Returns the path to the virtualenv's Python binary if found.
    """
    check_dirs = [cwd] + list(cwd.parents)
    # Stop searching above repo root or user home
    for base in check_dirs[:4]:
        for venv_name in (".venv", "venv", ".env", "env"):
            candidate = base / venv_name
            if candidate.is_dir():
                if sys.platform == "win32":
                    py_bin = candidate / "Scripts" / "python.exe"
                else:
                    py_bin = candidate / "bin" / "python"
                if py_bin.is_file():
                    return py_bin.resolve()
    return None


def get_sanitized_environment(cwd: Path, venv_py: Optional[Path] = None) -> dict[str, str]:
    """Return a sanitized copy of os.environ with secrets and credentials removed."""
    safe_env: dict[str, str] = {}
    for k, v in os.environ.items():
        k_upper = k.upper()
        if k_upper in ("TOKENIZERS_PARALLELISM", "AUTHENTICATION_PROFILE_DIR"):
            safe_env[k] = v
            continue
        if any(pat in k_upper for pat in SCRUB_ENV_PATTERNS):
            continue
        safe_env[k] = v

    if venv_py:
        venv_scripts = venv_py.parent
        existing_path = safe_env.get("PATH", "")
        safe_env["PATH"] = f"{str(venv_scripts)}{os.pathsep}{existing_path}"
        safe_env["VIRTUAL_ENV"] = str(venv_scripts.parent)

    existing_pythonpath = safe_env.get("PYTHONPATH", "")
    safe_env["PYTHONPATH"] = f"{str(cwd)}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(cwd)
    safe_env["PYTHONDONTWRITEBYTECODE"] = "1"
    safe_env["PYTHONUNBUFFERED"] = "1"
    return safe_env


def is_container_available() -> bool:
    """Check if Docker or Podman engine is active and reachable."""
    for engine in ("docker", "podman"):
        exe = shutil.which(engine)
        if exe:
            try:
                res = subprocess.run(
                    [exe, "info"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=2.0,
                    shell=False,
                )
                if res.returncode == 0:
                    return True
            except Exception:
                pass
    return False


def validate_and_tokenize_command(command: str, cwd: Optional[Path] = None) -> List[str]:
    """Parse and validate command string into an argument list without a shell.

    Enforces permitted test executables, rejects shell metacharacters and chaining.
    Automatically resolves virtualenv Python/pytest if a local project virtualenv is detected.
    """
    raw_cmd = command.strip()
    if not raw_cmd:
        raise CommandSecurityError("Test command cannot be empty.")

    # Check for shell metacharacters that indicate command chaining, redirection, or backticks
    # Allow semicolon inside quotes (handled by shlex), but forbid unquoted shell operators
    for op in ("&&", "||", "|", "`", "$(", "${"):
        if op in raw_cmd:
            raise CommandSecurityError(f"Shell chaining operator '{op}' is strictly prohibited.")

    posix = (sys.platform != "win32")
    try:
        raw_tokens = shlex.split(raw_cmd, posix=posix)
    except ValueError as e:
        raise CommandSecurityError(f"Failed to parse command arguments: {e}")

    if not raw_tokens:
        raise CommandSecurityError("Parsed command argument list is empty.")

    # Strip enclosing quotes preserved by Windows non-posix shlex
    tokens = []
    for t in raw_tokens:
        if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
            tokens.append(t[1:-1] if len(t) >= 2 else t)
        else:
            tokens.append(t)

    # Check each token for forbidden standalone operators or semicolon suffixes
    for token in tokens:
        if token in SHELL_OPERATOR_TOKENS:
            raise CommandSecurityError(f"Disallowed shell operator token '{token}'.")
        if token.startswith(";") or token.endswith(";"):
            raise CommandSecurityError(f"Disallowed command separator in argument '{token}'.")
        if "`" in token or "$(" in token:
            raise CommandSecurityError(f"Command substitution detected in argument '{token}'.")

    # Validate the target executable
    raw_exe = tokens[0].strip("\"'")
    exe_name = Path(raw_exe).name.lower()

    # Detect local project virtualenv
    venv_py = detect_project_virtualenv(cwd) if cwd else None

    # Allow python executable if it matches current sys.executable or project venv
    is_current_python = (Path(raw_exe).resolve() == Path(sys.executable).resolve())
    is_venv_python = (venv_py is not None and Path(raw_exe).resolve() == venv_py)

    if exe_name not in PERMITTED_EXECUTABLES and not is_current_python and not is_venv_python:
        raise CommandSecurityError(
            f"Executable '{exe_name}' is not permitted for test execution. "
            f"Permitted executables: {sorted(list(PERMITTED_EXECUTABLES))}"
        )

    # Resolve pytest and python using project virtualenv if detected
    resolved_tokens = list(tokens)
    target_py = str(venv_py) if venv_py else sys.executable

    if exe_name in ("pytest", "pytest.exe"):
        # If project has its own venv with pytest, route through it
        venv_pytest = (venv_py.parent / ("pytest.exe" if sys.platform == "win32" else "pytest")) if venv_py else None
        if venv_pytest and venv_pytest.is_file():
            resolved_tokens = [str(venv_pytest)] + resolved_tokens[1:]
        else:
            # Fall back to active system/user pytest executable if present in PATH
            system_pytest = shutil.which("pytest") or shutil.which("pytest.exe")
            if system_pytest:
                resolved_tokens = [system_pytest] + resolved_tokens[1:]
            elif venv_py:
                resolved_tokens = [str(venv_py), "-m", "pytest"] + resolved_tokens[1:]
            else:
                resolved_tokens = [target_py, "-m", "pytest"] + resolved_tokens[1:]
    elif exe_name in ("python", "python.exe", "python3", "python3.exe"):
        if venv_py and not Path(raw_exe).is_absolute():
            resolved_tokens = [target_py] + resolved_tokens[1:]
        elif not shutil.which(raw_exe):
            resolved_tokens = [target_py] + resolved_tokens[1:]

    return resolved_tokens


class SubprocessTestRunner:
    """Safely runs test suites inside isolated directories without shell injection vulnerabilities."""

    def __init__(
        self,
        timeout_seconds: int = 30,
        isolation_mode: str = "auto",  # "auto", "host", "container"
        untrusted: bool = False,
        max_output_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.isolation_mode = isolation_mode
        self.untrusted = untrusted
        self.max_output_bytes = max_output_bytes

    def run_test(
        self,
        command: str,
        cwd: Path,
        untrusted: Optional[bool] = None,
    ) -> TestExecutionResult:
        """Execute test command with argument validation, timeout, bounded memory output capture, and process-tree termination.

        Never uses shell=True. Scrubs environment variables. Fails closed if container isolation
        is required for untrusted execution. SubprocessTestRunner executes exclusively on the host.
        """
        start_time = time.perf_counter()
        is_untrusted = self.untrusted if untrusted is None else untrusted
        must_use_container = (self.isolation_mode == "container") or is_untrusted

        # 1. Check isolation requirements: SubprocessTestRunner NEVER claims container isolation
        if must_use_container:
            if not is_container_available():
                # Fail closed: container engine not detected
                msg = (
                    "Execution refused: container isolation is required for untrusted code execution, "
                    "but no running container engine (Docker/Podman) was detected. Failing closed."
                )
            else:
                # Fail closed: SubprocessTestRunner is a host runner, not a container engine
                msg = (
                    "Execution refused: container isolation was requested, but SubprocessTestRunner "
                    "executes directly on the host and cannot provide container isolation. "
                    "Use ContainerEvaluator for containerized execution. Failing closed."
                )
            return TestExecutionResult(
                passed=False,
                exit_code=125,
                duration_ms=round((time.perf_counter() - start_time) * 1000.0, 2),
                timed_out=False,
                stderr=msg,
                traceback_summary=f"ISOLATION REFUSAL: {msg}",
                isolation_used="refused",
            )

        # 2. Tokenize and validate the command
        try:
            tokens = validate_and_tokenize_command(command, cwd=cwd)
        except CommandSecurityError as err:
            return TestExecutionResult(
                passed=False,
                exit_code=126,
                duration_ms=round((time.perf_counter() - start_time) * 1000.0, 2),
                timed_out=False,
                stderr=str(err),
                traceback_summary=f"COMMAND SECURITY REFUSAL: {err}",
                isolation_used="refused",
            )

        # 3. Prepare sanitized environment with virtualenv detection
        venv_py = detect_project_virtualenv(cwd)
        env = get_sanitized_environment(cwd, venv_py=venv_py)
        is_windows = sys.platform == "win32"

        kwargs: dict = {
            "cwd": str(cwd),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": env,
            "shell": False,  # Security: Direct invocation, no shell
        }
        if not is_windows:
            kwargs["start_new_session"] = True

        timed_out = False
        returncode = -1
        stdout_collector = BoundedOutputCollector(max_bytes=self.max_output_bytes)
        stderr_collector = BoundedOutputCollector(max_bytes=self.max_output_bytes)

        def _drain_stream(stream, collector: BoundedOutputCollector) -> None:
            try:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    collector.feed(chunk)
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        try:
            proc = subprocess.Popen(tokens, **kwargs)
            t_out = threading.Thread(target=_drain_stream, args=(proc.stdout, stdout_collector), daemon=True)
            t_err = threading.Thread(target=_drain_stream, args=(proc.stderr, stderr_collector), daemon=True)
            t_out.start()
            t_err.start()

            try:
                returncode = proc.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_process_tree(proc.pid, is_windows)
                try:
                    returncode = proc.wait(timeout=2)
                except Exception:
                    returncode = -1
            finally:
                t_out.join(timeout=2.0)
                t_err.join(timeout=2.0)
        except Exception as ex:
            return TestExecutionResult(
                passed=False,
                exit_code=127,
                duration_ms=round((time.perf_counter() - start_time) * 1000.0, 2),
                timed_out=False,
                stderr=f"Subprocess launch error: {ex}",
                traceback_summary=f"LAUNCH ERROR: {ex}",
                isolation_used="host",
            )

        duration_ms = (time.perf_counter() - start_time) * 1000.0
        passed = (returncode == 0) and not timed_out

        # Scrub sensitive data from raw stdout/stderr before returning or extracting tracebacks
        raw_stdout = stdout_collector.get_text()
        raw_stderr = stderr_collector.get_text()
        scrubbed_stdout = scrub_secrets(raw_stdout)
        scrubbed_stderr = scrub_secrets(raw_stderr)

        traceback_summary = ""
        if not passed:
            traceback_summary = self._extract_traceback(scrubbed_stdout, scrubbed_stderr, timed_out)

        return TestExecutionResult(
            passed=passed,
            exit_code=returncode,
            duration_ms=round(duration_ms, 2),
            timed_out=timed_out,
            stdout=scrubbed_stdout,
            stderr=scrubbed_stderr,
            traceback_summary=traceback_summary,
            isolation_used="host",  # Strictly host execution reported
        )

    def _kill_process_tree(self, pid: int, is_windows: bool) -> None:
        """Hard-kill the process tree to prevent lingering worker processes."""
        if is_windows:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    shell=False,
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                pass
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except Exception:
                pass

    def _extract_traceback(self, stdout: str, stderr: str, timed_out: bool) -> str:
        """Extract high-signal error traceback lines for LLM feedback."""
        if timed_out:
            return f"TIMEOUT: Test execution exceeded limit of {self.timeout_seconds} seconds."

        combined = scrub_secrets((stdout + "\n" + stderr).strip())
        lines = combined.splitlines()

        failure_lines = []
        capture = False

        for line in lines:
            if "=== FAILURES ===" in line or "=== ERRORS ===" in line:
                capture = True
            if capture:
                failure_lines.append(line)
                if len(failure_lines) >= 60:
                    failure_lines.append("... [traceback truncated for brevity]")
                    break

        if failure_lines:
            return "\n".join(failure_lines)

        return "\n".join(lines[-25:]) if lines else "No output captured from test command."
