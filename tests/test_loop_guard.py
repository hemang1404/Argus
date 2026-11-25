"""Tests for streaming repetition-loop guard, trap-aware retry, and local eval tooling."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx

from argus.config import ArgusConfig
from argus.providers.adapter import CompletionResponse, OpenAICompatibleAdapter
from argus.providers.loop_guard import RepetitionLoopDetector
from argus.providers.models import ProviderConfig
from argus.runtime.controller import RecoveryController
from benchmarks.local_eval.run_experiment import (
    exact_mcnemar_pvalue,
    load_completed_ids,
    summarize_paired,
)


def test_loop_detector_allows_normal_search_replace_code():
    det = RepetitionLoopDetector()
    sample = (
        "<<<<<<< SEARCH\n"
        "def add(a, b):\n"
        "    return a - b\n"
        "=======\n"
        "def add(a, b):\n"
        "    return a + b\n"
        ">>>>>>> REPLACE\n"
    )
    for i in range(0, len(sample), 12):
        assert det.feed(sample[i : i + 12]) is None
    assert det.triggered_reason is None


def test_loop_detector_trips_on_repeated_line():
    det = RepetitionLoopDetector(line_repeat_threshold=4)
    line = "I need to carefully inspect the return value now.\n"
    triggered = None
    for _ in range(6):
        triggered = det.feed(line)
        if triggered:
            break
    assert triggered == "line_repeat"


def test_loop_detector_trips_on_ngram_without_newlines():
    det = RepetitionLoopDetector(ngram_chars=40, ngram_repeat_threshold=4)
    phrase = "Wait let me recheck that condition again "
    triggered = None
    for _ in range(10):
        triggered = det.feed(phrase)
        if triggered:
            break
    assert triggered == "ngram_repeat"


def test_adapter_streaming_loop_guard_aborts_sse_early():
    sse_lines = []
    for _ in range(12):
        chunk = {
            "model": "qwen2.5-coder-7b-instruct",
            "choices": [{"delta": {"content": "Let me reconsider the exact same line carefully.\n"}, "finish_reason": None}],
        }
        sse_lines.append(f"data: {json.dumps(chunk)}\n\n")
    sse_lines.append("data: [DONE]\n\n")
    body = "".join(sse_lines).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        req_json = json.loads(request.content.decode("utf-8"))
        assert req_json["stream"] is True
        return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = ProviderConfig(name="custom", base_url="http://127.0.0.1:8000/v1", model="qwen2.5-coder-7b-instruct")
    adapter = OpenAICompatibleAdapter(cfg, client=client)

    resp = adapter.chat_completion(
        messages=[{"role": "user", "content": "fix bug"}],
        loop_guard=True,
    )
    assert resp.finish_reason == "loop_aborted"
    assert resp.raw_response.get("loop_guard_reason") in ("line_repeat", "ngram_repeat")
    assert resp.completion_tokens > 0


def test_controller_trap_aware_retry_recovers_after_loop_abort(tmp_path: Path):
    src_file = tmp_path / "mod.py"
    src_file.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    test_file = tmp_path / "test_mod.py"
    test_file.write_text("from mod import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")

    seen_temps = []
    calls = 0

    class FakeAdapter:
        def chat_completion(self, messages, temperature=0.0, max_tokens=2500, model=None, loop_guard=False, **kwargs):
            nonlocal calls
            calls += 1
            seen_temps.append(temperature)
            assert loop_guard is True
            if calls == 1:
                return CompletionResponse(
                    content="Repeating myself endlessly...",
                    prompt_tokens=100,
                    completion_tokens=200,
                    duration_ms=50.0,
                    model=model or "qwen2.5-coder-7b-instruct",
                    finish_reason="loop_aborted",
                )
            return CompletionResponse(
                content=(
                    "<<<<<<< SEARCH\n"
                    "    return a - b\n"
                    "=======\n"
                    "    return a + b\n"
                    ">>>>>>> REPLACE\n"
                ),
                prompt_tokens=120,
                completion_tokens=40,
                duration_ms=40.0,
                model=model or "qwen2.5-coder-7b-instruct",
                finish_reason="stop",
            )

    cfg = ArgusConfig(
        provider="custom",
        model="qwen2.5-coder-7b-instruct",
        provider_config=ProviderConfig(
            name="custom",
            base_url="http://127.0.0.1:8000/v1",
            model="qwen2.5-coder-7b-instruct",
        ),
        max_attempts=3,
        temperature=0.0,
        loop_guard_enabled=True,
        trap_aware_retry=True,
    )
    ctrl = RecoveryController(config=cfg)
    with patch.object(ctrl, "_get_adapter", return_value=FakeAdapter()):
        outcome = ctrl.run(
            target_file="mod.py",
            test_command="pytest -q test_mod.py",
            workspace_root=str(tmp_path),
        )

    assert outcome.status == "SUCCESS"
    assert outcome.total_attempts == 2
    assert seen_temps == [0.0, 0.7]
    assert outcome.history[0].finish_reason == "loop_aborted"
    assert "repetition loop" in outcome.history[0].error_summary.lower()


def test_local_eval_resume_and_paired_summary(tmp_path: Path):
    base = tmp_path / "base.jsonl"
    treat = tmp_path / "treat.jsonl"
    base.write_text(
        "\n".join(
            [
                json.dumps({"task_id": "t1", "true_solved": True, "total_tokens": 500, "duration_s": 5.0}),
                json.dumps({"task_id": "t2", "true_solved": False, "total_tokens": 4000, "duration_s": 60.0}),
                json.dumps({"task_id": "t3", "true_solved": False, "total_tokens": 4000, "duration_s": 60.0}),
            ]
        ),
        encoding="utf-8",
    )
    treat.write_text(
        "\n".join(
            [
                json.dumps({"task_id": "t1", "true_solved": True, "total_tokens": 500, "duration_s": 5.0, "loop_aborts": 0}),
                json.dumps({"task_id": "t2", "true_solved": True, "total_tokens": 900, "duration_s": 12.0, "loop_aborts": 1}),
                json.dumps({"task_id": "t3", "true_solved": True, "total_tokens": 800, "duration_s": 10.0, "loop_aborts": 1}),
            ]
        ),
        encoding="utf-8",
    )
    assert load_completed_ids(base) == {"t1", "t2", "t3"}
    assert exact_mcnemar_pvalue(2, 0) == 0.5
    summary = summarize_paired(base, treat)
    assert summary["n_paired"] == 3
    assert summary["baseline_solved"] == 1
    assert summary["treatment_solved"] == 3
    assert summary["treatment_loop_aborts"] == 2
    assert summary["treatment_tokens"] < summary["baseline_tokens"]
