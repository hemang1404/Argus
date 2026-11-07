"""Unit tests for RecoveryController in-place apply behavior (Milestone A3)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from argus.config import ArgusConfig
from argus.runtime.controller import RecoveryController, RecoveryOutcome


def test_controller_populates_patched_content_on_success():
    """Verify that when a test passes, RecoveryOutcome includes the full patched content."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # Create target file with bug
        target = tmp_path / "calc.py"
        target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

        # Create test file
        test_file = tmp_path / "test_calc.py"
        test_file.write_text("from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")

        cfg = ArgusConfig(provider="antigravity")
        controller = RecoveryController(config=cfg)

        # Mock the model call to return a valid search/replace block
        mock_response = (
            "<<<<<<< SEARCH\n"
            "def add(a, b):\n"
            "    return a - b\n"
            "=======\n"
            "def add(a, b):\n"
            "    return a + b\n"
            ">>>>>>> REPLACE"
        )

        with patch.object(controller, "_call_model", return_value=(mock_response, 100, 50)):
            outcome = controller.run(
                target_file=target,
                test_command=f"pytest {test_file}",
            )

        assert outcome.status == "SUCCESS"
        assert outcome.patched_content is not None
        assert "return a + b" in outcome.patched_content
        assert outcome.target_file == str(target)

        # By default in isolated workspace, the original file remains untouched
        assert "return a - b" in target.read_text(encoding="utf-8")

        # Applying writes the verified content
        target.write_text(outcome.patched_content, encoding="utf-8")
        assert "return a + b" in target.read_text(encoding="utf-8")
