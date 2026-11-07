"""Unit tests for Argus Tool Integration Presets (Milestone A3)."""

from __future__ import annotations

import tempfile
from pathlib import Path
import pytest

from argus.adapters.presets import (
    PRESETS_REGISTRY,
    Preset,
    export_preset,
    get_preset,
    list_presets,
)


def test_list_presets():
    """Verify all expected presets are registered."""
    presets = list_presets()
    keys = {p.key for p in presets}
    expected = {"aider", "opencode", "continue", "claude", "cursor", "antigravity"}
    assert expected.issubset(keys)
    assert len(presets) >= 6


def test_get_preset_case_insensitive():
    """Verify lookup is case-insensitive and trims whitespace."""
    p1 = get_preset("aider")
    p2 = get_preset("  AIDER  ")
    assert p1 is not None
    assert p2 is not None
    assert p1.key == "aider"
    assert p1 == p2


def test_get_preset_unknown():
    """Verify unknown preset returns None."""
    assert get_preset("nonexistent_tool_xyz") is None


def test_export_preset_creates_file_and_guards_overwrite():
    """Verify export_preset creates the file and throws FileExistsError without overwrite."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # 1. Export Aider preset
        target_file = export_preset("aider", target_dir=tmp_path)
        assert target_file.exists()
        assert target_file.name == ".aider.conf.yml"
        content = target_file.read_text(encoding="utf-8")
        assert "http://127.0.0.1:8000/v1" in content
        assert "openai/argus/auto" in content

        # 2. Re-exporting without overwrite should raise FileExistsError
        with pytest.raises(FileExistsError):
            export_preset("aider", target_dir=tmp_path, overwrite=False)

        # 3. Exporting with overwrite=True succeeds
        target_file2 = export_preset("aider", target_dir=tmp_path, overwrite=True)
        assert target_file2.exists()


def test_export_opencode_json_validity():
    """Verify exported OpenCode configuration is valid JSON with expected schema."""
    import json

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        target_file = export_preset("opencode", target_dir=tmp_path)
        assert target_file.name == "opencode.json"
        
        data = json.loads(target_file.read_text(encoding="utf-8"))
        assert "providers" in data
        assert "argus" in data["providers"]
        assert data["providers"]["argus"]["baseURL"] == "http://127.0.0.1:8000/v1"
        model_ids = [m["id"] for m in data["providers"]["argus"]["models"]]
        assert "argus/auto" in model_ids
        assert "argus/frontier" in model_ids


