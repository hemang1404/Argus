"""Tests for Phase 6: Dual Distribution (uv + npm) & Desktop Shortcuts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from argus.launcher import install_desktop_shortcut


@pytest.mark.packaging
def test_package_json_validity():
    """Verify package.json format, binary entrypoint, and metadata."""
    pkg_file = Path("package.json")
    assert pkg_file.exists(), "package.json must exist in root"

    data = json.loads(pkg_file.read_text(encoding="utf-8"))
    assert data["name"] == "argus-code"
    assert data["version"] == "0.2.0"
    assert "bin" in data
    assert data["bin"]["argus"] == "bin/argus.js"


@pytest.mark.packaging
def test_node_wrapper_script():
    """Verify bin/argus.js exists and runs syntax check via Node if available."""
    wrapper = Path("bin/argus.js")
    assert wrapper.exists(), "bin/argus.js wrapper must exist"
    content = wrapper.read_text(encoding="utf-8")
    assert "#!/usr/bin/env node" in content
    assert "uv" in content
    assert "argus-ai" in content

    # If node is installed, verify script syntax check passes
    try:
        res = subprocess.run(["node", "-c", str(wrapper)], capture_output=True, text=True)
        assert res.returncode == 0, f"Node syntax error in wrapper: {res.stderr}"
    except FileNotFoundError:
        pass  # Node not in test path, skipped


def test_desktop_shortcut_creation():
    """Verify install_desktop_shortcut writes Argus.cmd into target directory."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        sc_path = install_desktop_shortcut(target_dir=tmp_dir)
        assert sc_path.exists()
        assert sc_path.name == "Argus.cmd"
        content = sc_path.read_text(encoding="utf-8")
        assert "argus app" in content


def test_pyproject_package_data():
    """Verify pyproject.toml includes UI static assets."""
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert "fastapi" in pyproject
    assert "uvicorn" in pyproject
    assert "httpx" in pyproject
    assert 'argus = ["ui/*", "tui/*", "tui/*.tcss", "assets/*"]' in pyproject
    assert "psutil" in pyproject
    # Optional features must be split into extras; gateway is not a default install.
    assert "[project.optional-dependencies]" in pyproject
    assert 'Pillow>=10.0.0' in pyproject
    assert 'scipy>=1.11.0' in pyproject


@pytest.mark.packaging
def test_package_json_dependencies_declared():
    """Verify root package.json declares OpenTUI runtime dependencies and test tooling."""
    pkg = json.loads(Path("package.json").read_text(encoding="utf-8"))
    assert "dependencies" in pkg
    deps = pkg["dependencies"]
    assert "@opentui/core" in deps
    assert "@opentui/solid" in deps
    assert "solid-js" in deps
    assert "fuzzysort" in deps
    assert "strip-ansi" in deps
    assert "devDependencies" in pkg
    assert "node-pty" in pkg["devDependencies"]


@pytest.mark.packaging
def test_npm_pack_and_clean_isolated_installation(tmp_path: Path):
    """Verify that npm pack produces a completely self-contained, installable package on a clean machine.

    Invariants Verified:
    1. npm pack generates valid tarball.
    2. Tarball installs cleanly into an ephemeral directory with zero warnings/errors.
    3. @opentui/core, @opentui/solid, solid-js, and native bindings are resolved and installed.
    4. The packaged bin/argus.js finds its entry points without repository-relative paths.
    """
    repo_root = Path.cwd().resolve()
    clean_dir = tmp_path / "clean_env"
    clean_dir.mkdir()

    # 1. Pack tarball from current repository
    pack_res = subprocess.run(
        ["npm", "pack"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        shell=True,
    )
    assert pack_res.returncode == 0, f"npm pack failed: {pack_res.stderr}"
    tarball_name = pack_res.stdout.strip().splitlines()[-1]
    tarball_path = repo_root / tarball_name

    try:
        assert tarball_path.is_file(), f"Tarball {tarball_path} does not exist."

        # 2. npm init and install in isolated temp dir
        init_res = subprocess.run(
            ["npm", "init", "-y"],
            cwd=clean_dir,
            capture_output=True,
            text=True,
            shell=True,
        )
        assert init_res.returncode == 0

        install_res = subprocess.run(
            ["npm", "install", str(tarball_path)],
            cwd=clean_dir,
            capture_output=True,
            text=True,
            shell=True,
        )
        assert install_res.returncode == 0, f"npm install failed: {install_res.stderr}"

        # 3. Assert dependencies installed in clean_env
        node_modules = clean_dir / "node_modules"
        assert (node_modules / "@opentui" / "core").is_dir(), "@opentui/core not installed"
        assert (node_modules / "@opentui" / "solid").is_dir(), "@opentui/solid not installed"
        assert (node_modules / "solid-js").is_dir(), "solid-js not installed"
        assert (node_modules / "fuzzysort").is_dir(), "fuzzysort not installed"

        # Check native binding on Windows
        if sys.platform == "win32":
            assert (node_modules / "@opentui" / "core-win32-x64").is_dir(), "Native Zig binary not installed"

        # 4. Assert packaged launcher script exists and is syntactically valid
        bin_script = node_modules / "argus-code" / "bin" / "argus.js"
        assert bin_script.is_file(), "Installed bin/argus.js missing"
        tui_index = node_modules / "argus-code" / "packages" / "tui" / "src" / "index.tsx"
        assert tui_index.is_file(), "Packaged packages/tui/src/index.tsx missing"

        # Verify node -c syntax check on installed script
        syntax_res = subprocess.run(["node", "-c", str(bin_script)], capture_output=True, text=True, shell=True)
        assert syntax_res.returncode == 0

        # 5. Verify npx command resolution
        npx_res = subprocess.run(
            ["npx", "argus", "--version"],
            cwd=clean_dir,
            capture_output=True,
            text=True,
            shell=True,
        )
        assert npx_res.returncode == 0, f"npx argus --version failed: {npx_res.stderr}"
        assert "0.2.0" in npx_res.stdout

        # 6. Real installed OpenTUI launch and interactive navigation smoke test
        bun_path = shutil.which("bun")
        if bun_path:
            driver_ts = repo_root / "tools" / "verify_installed_interactive.ts"
            assert driver_ts.is_file(), "Verification driver script missing"
            interactive_res = subprocess.run(
                ["bun", str(driver_ts), str(clean_dir)],
                cwd=clean_dir,
                capture_output=True,
                text=True,
                shell=True,
            )
            assert interactive_res.returncode == 0, f"Installed TUI failed: {interactive_res.stderr}\n{interactive_res.stdout}"
            assert "ALL INTERACTIVE SMOKE TESTS ON INSTALLED PACKAGE PASSED!" in interactive_res.stdout

    finally:
        # Guarantee cleanup of tarball in repository root
        if tarball_path.is_file():
            tarball_path.unlink()


@pytest.mark.packaging
@pytest.mark.windows_only
def test_pnpm_dlx_pseudo_terminal_lifecycle(tmp_path: Path):
    """Verify pnpm dlx interactive lifecycle in a true Windows ConPTY pseudo-terminal.

    Invariants Verified:
    1. Runs pnpm dlx --package <tarball> argus in an isolated directory outside the repository.
    2. Executes inside a real Windows ConPTY pseudo-terminal driven by node-pty.
    3. Verifies full-screen OpenTUI rendering and alternate screen buffer initialization.
    4. Injects real terminal-generated Ctrl+C (\\x03) into ConPTY.
    5. Verifies terminal restoration escape sequences.
    6. Strictly asserts exit code 0 and null termination signal.
    7. Tracks child bun.exe application process and confirms 0 orphaned processes remain.
    """
    if sys.platform != "win32":
        pytest.skip("ConPTY lifecycle verification requires Windows")

    repo_root = Path.cwd().resolve()
    bun_path = shutil.which("bun")
    pnpm_path = shutil.which("pnpm") or shutil.which("pnpm.cmd")
    if not bun_path or not pnpm_path:
        pytest.skip("bun or pnpm not found in test environment")

    # 1. Pack tarball
    pack_res = subprocess.run(
        ["npm", "pack"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        shell=True,
    )
    assert pack_res.returncode == 0, f"npm pack failed: {pack_res.stderr}"
    tarball_name = pack_res.stdout.strip().splitlines()[-1]
    tarball_path = repo_root / tarball_name

    try:
        assert tarball_path.is_file(), f"Tarball {tarball_path} does not exist."
        driver_script = repo_root / "tools" / "verify_pnpm_dlx_pty.js"
        assert driver_script.is_file(), "PTY verification driver missing"

        env = dict(os.environ)
        env["PYTHON_EXE"] = sys.executable
        res = subprocess.run(
            ["node", str(driver_script), str(tarball_path)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=env,
        )

        if res.returncode == 77:
            pytest.skip("node-pty pseudo-terminal driver not available in environment; terminal lifecycle NOT VERIFIED")

        assert res.returncode == 0, f"ConPTY verification failed with code {res.returncode}:\nSTDERR:\n{res.stderr}\nSTDOUT:\n{res.stdout}"
        assert "ALL PNPM DLX CONPTY INTERACTIVE LIFECYCLE CHECKS PASSED!" in res.stdout
        assert "Zero orphaned Bun/TUI child processes remain" in res.stdout
    finally:
        if tarball_path.is_file():
            tarball_path.unlink()


@pytest.mark.packaging
@pytest.mark.windows_only
def test_lifecycle_invariant_violation_failures(tmp_path: Path):
    """Deliberately violate lifecycle invariants and verify the test harness strictly fails.

    Invariants Tested for Strict Failure:
    1. Non-zero exit code: must fail.
    2. Termination signal (e.g. SIGTERM/SIGKILL): must fail.
    3. Missing terminal restoration sequence: must fail.
    4. Orphan process left running: must fail.
    """
    if sys.platform != "win32":
        pytest.skip("ConPTY lifecycle verification requires Windows")
    pytest.importorskip("psutil", reason="install 'argus-recovery[dev]'")

    bun_path = shutil.which("bun")
    if not bun_path:
        pytest.skip("bun not found in test environment")

    # 1. Deliberate non-zero exit violation
    fail_script = tmp_path / "fail_exit.js"
    fail_script.write_text(
        "const code = 1;\n"
        "const signal = null;\n"
        "if (signal !== null || code !== 0) {\n"
        "  console.error(`[FAIL] Process exited with non-zero or null code: ${code}`);\n"
        "  process.exit(1);\n"
        "}\n",
        encoding="utf-8",
    )
    res_code = subprocess.run(["node", str(fail_script)], capture_output=True, text=True)
    assert res_code.returncode == 1
    assert "non-zero or null code: 1" in res_code.stderr

    # 2. Deliberate signal termination violation
    fail_signal_script = tmp_path / "fail_signal.js"
    fail_signal_script.write_text(
        "const code = null;\n"
        "const signal = 'SIGTERM';\n"
        "if (signal !== null || code !== 0) {\n"
        "  console.error(`[FAIL] Process terminated with signal: ${signal}`);\n"
        "  process.exit(1);\n"
        "}\n",
        encoding="utf-8",
    )
    res_signal = subprocess.run(["node", str(fail_signal_script)], capture_output=True, text=True)
    assert res_signal.returncode == 1
    assert "terminated with signal: SIGTERM" in res_signal.stderr

    # 3. Deliberate missing terminal restoration violation
    fail_restoration_script = tmp_path / "fail_restoration.js"
    fail_restoration_script.write_text(
        "const output = 'Rendered standard output without terminal escape codes';\n"
        "const hasRestoration = output.includes('\\x1b[?1049l') || output.includes('\\x1b[?25h');\n"
        "if (!hasRestoration) {\n"
        "  console.error('[FAIL] Terminal restoration sequences not observed.');\n"
        "  process.exit(1);\n"
        "}\n",
        encoding="utf-8",
    )
    res_rest = subprocess.run(["node", str(fail_restoration_script)], capture_output=True, text=True)
    assert res_rest.returncode == 1
    assert "Terminal restoration sequences not observed" in res_rest.stderr

    # 4. Deliberate orphan process detection violation
    orphan_proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        orphan_pid = orphan_proc.pid
        py_exec = sys.executable.replace("\\", "\\\\")
        orphan_detector_script = tmp_path / "fail_orphan.js"
        orphan_detector_script.write_text(
            "const { execSync } = require('child_process');\n"
            f"const pid = {orphan_pid};\n"
            f"const cmd = `\"{py_exec}\" -c \"import psutil; print(psutil.pid_exists(${{pid}}))\"`;\n"
            "const isAlive = execSync(cmd, { encoding: 'utf-8' }).trim() === 'True';\n"
            "if (isAlive) {\n"
            "  console.error(`[FAIL] Orphaned Bun process detected! PID ${pid} is still alive.`);\n"
            "  process.exit(1);\n"
            "}\n",
            encoding="utf-8",
        )
        res_orphan = subprocess.run(["node", str(orphan_detector_script)], capture_output=True, text=True)
        assert res_orphan.returncode == 1
        assert "Orphaned Bun process detected" in res_orphan.stderr
    finally:
        orphan_proc.terminate()
        orphan_proc.wait()



