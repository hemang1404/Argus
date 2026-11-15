"""Every documented CLI command and sub-command must at least render ``--help``.

``--help`` is eager and runs no command body, so this touches no provider, network or user
configuration; it catches commands that cannot even be constructed or whose option
declarations are broken.
"""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from argus.cli import main


def _command_paths(group: click.Group, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    paths: list[tuple[str, ...]] = []
    for name, cmd in sorted(group.commands.items()):
        paths.append((*prefix, name))
        if isinstance(cmd, click.Group):
            paths.extend(_command_paths(cmd, (*prefix, name)))
    return paths


COMMAND_PATHS = _command_paths(main)


def test_command_tree_is_not_empty():
    assert ("run",) in COMMAND_PATHS
    assert len(COMMAND_PATHS) >= 20


@pytest.mark.parametrize("path", COMMAND_PATHS, ids=lambda p: " ".join(p))
def test_command_help_renders(path: tuple[str, ...]):
    result = CliRunner().invoke(main, [*path, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
    assert result.exception is None


def test_version_flag_reports_package_version():
    import argus

    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert argus.__version__ in result.output
