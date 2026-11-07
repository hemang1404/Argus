"""Argus update service: package manager detection, PyPI querying, and in-place upgrade lifecycle."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

import httpx
from packaging.version import InvalidVersion, Version

from argus import __version__

logger = logging.getLogger("argus.update")

DEFAULT_PYPI_JSON_URL = "https://pypi.org/pypi/argus-recovery/json"
DEFAULT_RELEASE_NOTES_URL = "https://github.com/hemang1404/Argus/releases"


class UpdateChannel(str, Enum):
    """Release channel to query."""
    STABLE = "stable"
    BETA = "beta"


class InstallMethod(str, Enum):
    """Detected or target installation method."""
    UV = "uv"
    PIPX = "pipx"
    PIP = "pip"
    SOURCE = "source"
    UNKNOWN = "unknown"


@dataclass
class UpdateCheckResult:
    """Result of an update availability check."""
    current_version: str
    latest_version: str
    has_update: bool
    channel: UpdateChannel
    install_method: InstallMethod
    release_notes_url: str = DEFAULT_RELEASE_NOTES_URL
    all_versions: List[str] = field(default_factory=list)
    error: Optional[str] = None
    is_source_checkout: bool = False
    warning: Optional[str] = None


def detect_install_method() -> InstallMethod:
    """Detect how the current Argus runtime was installed."""
    # 1. Source checkout check
    try:
        repo_root = Path(__file__).resolve().parent.parent
        git_dir = repo_root / ".git"
        pyproject = repo_root / "pyproject.toml"
        if git_dir.is_dir() and pyproject.is_file():
            return InstallMethod.SOURCE
    except Exception:
        pass

    # 2. UV tool check
    uv_tool_dir = os.environ.get("UV_TOOL_DIR", "")
    prefix_normalized = sys.prefix.replace("\\", "/").lower()
    exec_normalized = sys.executable.replace("\\", "/").lower()

    if uv_tool_dir and uv_tool_dir.replace("\\", "/").lower() in prefix_normalized:
        return InstallMethod.UV
    if "uv/tools" in prefix_normalized or "uv/tools" in exec_normalized:
        return InstallMethod.UV

    # 3. Pipx check
    pipx_home = os.environ.get("PIPX_HOME", "")
    if pipx_home and pipx_home.replace("\\", "/").lower() in prefix_normalized:
        return InstallMethod.PIPX
    if "pipx/venvs" in prefix_normalized or "pipx" in prefix_normalized:
        return InstallMethod.PIPX

    # 4. Pip / virtualenv check
    is_venv = (
        hasattr(sys, "real_prefix")
        or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
    )
    if is_venv or shutil.which("pip"):
        return InstallMethod.PIP

    return InstallMethod.UNKNOWN


def check_for_updates(
    channel: Optional[UpdateChannel] = None,
    include_prereleases: bool = False,
    pypi_url: str = DEFAULT_PYPI_JSON_URL,
    timeout: float = 5.0,
    client: Optional[httpx.Client] = None,
) -> UpdateCheckResult:
    """Query PyPI for available updates to argus-recovery."""
    current_ver_str = __version__
    try:
        current_ver = Version(current_ver_str)
    except InvalidVersion:
        current_ver = Version("0.0.0")

    # Determine channel
    if channel is None:
        if include_prereleases or current_ver.is_prerelease:
            active_channel = UpdateChannel.BETA
        else:
            active_channel = UpdateChannel.STABLE
    else:
        active_channel = channel

    method = detect_install_method()
    is_source = (method == InstallMethod.SOURCE)
    warning_msg = None
    if is_source:
        warning_msg = (
            "Argus is running from a local source/git checkout. "
            "Direct package upgrades via PyPI may conflict with your repository. "
            "Consider updating via 'git pull' or your development workflow instead."
        )

    headers = {"User-Agent": f"argus-recovery/{current_ver_str}"}

    def _parse_pypi_payload(data: dict[str, Any]) -> Tuple[List[str], str]:
        releases = data.get("releases", {})
        raw_versions = list(releases.keys())
        parsed_versions: List[Tuple[Version, str]] = []

        for v_str in raw_versions:
            try:
                v_obj = Version(v_str)
                if active_channel == UpdateChannel.STABLE and not include_prereleases:
                    if v_obj.is_prerelease:
                        continue
                parsed_versions.append((v_obj, v_str))
            except InvalidVersion:
                continue

        parsed_versions.sort(key=lambda item: item[0])
        sorted_ver_strings = [item[1] for item in parsed_versions]

        if sorted_ver_strings:
            latest_str = sorted_ver_strings[-1]
        else:
            info_ver = data.get("info", {}).get("version", current_ver_str)
            latest_str = info_ver

        return sorted_ver_strings, latest_str

    try:
        if client is not None:
            response = client.get(pypi_url, headers=headers, timeout=timeout)
            if response.status_code != 200:
                return UpdateCheckResult(
                    current_version=current_ver_str,
                    latest_version=current_ver_str,
                    has_update=False,
                    channel=active_channel,
                    install_method=method,
                    is_source_checkout=is_source,
                    warning=warning_msg,
                    error=f"PyPI query failed with HTTP {response.status_code}",
                )
            data = response.json()
        else:
            with httpx.Client(timeout=timeout) as internal_client:
                response = internal_client.get(pypi_url, headers=headers)
                if response.status_code != 200:
                    return UpdateCheckResult(
                        current_version=current_ver_str,
                        latest_version=current_ver_str,
                        has_update=False,
                        channel=active_channel,
                        install_method=method,
                        is_source_checkout=is_source,
                        warning=warning_msg,
                        error=f"PyPI query failed with HTTP {response.status_code}",
                    )
                data = response.json()

        all_versions, latest_str = _parse_pypi_payload(data)
        try:
            latest_obj = Version(latest_str)
            has_update = latest_obj > current_ver
        except InvalidVersion:
            has_update = False

        return UpdateCheckResult(
            current_version=current_ver_str,
            latest_version=latest_str,
            has_update=has_update,
            channel=active_channel,
            install_method=method,
            all_versions=all_versions,
            is_source_checkout=is_source,
            warning=warning_msg,
        )

    except httpx.RequestError as exc:
        return UpdateCheckResult(
            current_version=current_ver_str,
            latest_version=current_ver_str,
            has_update=False,
            channel=active_channel,
            install_method=method,
            is_source_checkout=is_source,
            warning=warning_msg,
            error=f"Network error querying PyPI: {exc}",
        )
    except Exception as exc:
        return UpdateCheckResult(
            current_version=current_ver_str,
            latest_version=current_ver_str,
            has_update=False,
            channel=active_channel,
            install_method=method,
            is_source_checkout=is_source,
            warning=warning_msg,
            error=f"Unexpected error checking updates: {exc}",
        )


def get_update_command(
    method: InstallMethod,
    target_version: Optional[str] = None,
    pre: bool = False,
) -> List[str]:
    """Generate the subprocess command array for package manager execution."""
    if method == InstallMethod.UV:
        uv_bin = shutil.which("uv") or "uv"
        if target_version:
            return [uv_bin, "tool", "install", "--upgrade", f"argus-recovery=={target_version}"]
        elif pre:
            return [uv_bin, "tool", "upgrade", "--prerelease", "allow", "argus-recovery"]
        else:
            return [uv_bin, "tool", "upgrade", "argus-recovery"]

    elif method == InstallMethod.PIPX:
        pipx_bin = shutil.which("pipx") or "pipx"
        if target_version:
            return [pipx_bin, "install", "--force", f"argus-recovery=={target_version}"]
        elif pre:
            return [pipx_bin, "upgrade", "--pip-args=--pre", "argus-recovery"]
        else:
            return [pipx_bin, "upgrade", "argus-recovery"]

    elif method == InstallMethod.PIP:
        pip_cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]
        if target_version:
            pip_cmd.append(f"argus-recovery=={target_version}")
        elif pre:
            pip_cmd.extend(["--pre", "argus-recovery"])
        else:
            pip_cmd.append("argus-recovery")
        return pip_cmd

    else:
        raise ValueError(f"Automated package update is not supported for installation method '{method.value}'.")


def apply_update(
    method: Optional[InstallMethod] = None,
    target_version: Optional[str] = None,
    pre: bool = False,
    timeout: float = 120.0,
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> Tuple[bool, str]:
    """Execute the package manager update command using array arguments."""
    active_method = method or detect_install_method()
    if active_method == InstallMethod.SOURCE:
        return False, "Refusing automated update: running from a source git checkout."
    if active_method == InstallMethod.UNKNOWN:
        active_method = InstallMethod.PIP

    try:
        cmd = get_update_command(active_method, target_version=target_version, pre=pre)
    except Exception as exc:
        return False, str(exc)

    try:
        if runner is not None:
            proc = runner(cmd)
        else:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )

        if proc.returncode == 0:
            out = proc.stdout.strip() or f"Successfully upgraded argus-recovery via {active_method.value}."
            return True, out
        else:
            err = proc.stderr.strip() or proc.stdout.strip() or f"Process exited with code {proc.returncode}"
            return False, f"Update command failed ({' '.join(cmd)}):\n{err}"

    except subprocess.TimeoutExpired:
        return False, f"Update command timed out after {timeout} seconds."
    except Exception as exc:
        return False, f"Execution failed: {exc}"


def verify_update(
    expected_version: Optional[str] = None,
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> Tuple[bool, str]:
    """Verify that the updated argus CLI responds with the expected version."""
    cmd = [sys.executable, "-m", "argus.cli", "--version"]
    try:
        if runner is not None:
            proc = runner(cmd)
        else:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )

        if proc.returncode != 0:
            return False, f"Verification failed with return code {proc.returncode}: {proc.stderr}"

        output = proc.stdout.strip()
        if expected_version and expected_version not in output:
            return False, f"Version mismatch: expected '{expected_version}' but got '{output}'."

        return True, output
    except Exception as exc:
        return False, f"Verification execution error: {exc}"
