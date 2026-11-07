"""Unit and integration tests for Structured JSON Lines IPC Event Protocol (DISC-03).

Verifies:
1. Versioned event serialization and deserialization (ArgusEvent).
2. JsonLinesEmitter stream writing and single-line format.
3. Event emission during RecoveryController recovery lifecycle.
4. CLI `argus run --json-events` stdout purity and parseability.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from click.testing import CliRunner

from argus.cli import main
from argus.config import ArgusConfig
from argus.runtime.controller import RecoveryController
from argus.runtime.events import ArgusEvent, EventType, JsonLinesEmitter, PROTOCOL_VERSION


def test_argus_event_serialization_and_deserialization():
    """Verify that ArgusEvent serializes to single-line JSON and round-trips accurately."""
    data = {"run_id": "run_123", "target_file": "src/main.py", "attempt": 1}
    event = ArgusEvent(event=EventType.SESSION_INIT, data=data)

    serialized = event.to_json()
    assert "\n" not in serialized
    assert '"event":"SESSION_INIT"' in serialized
    assert f'"version":"{PROTOCOL_VERSION}"' in serialized

    deserialized = ArgusEvent.from_json(serialized)
    assert deserialized is not None
    assert deserialized.event == "SESSION_INIT"
    assert deserialized.data["run_id"] == "run_123"
    assert deserialized.version == PROTOCOL_VERSION


def test_argus_event_malformed_json_handling():
    """Verify that from_json safely returns None on malformed input."""
    assert ArgusEvent.from_json("not valid json") is None
    assert ArgusEvent.from_json('{"no_event_field": 123}') is None
    assert ArgusEvent.from_json("") is None


def test_json_lines_emitter_all_event_types():
    """Verify that JsonLinesEmitter emits valid JSON lines for all lifecycle stages."""
    stream = io.StringIO()
    emitter = JsonLinesEmitter(stream=stream)

    emitter.session_init(
        run_id="run_test",
        target_file="test.py",
        test_command="pytest",
        baseline_hash="abc123hash",
        model="test-model",
        max_attempts=4,
        budget_limit_usd=0.05,
    )
    emitter.attempt_start(attempt=1, max_attempts=4, model="test-model")
    emitter.inference_done(attempt=1, duration_ms=250.0, prompt_tokens=100, completion_tokens=30, cost_usd=0.0001, model="test-model")
    emitter.patch_reconciled(attempt=1, patch_success=True, tier_used=1, ast_valid=True)
    emitter.test_result(attempt=1, passed=True, duration_ms=50.0)
    emitter.outcome(
        status="SUCCESS",
        run_id="run_test",
        verified=True,
        total_attempts=1,
        total_cost_usd=0.0001,
        total_duration_ms=400.0,
        target_file="test.py",
        unified_diff="--- a\n+++ b\n",
        message="Resolved!",
    )

    output = stream.getvalue()
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    assert len(lines) == 6

    parsed_events = [json.loads(line) for line in lines]
    event_names = [e["event"] for e in parsed_events]

    assert event_names == [
        "SESSION_INIT",
        "ATTEMPT_START",
        "INFERENCE_DONE",
        "PATCH_RECONCILED",
        "TEST_RESULT",
        "OUTCOME",
    ]

    # Verify version in all envelopes
    for p in parsed_events:
        assert p["version"] == PROTOCOL_VERSION


def test_controller_emits_json_events(tmp_path: Path):
    """Verify RecoveryController emits structured events throughout its execution."""
    target = tmp_path / "calc.py"
    target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    test_file = tmp_path / "test_calc.py"
    test_file.write_text("from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")

    cfg = ArgusConfig(provider="antigravity")
    controller = RecoveryController(config=cfg)

    mock_response = (
        "<<<<<<< SEARCH\n"
        "def add(a, b):\n"
        "    return a - b\n"
        "=======\n"
        "def add(a, b):\n"
        "    return a + b\n"
        ">>>>>>> REPLACE"
    )

    stream = io.StringIO()
    emitter = JsonLinesEmitter(stream=stream)

    with patch.object(controller, "_call_model", return_value=(mock_response, 100, 50)):
        outcome = controller.run(
            target_file=target,
            test_command=f"pytest {test_file}",
            event_emitter=emitter,
        )

    assert outcome.status == "SUCCESS"
    output = stream.getvalue()
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    assert len(lines) >= 4

    events = [json.loads(line) for line in lines]
    assert events[0]["event"] == "SESSION_INIT"
    assert events[0]["data"]["target_file"] == str(target)
    assert events[-1]["event"] == "OUTCOME"
    assert events[-1]["data"]["status"] == "SUCCESS"
    assert events[-1]["data"]["verified"] is True


def test_cli_run_json_events_mode(tmp_path: Path):
    """Verify CLI `argus run --json-events` emits clean, parseable JSON lines on stdout."""
    target = tmp_path / "math_mod.py"
    target.write_text("def sub(a, b):\n    return a + b\n", encoding="utf-8")

    test_file = tmp_path / "test_math.py"
    test_file.write_text("from math_mod import sub\ndef test_sub():\n    assert sub(5, 3) == 2\n", encoding="utf-8")

    mock_response = (
        "<<<<<<< SEARCH\n"
        "def sub(a, b):\n"
        "    return a + b\n"
        "=======\n"
        "def sub(a, b):\n"
        "    return a - b\n"
        ">>>>>>> REPLACE"
    )

    runner = CliRunner()
    with patch("argus.runtime.controller.RecoveryController._call_model", return_value=(mock_response, 80, 40)):
        res = runner.invoke(
            main,
            [
                "run",
                "--provider",
                "antigravity",
                "-f",
                str(target),
                "-t",
                f"pytest {test_file}",
                "--json-events",
            ],
            catch_exceptions=False,
        )

    assert res.exit_code == 0
    lines = [line.strip() for line in res.output.splitlines() if line.strip()]
    assert len(lines) >= 4

    # Every single line must be valid JSON with an event envelope
    for line in lines:
        parsed = json.loads(line)
        assert "event" in parsed
        assert "data" in parsed
        assert parsed["version"] == PROTOCOL_VERSION

    # First event is SESSION_INIT, last is OUTCOME
    first = json.loads(lines[0])
    assert first["event"] == "SESSION_INIT"
    last = json.loads(lines[-1])
    assert last["event"] == "OUTCOME"
    assert last["data"]["status"] == "SUCCESS"
