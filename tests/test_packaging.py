"""Static packaging checks: metadata, single-source version, shipped files.

These read pyproject.toml / MANIFEST.in from the repository (located from this file, not from
the current directory) and never build or install anything. The wheel itself is built and
installed in a clean virtualenv by the CI ``build`` job.
"""

from __future__ import annotations

import re
import tempfile
import tomllib
from pathlib import Path

import argus
from argus.launcher import install_desktop_shortcut

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_desktop_shortcut_creation():
    """install_desktop_shortcut writes Argus.cmd into the target directory."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        sc_path = install_desktop_shortcut(target_dir=tmp_dir)
        assert sc_path.exists()
        assert sc_path.name == "Argus.cmd"
        assert "argus app" in sc_path.read_text(encoding="utf-8")


def test_version_has_a_single_source():
    """The version lives only in argus/__init__.py; pyproject reads it."""
    data = _pyproject()
    assert "version" not in data["project"]
    assert "version" in data["project"]["dynamic"]
    assert data["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "argus.__version__"}
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?", argus.__version__)


def test_package_data_and_py_typed_are_shipped():
    data = _pyproject()
    package_data = data["tool"]["setuptools"]["package-data"]["argus"]
    assert "py.typed" in package_data
    assert "assets/*" in package_data
    assert (REPO_ROOT / "argus" / "py.typed").is_file()
    assert "Typing :: Typed" in data["project"]["classifiers"]


def test_sdist_manifest_includes_test_support_files():
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include tests *.py" in manifest  # includes conftest.py
    assert "recursive-include tests/fixtures" in manifest
    assert "include argus/py.typed" in manifest


def test_dependency_sets_match_imports():
    """Core dependencies stay minimal; optional stacks are extras; no unused heavy deps."""
    data = _pyproject()
    core = {re.split(r"[<>=!~\[ ]", d, maxsplit=1)[0].lower() for d in data["project"]["dependencies"]}
    extras = data["project"]["optional-dependencies"]
    all_extra_text = " ".join(" ".join(v) for v in extras.values()).lower()

    for unused in ("scipy", "numpy", "tomli", "pydantic"):
        assert unused not in core
        assert unused not in all_extra_text
    for optional in ("textual", "pillow", "fastapi", "uvicorn", "tiktoken"):
        assert optional not in core
    assert {"click", "rich", "httpx", "keyring", "mcp", "pyyaml", "python-dotenv", "packaging"} <= core
    assert "research" not in extras
    assert "tiktoken" in " ".join(extras["tokens"]).lower()
    assert "argus-recovery[tui]" in extras["all"]
