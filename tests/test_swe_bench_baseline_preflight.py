"""Unit and integration tests for SWE-bench Repository Baseline Preflight Orchestrator.

Validates:
1. Manifest v2 repository task parsing and image inventory identification.
2. Host disk storage and download estimation bounds.
3. Live container baseline verification on psf__requests-1766 (base commit, F2P failure, cleanup).
4. Idempotent timestamped artifact persistence.
5. Strict decoupling: zero inference credentials or model adapter calls.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from src.experiments.run_swe_bench_baseline_preflight import (
        BaselinePreflightOrchestrator,
        ImageInventoryItem,
        BaselineTestRecord,
    )
except (ImportError, ModuleNotFoundError):
    pytest.skip("SWE-bench experiment harness not installed in public release", allow_module_level=True)
from argus.runtime.container import is_container_engine_available

if not is_container_engine_available("docker"):
    pytest.skip("Local Docker daemon unavailable", allow_module_level=True)


def test_inventory_manifest_v2_loads_and_identifies_images():
    """Verify orchestrator loads Manifest v2 and inventories all 15 repository tasks."""
    orchestrator = BaselinePreflightOrchestrator(
        manifest_path="experiments_archive/pilot_30_manifest_v2.json"
    )
    assert len(orchestrator.repo_tasks) == 15
    inventory = orchestrator.get_inventory()
    assert len(inventory) == 15

    # Check presence of key replacement tasks
    task_ids = {i.task_id for i in inventory}
    assert "pylint-dev__pylint-7277" in task_ids  # Replaced flask-5014
    assert "pydata__xarray-7233" in task_ids      # Replaced requests-5414
    assert "pallets__flask-5014" not in task_ids
    assert "psf__requests-5414" not in task_ids

    # Verify requests-1766 is recognized as locally present
    req_item = next(i for i in inventory if i.task_id == "psf__requests-1766")
    assert req_item.is_present is True
    assert "psf_1776_requests-1766" in req_item.container_image
    assert req_item.base_commit == "847735553aeda6e6633f2b32e14ba14ba86887a4"


def test_disk_storage_calculation_bounds():
    """Verify disk storage inspection and download estimation bounds."""
    orchestrator = BaselinePreflightOrchestrator(
        manifest_path="experiments_archive/pilot_30_manifest_v2.json"
    )
    report = orchestrator.get_disk_storage_report()
    assert report["total_repo_tasks"] == 15
    assert report["present_images_count"] >= 1
    assert report["missing_images_count"] <= 14
    assert report["host_disk_free_gb"] > 0
    assert "GB" in report["estimated_download_gb_range"]
    assert "GB" in report["estimated_storage_gb_range"]
    assert len(report["inventory"]) == 15


def test_single_baseline_verification_on_requests_1766(tmp_path: Path):
    """Verify live in-container baseline execution on psf__requests-1766."""
    orchestrator = BaselinePreflightOrchestrator(
        manifest_path="experiments_archive/pilot_30_manifest_v2.json",
        output_dir=tmp_path / "preflight_out",
    )
    task = next(t for t in orchestrator.repo_tasks if t["task_id"] == "psf__requests-1766")
    rec = orchestrator.verify_single_baseline(task)

    assert rec.task_id == "psf__requests-1766"
    assert rec.base_commit_verified is True
    assert rec.f2p_failed_as_expected is True  # Untouched baseline must fail
    assert rec.f2p_exit_code != 0
    assert rec.p2p_passed_as_expected is True
    assert rec.baseline_verified is True
    assert "sha256:0618949b4840" in rec.image_digest


def test_idempotent_results_persistence(tmp_path: Path):
    """Verify results persistence creates timestamped records and appends to audit history."""
    out_dir = tmp_path / "test_preflight"
    orchestrator = BaselinePreflightOrchestrator(
        manifest_path="experiments_archive/pilot_30_manifest_v2.json",
        output_dir=out_dir,
    )
    dummy_rec = BaselineTestRecord(
        task_id="test_task_01",
        repo="test/repo",
        base_commit="abcdef123456",
        container_image="test:latest",
        image_digest="sha256:dummy",
        base_commit_verified=True,
        f2p_command="pytest test_dummy.py -q",
        f2p_exit_code=1,
        f2p_failed_as_expected=True,
        f2p_duration_ms=120.0,
        p2p_command="",
        p2p_exit_code=0,
        p2p_passed_as_expected=True,
        p2p_duration_ms=0.0,
        baseline_verified=True,
    )
    orchestrator._save_preflight_results([dummy_rec])

    can_json = out_dir / "baseline_preflight_results.json"
    can_md = out_dir / "BASELINE_PREFLIGHT_REPORT.md"
    hist_log = out_dir / "preflight_audit_history.jsonl"

    assert can_json.exists()
    assert can_md.exists()
    assert hist_log.exists()

    with open(can_json, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["total_tested"] == 1
    assert data["verified_count"] == 1
    assert data["results"][0]["task_id"] == "test_task_01"


def test_zero_model_inference_decoupling():
    """Verify that orchestrator imports no API adapters or secret tokens."""
    import inspect
    from src.experiments import run_swe_bench_baseline_preflight as mod

    source = inspect.getsource(mod)
    # Ensure no LLM completion calls exist in preflight orchestrator
    assert "chat_completion" not in source
    assert "GROQ_API_KEY" not in source
    assert "OPENAI_API_KEY" not in source
