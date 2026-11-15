"""Close-out regression tests: Antigravity streaming (SEC-19) and usage labelling (FIN-08)."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from argus.providers.adapter import ProviderError
from argus.providers.antigravity import AntigravityHostAdapter
from argus.runtime.workspace import is_process_alive


def _adapter(monkeypatch, script: str, timeout: float = 20.0, idle: float | None = None) -> AntigravityHostAdapter:
    adapter = AntigravityHostAdapter(timeout_seconds=timeout)
    if idle is not None:
        adapter._stream_idle_timeout_seconds = idle
    adapter._is_agentapi = False
    monkeypatch.setattr(adapter, "get_cli_path", lambda: Path(sys.executable))
    monkeypatch.setattr(
        adapter,
        "_build_command",
        lambda **kw: ([sys.executable, "-u", "-c", script], "stub-model"),
    )
    return adapter


MSGS = [{"role": "user", "content": "hi"}]


def test_stream_yields_deltas_and_checks_exit_code(monkeypatch):
    script = (
        "import json\n"
        "for t in ('he', 'llo'):\n"
        "    print(json.dumps({'event': 'step_update', 'step_update': {'text_delta': t}}))\n"
    )
    adapter = _adapter(monkeypatch, script)
    assert "".join(adapter.stream_chat_completion(MSGS)) == "hello"


def test_stream_nonzero_exit_is_an_error_with_scrubbed_stderr(monkeypatch):
    script = "import sys\nsys.stderr.write('boom api_key=sk-abcdefghijklmnopqrstuvwxyz0123456789\\n')\nsys.exit(3)\n"
    adapter = _adapter(monkeypatch, script)
    with pytest.raises(ProviderError) as ei:
        list(adapter.stream_chat_completion(MSGS))
    assert "code 3" in str(ei.value)
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in str(ei.value)


def test_stream_large_stderr_does_not_deadlock(monkeypatch):
    script = (
        "import json, sys\n"
        "sys.stderr.write('x' * 2_000_000)\n"
        "sys.stderr.flush()\n"
        "print(json.dumps({'event': 'step_update', 'step_update': {'text_delta': 'ok'}}))\n"
    )
    adapter = _adapter(monkeypatch, script, timeout=30.0)
    started = time.monotonic()
    assert "".join(adapter.stream_chat_completion(MSGS)) == "ok"
    assert time.monotonic() - started < 25


def test_stream_idle_timeout_kills_the_process(monkeypatch, tmp_path):
    pidfile = tmp_path / "pid.txt"
    script = f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(120)\n"
    adapter = _adapter(monkeypatch, script, timeout=60.0, idle=1.0)
    started = time.monotonic()
    with pytest.raises(ProviderError) as ei:
        list(adapter.stream_chat_completion(MSGS))
    assert "no output" in str(ei.value)
    assert ei.value.status_code == 504
    assert time.monotonic() - started < 20
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while is_process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not is_process_alive(pid)


def test_stream_overall_timeout_with_steady_output(monkeypatch):
    script = (
        "import json, time\n"
        "while True:\n"
        "    print(json.dumps({'event': 'step_update', 'step_update': {'text_delta': '.'}}))\n"
        "    time.sleep(0.2)\n"
    )
    adapter = _adapter(monkeypatch, script, timeout=2.0, idle=30.0)
    with pytest.raises(ProviderError) as ei:
        list(adapter.stream_chat_completion(MSGS))
    assert "timed out after" in str(ei.value)


def test_stream_timeout_terminates_child_processes(monkeypatch, tmp_path):
    childfile = tmp_path / "child.txt"
    script = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(childfile)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"
    )
    adapter = _adapter(monkeypatch, script, timeout=60.0, idle=2.0)
    with pytest.raises(ProviderError):
        list(adapter.stream_chat_completion(MSGS))
    child_pid = int(childfile.read_text())
    deadline = time.monotonic() + 10
    while is_process_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not is_process_alive(child_pid)


def test_stream_consumer_cancel_terminates_the_process(monkeypatch, tmp_path):
    pidfile = tmp_path / "pid.txt"
    script = (
        "import json, os, time\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "while True:\n"
        "    print(json.dumps({'event': 'step_update', 'step_update': {'text_delta': 'x'}}))\n"
        "    time.sleep(0.1)\n"
    )
    adapter = _adapter(monkeypatch, script, timeout=60.0, idle=30.0)
    gen = adapter.stream_chat_completion(MSGS)
    assert next(gen) == "x"
    gen.close()
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while is_process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not is_process_alive(pid)


# --- FIN-08: honest usage labelling ------------------------------------------------------------


def _fake_run(stdout: str):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    return run


def test_antigravity_flags_estimated_usage(monkeypatch):
    adapter = AntigravityHostAdapter()
    monkeypatch.setattr(adapter, "get_cli_path", lambda: Path(sys.executable))
    monkeypatch.setattr(adapter, "_build_command", lambda **kw: (["x"], "m"))
    monkeypatch.setattr(subprocess, "run", _fake_run("plain text answer"))
    resp = adapter.chat_completion(MSGS)
    assert resp.usage_estimated is True


def test_antigravity_reported_usage_is_not_flagged(monkeypatch):
    adapter = AntigravityHostAdapter()
    monkeypatch.setattr(adapter, "get_cli_path", lambda: Path(sys.executable))
    monkeypatch.setattr(adapter, "_build_command", lambda **kw: (["x"], "m"))
    payload = '{"status": "SUCCESS", "response": "ok", "usage": {"input_tokens": 12, "output_tokens": 3}}'
    monkeypatch.setattr(subprocess, "run", _fake_run(payload))
    resp = adapter.chat_completion(MSGS)
    assert resp.usage_estimated is False
    assert (resp.prompt_tokens, resp.completion_tokens) == (12, 3)


def test_gateway_labels_estimated_antigravity_usage_as_locally_estimated():
    src = (Path(__file__).resolve().parent.parent / "argus" / "gateway" / "server.py").read_text(encoding="utf-8")
    assert 'usage_estimated", False) else "provider_reported"' in src
