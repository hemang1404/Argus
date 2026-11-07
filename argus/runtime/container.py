"""Disposable Container Evaluation Harness for Argus.

Provides isolated, sandboxed execution of SWE-bench repository tests inside
disposable Docker/Podman containers with:
  1. Strict base-commit verification inside the container (/testbed)
  2. Safe in-memory tar-stream candidate transfer (no host directory mounts)
  3. Pre-test candidate SHA256 hash verification inside the container
  4. Credential isolation (host API keys scrubbed, no host secrets in env)
  5. Configurable repository test timeouts, memory/CPU/PIDs limits, and task deadline
  6. Guaranteed container disposal or workspace reset between attempts
"""

from __future__ import annotations

import hashlib
import io
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


class ContainerEvaluationError(Exception):
    """Base exception for container evaluation harness."""
    pass


class BaseCommitMismatchError(ContainerEvaluationError):
    """Raised when repository HEAD commit in container does not match expected task base commit."""
    pass


class CandidateIntegrityError(ContainerEvaluationError):
    """Raised when transferred candidate file hash inside container does not match expected hash."""
    pass


class ContainerSecurityRefusal(ContainerEvaluationError):
    """Raised when container execution violates security policy or container engine is missing."""
    pass


@dataclass
class ContainerConfig:
    """Configuration for containerized repository test evaluation."""
    image: str = "ubuntu:22.04"
    workdir: str = "/testbed"
    base_commit: Optional[str] = None
    expected_candidate_hash: Optional[str] = None
    timeout_seconds: int = 180  # Separate repository evaluation timeout (vs algorithmic 30s)
    memory_limit: str = "4g"
    cpus: float = 2.0
    pids_limit: int = 256
    network_disabled: bool = True  # Security sandbox: no internet access during test run
    engine: str = "docker"  # "docker" or "podman"
    disposable: bool = True  # True = destroy container on completion; False = reset workspace
    scrub_host_env: bool = True
    setup_patch: Optional[str] = None
    setup_files: Dict[str, str] = field(default_factory=dict)


@dataclass
class ContainerExecutionResult:
    """Detailed telemetry from containerized test execution."""
    passed: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    timed_out: bool = False
    base_commit_verified: bool = False
    candidate_hash_verified: bool = False
    container_id: str = ""
    error: Optional[str] = None
    traceback_summary: str = ""


# Host environment patterns that must NEVER be passed into evaluation containers
FORBIDDEN_ENV_KEYS = {
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "ARGUS_GATEWAY_API_KEY",
    "GITHUB_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
}


def is_container_engine_available(engine: str = "docker") -> bool:
    """Check if the requested container engine is installed and responsive."""
    exe = shutil.which(engine)
    if not exe:
        return False
    try:
        res = subprocess.run(
            [exe, "info"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3.0,
            shell=False,
        )
        return res.returncode == 0
    except Exception:
        return False


def build_candidate_tar_stream(relative_path: str, content: str) -> bytes:
    """Package candidate file content into an in-memory tar archive for safe transfer."""
    clean_path = relative_path.replace("\\", "/").lstrip("/")
    data = content.encode("utf-8")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tarinfo = tarfile.TarInfo(name=clean_path)
        tarinfo.size = len(data)
        tarinfo.mtime = int(time.time())
        tarinfo.mode = 0o644
        tar.addfile(tarinfo, io.BytesIO(data))
    return buf.getvalue()


class ContainerEvaluator:
    """Manages the isolated lifecycle of evaluation containers for repository benchmark tasks."""

    def __init__(
        self,
        config: Optional[ContainerConfig] = None,
        raw_executor: Optional[Callable[..., Tuple[int, str, str]]] = None,
    ) -> None:
        self.config = config or ContainerConfig()
        self._raw_executor = raw_executor
        self.active_container_id: Optional[str] = None

    def _execute_command(
        self,
        args: List[str],
        input_bytes: Optional[bytes] = None,
        timeout: Optional[int] = None,
    ) -> Tuple[int, str, str]:
        """Execute a container command with sanitized environment and timeout."""
        if self._raw_executor is not None:
            return self._raw_executor(args, input_bytes=input_bytes, timeout=timeout)

        # Sanitize host environment: explicitly scrub credentials
        safe_env = {
            k: v for k, v in os.environ.items()
            if k.upper() not in FORBIDDEN_ENV_KEYS and not any(p in k.upper() for p in ("SECRET", "KEY", "TOKEN", "AUTH"))
        }

        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=safe_env,
                shell=False,
            )
            stdout, stderr = proc.communicate(input=input_bytes, timeout=timeout)
            return (
                proc.returncode,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=2.0)
            except Exception:
                pass
            raise
        except Exception as e:
            return (127, "", str(e))

    def start_container(self) -> str:
        """Create and start an isolated evaluation container without host directory mounts."""
        cmd = [
            self.config.engine,
            "run",
            "-d",
            f"--memory={self.config.memory_limit}",
            f"--cpus={self.config.cpus}",
            f"--pids-limit={self.config.pids_limit}",
            "-w", self.config.workdir,
        ]
        if self.config.network_disabled:
            cmd.append("--network=none")

        cmd.extend([self.config.image, "tail", "-f", "/dev/null"])

        code, out, err = self._execute_command(cmd, timeout=30)
        if code != 0:
            raise ContainerSecurityRefusal(f"Failed to start evaluation container ({code}): {err}")

        cid = out.strip()
        self.active_container_id = cid
        return cid

    def verify_base_commit(self, container_id: str, expected_commit: str) -> bool:
        """Verify repository HEAD inside container matches expected base commit."""
        expected = expected_commit.strip()
        cmd = [
            self.config.engine,
            "exec",
            "-w", self.config.workdir,
            container_id,
            "git", "rev-parse", "HEAD",
        ]
        code, out, err = self._execute_command(cmd, timeout=15)
        if code != 0:
            raise BaseCommitMismatchError(f"git rev-parse HEAD failed in container: {err}")

        actual_commit = out.strip()
        if actual_commit != expected:
            # Attempt to checkout the expected commit if container was prepared with a wrapper commit (e.g. SWE-bench)
            cmd_checkout = [
                self.config.engine,
                "exec",
                "-w", self.config.workdir,
                container_id,
                "git", "checkout", expected,
            ]
            self._execute_command(cmd_checkout, timeout=15)
            # Re-verify rev-parse HEAD after checkout
            code2, out2, err2 = self._execute_command(cmd, timeout=15)
            actual_commit = out2.strip() if code2 == 0 else actual_commit

        if actual_commit != expected:
            raise BaseCommitMismatchError(
                f"Container base commit '{actual_commit}' does not match expected '{expected}'"
            )
        return True

    def apply_setup_patch(self, container_id: str, patch_content: str) -> bool:
        """Apply a setup/test patch to container workdir via stdin."""
        cmd = [
            self.config.engine,
            "exec",
            "-i",
            "-w", self.config.workdir,
            container_id,
            "git", "apply",
        ]
        code, out, err = self._execute_command(cmd, input_bytes=patch_content.encode("utf-8"), timeout=30)
        if code != 0:
            raise ContainerEvaluationError(f"Failed to apply setup patch in container ({code}): {err}")
        return True

    def transfer_and_verify_candidate(
        self,
        container_id: str,
        target_rel_path: str,
        candidate_content: str,
        expected_candidate_hash: Optional[str] = None,
    ) -> bool:
        """Safely stream candidate into container via in-memory tar and verify its SHA256 inside the container."""
        clean_rel_path = target_rel_path.replace("\\", "/").lstrip("/")
        expected_sha = expected_candidate_hash or hashlib.sha256(candidate_content.encode("utf-8")).hexdigest()

        # 1. Package candidate into memory tar stream
        tar_bytes = build_candidate_tar_stream(clean_rel_path, candidate_content)

        # 2. Extract tar stream into container workdir
        cmd_extract = [
            self.config.engine,
            "exec",
            "-i",
            "-w", self.config.workdir,
            container_id,
            "tar", "-xf", "-", "-C", self.config.workdir,
        ]
        code, out, err = self._execute_command(cmd_extract, input_bytes=tar_bytes, timeout=30)
        if code != 0:
            raise CandidateIntegrityError(f"Failed to extract candidate in container ({code}): {err}")

        # 3. Verify SHA256 inside container
        full_container_path = f"{self.config.workdir.rstrip('/')}/{clean_rel_path}"
        cmd_hash = [
            self.config.engine,
            "exec",
            container_id,
            "sha256sum", full_container_path,
        ]
        code, out, err = self._execute_command(cmd_hash, timeout=15)
        if code != 0:
            raise CandidateIntegrityError(f"sha256sum failed in container for {full_container_path}: {err}")

        container_sha = out.split()[0].strip() if out.split() else ""
        if container_sha != expected_sha:
            raise CandidateIntegrityError(
                f"Candidate integrity mismatch: container file hash '{container_sha}' != expected '{expected_sha}'"
            )
        return True

    def execute_test(
        self,
        container_id: str,
        test_command: str,
        timeout_seconds: Optional[int] = None,
    ) -> ContainerExecutionResult:
        """Execute repository verification command strictly inside the container."""
        timeout = timeout_seconds or self.config.timeout_seconds
        start_time = time.perf_counter()

        # Tokenize test command
        cmd_tokens = shlex.split(test_command, posix=True)
        exec_cmd = [
            self.config.engine,
            "exec",
            "-w", self.config.workdir,
            container_id,
        ] + cmd_tokens

        timed_out = False
        try:
            code, out, err = self._execute_command(exec_cmd, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            code = 124
            out, err = "", f"Container test execution timed out after {timeout}s"

        duration_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
        passed = (code == 0) and not timed_out

        tb = ""
        if not passed:
            tb = f"FAIL (exit {code}):\n{err or out}".strip()

        return ContainerExecutionResult(
            passed=passed,
            exit_code=code,
            stdout=out,
            stderr=err,
            duration_ms=duration_ms,
            timed_out=timed_out,
            container_id=container_id,
            traceback_summary=tb,
        )

    def reset_workspace(self, container_id: str) -> None:
        """Reset container repository workspace to clean base state between attempts."""
        cmd = [
            self.config.engine,
            "exec",
            "-w", self.config.workdir,
            container_id,
            "sh", "-c", "git reset --hard HEAD && git clean -fdx",
        ]
        self._execute_command(cmd, timeout=30)

    def destroy_container(self, container_id: Optional[str] = None) -> None:
        """Forcefully stop and remove the container."""
        cid = container_id or self.active_container_id
        if not cid:
            return
        cmd = [self.config.engine, "rm", "-f", cid]
        try:
            self._execute_command(cmd, timeout=15)
        except Exception:
            pass
        finally:
            if cid == self.active_container_id:
                self.active_container_id = None

    def evaluate(
        self,
        candidate_content: str,
        target_rel_path: str,
        test_command: str,
        expected_base_commit: Optional[str] = None,
        expected_candidate_hash: Optional[str] = None,
    ) -> ContainerExecutionResult:
        """End-to-end atomic container evaluation cycle."""
        cid = None
        base_verified = False
        cand_verified = False

        try:
            # 1. Start disposable container
            cid = self.start_container()

            # 2. Verify base commit
            target_commit = expected_base_commit or self.config.base_commit
            if target_commit:
                self.verify_base_commit(cid, target_commit)
                base_verified = True

            # 2b. Apply setup/test patch if configured
            if self.config.setup_patch:
                self.apply_setup_patch(cid, self.config.setup_patch)

            # 2c. Transfer setup files if configured (e.g. test files)
            if self.config.setup_files:
                for rel_path, content in self.config.setup_files.items():
                    self.transfer_and_verify_candidate(
                        container_id=cid,
                        target_rel_path=rel_path,
                        candidate_content=content,
                    )

            # 3. Transfer and verify candidate hash
            self.transfer_and_verify_candidate(
                container_id=cid,
                target_rel_path=target_rel_path,
                candidate_content=candidate_content,
                expected_candidate_hash=expected_candidate_hash or self.config.expected_candidate_hash,
            )
            cand_verified = True

            # 4. Execute test inside container
            res = self.execute_test(cid, test_command)
            res.base_commit_verified = base_verified
            res.candidate_hash_verified = cand_verified
            return res

        except BaseCommitMismatchError as e:
            return ContainerExecutionResult(
                passed=False,
                exit_code=128,
                duration_ms=0.0,
                base_commit_verified=False,
                candidate_hash_verified=False,
                container_id=cid or "",
                error=str(e),
                traceback_summary=f"BASE_COMMIT_MISMATCH: {e}",
            )
        except CandidateIntegrityError as e:
            return ContainerExecutionResult(
                passed=False,
                exit_code=129,
                duration_ms=0.0,
                base_commit_verified=base_verified,
                candidate_hash_verified=False,
                container_id=cid or "",
                error=str(e),
                traceback_summary=f"CANDIDATE_INTEGRITY_MISMATCH: {e}",
            )
        except Exception as e:
            return ContainerExecutionResult(
                passed=False,
                exit_code=130,
                duration_ms=0.0,
                base_commit_verified=base_verified,
                candidate_hash_verified=cand_verified,
                container_id=cid or "",
                error=str(e),
                traceback_summary=f"CONTAINER_EXECUTION_ERROR: {e}",
            )
        finally:
            if cid and self.config.disposable:
                self.destroy_container(cid)

