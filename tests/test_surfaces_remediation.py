"""Regression tests for the user-facing surfaces (CLI, API, TUI, MCP consumption side).

Everything here runs against stub controllers/adapters, an isolated ARGUS_CONFIG_DIR and the
in-memory keyring from conftest.py: no network, no real provider and no host CLI is used.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from click.testing import CliRunner

from argus.cli import main
from argus.config import DEFAULT_BUDGET_USD, DEFAULT_MAX_ATTEMPTS, MAX_ATTEMPTS_CEILING, ModelCost
from argus.providers.models import ProviderConfig
from argus.providers.storage import ProviderStorage
from argus.runtime.candidate import CandidateArtifactStore
from argus.runtime.controller import RecoveryController, RecoveryOutcome
from argus.runtime.runner import HOST_EXECUTION_WARNING


# --------------------------------------------------------------------------- helpers
class _RecordingController:
    """Stands in for RecoveryController: records what the CLI passes, performs no recovery."""

    calls: List[Dict[str, Any]] = []
    configs: List[Any] = []
    outcome: RecoveryOutcome

    _resolve_workspace_root = staticmethod(RecoveryController._resolve_workspace_root)

    def __init__(self, config=None) -> None:
        self.config = config
        type(self).configs.append(config)

    def run(self, **kwargs):
        type(self).calls.append(kwargs)
        return type(self).outcome


@pytest.fixture
def recording_controller(monkeypatch: pytest.MonkeyPatch):
    _RecordingController.calls = []
    _RecordingController.configs = []
    _RecordingController.outcome = RecoveryOutcome(
        status="ALREADY_PASSING", total_attempts=0, total_cost_usd=0.0, total_duration_ms=1.0
    )
    monkeypatch.setattr("argus.cli.RecoveryController", _RecordingController)
    return _RecordingController


def _save_candidate(ws: Path, run_id: str = "run_surface", verified: bool = True) -> Path:
    target = ws / "calc.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")
    import hashlib

    CandidateArtifactStore.save_run_artifact(
        run_id=run_id,
        target_file="calc.py",
        test_command="pytest",
        baseline_hash=hashlib.sha256(target.read_bytes()).hexdigest(),
        status="SUCCESS",
        verified=verified,
        tier_used=1,
        attempts=1,
        cost_usd=0.0,
        duration_ms=1.0,
        patched_content="def f():\n    return 2\n",
        unified_diff="--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def f():\n-    return 1\n+    return 2\n",
        workspace_root=ws,
    )
    return target


def _configure_stub_provider(name: str = "stubprov") -> None:
    ProviderStorage().save_provider(
        ProviderConfig(name=name, base_url="http://127.0.0.1:9/v1", model="mock-model", api_key="k-123456789")
    )
    ProviderStorage().set_active_provider(name)


# --------------------------------------------------------------------------- UX-002 gateway
def test_gateway_without_key_is_a_security_refusal_not_a_traceback(monkeypatch: pytest.MonkeyPatch):
    from argus.gateway.config import GatewayConfig

    monkeypatch.delenv("ARGUS_GATEWAY_API_KEY", raising=False)
    monkeypatch.setattr(GatewayConfig, "load", classmethod(lambda cls, path=None: cls()))
    for cmd in ("gateway", "serve"):
        res = CliRunner().invoke(main, [cmd])
        assert res.exit_code == 1, res.output
        assert "Security Refusal" in res.output
        assert res.exception is None or isinstance(res.exception, SystemExit)


def test_gateway_starts_through_the_plain_function(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import uvicorn

    from argus.gateway import server as gw_server
    from argus.gateway.config import GatewayConfig

    started: Dict[str, Any] = {}
    monkeypatch.setenv("ARGUS_GATEWAY_API_KEY", "test-gateway-key")
    monkeypatch.setattr(GatewayConfig, "load", classmethod(lambda cls, path=None: cls()))
    monkeypatch.setattr(gw_server, "init_gateway", lambda config, workspace: started.update(workspace=workspace))
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, log_level: started.update(host=host, port=port))

    res = CliRunner().invoke(main, ["-w", str(tmp_path), "gateway", "--port", "8123"])
    assert res.exit_code == 0, res.output
    assert started["host"] == "127.0.0.1" and started["port"] == 8123
    assert Path(started["workspace"]) == tmp_path.resolve()  # the global -w reached the gateway


def test_gateway_refuses_non_loopback_without_allow_remote(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARGUS_GATEWAY_API_KEY", "k")
    res = CliRunner().invoke(main, ["gateway", "--host", "0.0.0.0"])
    assert res.exit_code == 1
    assert "non-loopback" in res.output


# --------------------------------------------------------------------------- UX-001 exec
def test_exec_without_provider_errors_and_never_touches_the_host_runtime(monkeypatch: pytest.MonkeyPatch):
    import argus.providers.antigravity as agy

    def boom(*a, **k):
        raise AssertionError("exec must not reach the host runtime without an explicit provider")

    monkeypatch.setattr(agy.AntigravityHostAdapter, "chat_completion", boom)
    monkeypatch.setattr(agy, "find_antigravity_cli", boom)
    res = CliRunner().invoke(main, ["exec", "hello"])
    assert res.exit_code == 1
    assert "No LLM provider configured" in res.output
    assert "antigravity" in res.output  # tells the user how to opt in explicitly


def test_exec_uses_the_configured_provider_and_reports_the_answering_model(monkeypatch: pytest.MonkeyPatch):
    _configure_stub_provider()
    seen: Dict[str, Any] = {}

    class _Resp:
        content = "stub answer"
        model = "answering-model"
        duration_ms = 5.0
        prompt_tokens = 3
        completion_tokens = 2

    class _Adapter:
        def __init__(self, config):
            seen["provider"] = config.name

        def chat_completion(self, messages, effort=None):
            seen["messages"] = messages
            return _Resp()

    monkeypatch.setattr("argus.cli.OpenAICompatibleAdapter", _Adapter)
    res = CliRunner().invoke(main, ["exec", "say hi"])
    assert res.exit_code == 0, res.output
    assert seen["provider"] == "stubprov"
    assert "stub answer" in res.output
    assert "answered by answering-model" in res.output


def test_exec_scrubs_secrets_from_the_context_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _configure_stub_provider()
    secret = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"
    ctx = tmp_path / "ctx.py"
    ctx.write_text(f'KEY = "{secret}"\n', encoding="utf-8")
    seen: Dict[str, Any] = {}

    class _Resp:
        content, model, duration_ms, prompt_tokens, completion_tokens = "ok", "m", 1.0, 1, 1

    class _Adapter:
        def __init__(self, config):
            pass

        def chat_completion(self, messages, effort=None):
            seen["messages"] = messages
            return _Resp()

    monkeypatch.setattr("argus.cli.OpenAICompatibleAdapter", _Adapter)
    res = CliRunner().invoke(main, ["exec", "explain", "-f", str(ctx)])
    assert res.exit_code == 0, res.output
    assert secret not in json.dumps(seen["messages"])


# --------------------------------------------------------------------------- UX-013 exit codes
@pytest.mark.parametrize(
    "args",
    [
        ["provider", "select", "does-not-exist"],
        ["provider", "delete", "does-not-exist"],
        ["provider", "test", "does-not-exist"],
        ["provider", "test"],
        ["inspect", "run_missing"],
        ["inspect", "../escape"],
        ["init", "bogus-client"],
        ["diff"],
        ["diff", "../escape"],
        ["approve", "../escape", "-y"],
        ["reject", "../escape"],
        ["apply", "--run-id", "../escape"],
        ["apply"],
    ],
)
def test_errors_exit_nonzero(args: List[str], tmp_path: Path):
    res = CliRunner().invoke(main, ["-w", str(tmp_path)] + args)
    assert res.exit_code == 1, f"{args}: {res.output}"
    assert res.exception is None or isinstance(res.exception, SystemExit)


def test_init_refuses_to_overwrite_and_exits_nonzero(tmp_path: Path):
    first = CliRunner().invoke(main, ["init", "aider", "--dir", str(tmp_path)])
    assert first.exit_code == 0, first.output
    second = CliRunner().invoke(main, ["init", "aider", "--dir", str(tmp_path)])
    assert second.exit_code == 1
    assert "exists" in second.output.lower()


def test_provider_test_failure_exits_nonzero(monkeypatch: pytest.MonkeyPatch):
    _configure_stub_provider()

    class _Result:
        success = False
        message = "refused"
        status_code = None

    class _Adapter:
        def __init__(self, config):
            pass

        def test_connection(self):
            return _Result()

    monkeypatch.setattr("argus.cli.OpenAICompatibleAdapter", _Adapter)
    res = CliRunner().invoke(main, ["provider", "test"])
    assert res.exit_code == 1
    assert "Connection failed" in res.output


# --------------------------------------------------------------------------- UX-015 validation
@pytest.mark.parametrize(
    "extra",
    [
        ["--attempts", "0"],
        ["--attempts", str(MAX_ATTEMPTS_CEILING + 1)],
        ["--budget", "-5"],
        ["--budget", "0"],
        ["--budget", "nan"],
        ["--budget", "inf"],
        ["--budget", "1000"],
        ["--price", "not-a-price"],
        ["--price", "m=1"],
        ["--price", "m=-1,2"],
        ["--price", "m=nan,2"],
    ],
)
def test_run_rejects_invalid_limits_before_doing_anything(extra: List[str], recording_controller):
    res = CliRunner().invoke(main, ["run", "-t", "pytest", "--provider", "antigravity"] + extra)
    assert res.exit_code == 2, res.output
    assert recording_controller.calls == []


def test_run_defaults_come_from_config(recording_controller):
    res = CliRunner().invoke(main, ["run", "-t", "pytest", "--provider", "antigravity"])
    assert res.exit_code == 0, res.output
    cfg = recording_controller.configs[0]
    assert cfg.max_attempts == DEFAULT_MAX_ATTEMPTS
    assert cfg.budget_limit_usd == DEFAULT_BUDGET_USD


def test_run_accepts_attempts_up_to_the_config_ceiling(recording_controller):
    res = CliRunner().invoke(
        main, ["run", "-t", "pytest", "--provider", "antigravity", "-a", str(MAX_ATTEMPTS_CEILING)]
    )
    assert res.exit_code == 0, res.output
    assert recording_controller.configs[0].max_attempts == MAX_ATTEMPTS_CEILING


def test_price_option_reaches_the_config(recording_controller):
    res = CliRunner().invoke(
        main,
        ["run", "-t", "pytest", "--provider", "antigravity", "--price", "vendor/big-model=3,15", "--price", "x=1,2,0.5"],
    )
    assert res.exit_code == 0, res.output
    prices = recording_controller.configs[0].model_prices
    assert prices["vendor/big-model"] == ModelCost(3.0, 15.0)
    assert prices["x"] == ModelCost(1.0, 2.0, 0.5)


# --------------------------------------------------------------------------- UX-014 / workspace_root
def test_workspace_root_is_none_unless_the_user_gave_one(recording_controller, tmp_path: Path, monkeypatch):
    sub = tmp_path / "proj"
    sub.mkdir()
    (sub / "m.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(sub)

    res = CliRunner().invoke(main, ["run", "-t", "pytest", "-f", "m.py", "--provider", "antigravity"])
    assert res.exit_code == 0, res.output
    call = recording_controller.calls[-1]
    assert call["workspace_root"] is None
    assert call["target_file"] == "m.py"  # passed through unchanged so the controller infers the workspace


def test_global_and_local_workspace_flags_are_honoured(recording_controller, tmp_path: Path):
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "m.py").write_text("x = 1\n", encoding="utf-8")

    res = CliRunner().invoke(main, ["-w", str(ws), "run", "-t", "pytest", "-f", "m.py", "--provider", "antigravity"])
    assert res.exit_code == 0, res.output
    call = recording_controller.calls[-1]
    assert Path(call["workspace_root"]) == ws.resolve()
    assert Path(call["target_file"]) == (ws / "m.py").resolve()  # relative -f resolved against the workspace

    other = tmp_path / "other"
    other.mkdir()
    res = CliRunner().invoke(
        main, ["-w", str(ws), "run", "-w", str(other), "-t", "pytest", "--provider", "antigravity"]
    )
    assert res.exit_code == 0, res.output
    assert Path(recording_controller.calls[-1]["workspace_root"]) == other.resolve()  # command -w wins


def test_missing_target_file_is_a_usage_error(recording_controller):
    res = CliRunner().invoke(main, ["run", "-t", "pytest", "-f", "nope.py", "--provider", "antigravity"])
    assert res.exit_code == 2
    assert recording_controller.calls == []


def test_global_workspace_is_used_by_candidate_commands(tmp_path: Path):
    ws = tmp_path / "repo"
    ws.mkdir()
    _save_candidate(ws)
    res = CliRunner().invoke(main, ["-w", str(ws), "candidates"])
    assert res.exit_code == 0, res.output
    assert "run_surface" in res.output
    res = CliRunner().invoke(main, ["-w", str(ws), "inspect", "run_surface"])
    assert res.exit_code == 0, res.output
    assert "Patch tier" in res.output


def test_diff_fallback_reads_last_run_diff_from_the_workspace(tmp_path: Path):
    ws = tmp_path / "repo"
    (ws / ".argus").mkdir(parents=True)
    (ws / ".argus" / "last_run.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
    res = CliRunner().invoke(main, ["-w", str(ws), "diff"])
    assert res.exit_code == 0, res.output
    assert "+++ b" in res.output


# --------------------------------------------------------------------------- output of a run
def test_run_shows_host_warning_policy_notes_and_model_ladder(recording_controller):
    recording_controller.outcome = RecoveryOutcome(
        status="ALREADY_PASSING",
        total_attempts=0,
        total_cost_usd=0.0,
        total_duration_ms=1.0,
        config_notes=["policy: max_attempts tightened to 2"],
        routing_reasons=["simple", "model_ladder=explicit models=m1,m2 escalation=on"],
    )
    res = CliRunner().invoke(main, ["run", "-t", "pytest", "--provider", "antigravity"])
    assert res.exit_code == 0, res.output
    flat = " ".join(res.output.split())
    assert "not sandboxed" in flat
    assert "policy: max_attempts tightened to 2" in flat
    assert "model_ladder=explicit models=m1,m2 escalation=on" in flat


def test_json_events_keep_stdout_clean_and_warn_on_stderr(recording_controller):
    res = CliRunner().invoke(main, ["run", "-t", "pytest", "--provider", "antigravity", "--json-events"])
    assert res.exit_code == 0, res.output
    assert HOST_EXECUTION_WARNING not in res.stdout
    assert "NOTICE" in res.stderr and "not sandboxed" in " ".join(res.stderr.split())


def test_failed_run_exits_nonzero(recording_controller):
    recording_controller.outcome = RecoveryOutcome(
        status="CONFIG_ERROR", total_attempts=0, total_cost_usd=0.0, total_duration_ms=1.0, message="unpriced model"
    )
    res = CliRunner().invoke(main, ["run", "-t", "pytest", "--provider", "antigravity"])
    assert res.exit_code == 1
    assert "unpriced model" in res.output


def test_apply_flag_without_review_authorization_exits_nonzero(recording_controller, tmp_path: Path):
    recording_controller.outcome = RecoveryOutcome(
        status="SUCCESS",
        total_attempts=1,
        total_cost_usd=0.0,
        total_duration_ms=1.0,
        unified_diff="--- a/x\n+++ b/x\n",
        run_id="run_x",
    )
    res = CliRunner().invoke(
        main, ["-w", str(tmp_path), "run", "-t", "pytest", "--provider", "antigravity", "--apply"]
    )
    assert res.exit_code == 1
    assert "Refusing to apply" in res.output


# --------------------------------------------------------------------------- typed candidate errors
def test_tampered_candidate_reports_signature_error_everywhere(tmp_path: Path):
    ws = tmp_path / "repo"
    ws.mkdir()
    _save_candidate(ws)
    meta = ws / ".argus" / "runs" / "run_surface" / "metadata.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["verified"] = True
    data["approved"] = True  # forged approval without a valid signature
    meta.write_text(json.dumps(data), encoding="utf-8")

    for args in (
        ["inspect", "run_surface"],
        ["approve", "run_surface", "-y"],
        ["reject", "run_surface"],
        ["apply", "--run-id", "run_surface"],
    ):
        res = CliRunner().invoke(main, ["-w", str(ws)] + args)
        assert res.exit_code == 1, f"{args}: {res.output}"
        assert "CANDIDATE_SIGNATURE_INVALID" in res.output, f"{args}: {res.output}"
        assert res.exception is None or isinstance(res.exception, SystemExit)
    assert (ws / "calc.py").read_text(encoding="utf-8") == "def f():\n    return 1\n"


def test_invalid_run_id_is_explained(tmp_path: Path):
    res = CliRunner().invoke(main, ["-w", str(tmp_path), "approve", "../escape", "-y"])
    assert res.exit_code == 1
    assert "INVALID_RUN_ID" in res.output


def test_approval_prompt_declined_exits_nonzero_and_changes_nothing(tmp_path: Path):
    ws = tmp_path / "repo"
    ws.mkdir()
    _save_candidate(ws)
    res = CliRunner().invoke(main, ["-w", str(ws), "approve", "run_surface"], input="n\n")
    assert res.exit_code == 1
    meta, _ = CandidateArtifactStore.load_candidate("run_surface", ws)
    assert meta.approved is False


def test_reject_after_apply_says_it_does_not_revert(tmp_path: Path):
    ws = tmp_path / "repo"
    ws.mkdir()
    target = _save_candidate(ws)
    runner = CliRunner()
    assert runner.invoke(main, ["-w", str(ws), "approve", "run_surface", "-y"]).exit_code == 0
    assert runner.invoke(main, ["-w", str(ws), "apply", "--run-id", "run_surface"]).exit_code == 0
    assert "return 2" in target.read_text(encoding="utf-8")
    res = runner.invoke(main, ["-w", str(ws), "reject", "run_surface"])
    assert res.exit_code == 0
    assert "does not revert" in " ".join(res.output.split())


# --------------------------------------------------------------------------- UX-016 provider set
def test_provider_set_api_key_from_env_is_non_interactive(monkeypatch: pytest.MonkeyPatch):
    _configure_stub_provider()  # already active: --no-active must leave it selected
    monkeypatch.setenv("MY_PROVIDER_KEY", "sk-env-123456789")
    res = CliRunner().invoke(main, ["provider", "set", "--preset", "openai", "--api-key-env", "MY_PROVIDER_KEY", "--no-active"])
    assert res.exit_code == 0, res.output
    assert "sk-env-123456789" not in res.output
    assert ProviderStorage().get_provider("openai").api_key == "sk-env-123456789"
    assert ProviderStorage().get_active_provider().name == "stubprov"  # --no-active honoured


def test_provider_set_api_key_from_stdin_flag():
    res = CliRunner().invoke(main, ["provider", "set", "--preset", "openai", "--api-key-stdin"], input="sk-stdin-123456789\n")
    assert res.exit_code == 0, res.output
    assert ProviderStorage().get_provider("openai").api_key == "sk-stdin-123456789"


def test_provider_set_fails_fast_without_a_key_source():
    res = CliRunner().invoke(main, ["provider", "set", "--preset", "openai"], input="")
    assert res.exit_code == 1
    assert "needs an API key" in res.output
    assert ProviderStorage().get_provider("openai").api_key is None  # nothing was saved


def test_provider_set_rejects_conflicting_key_options(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("K", "v")
    res = CliRunner().invoke(main, ["provider", "set", "--preset", "openai", "--api-key", "a", "--api-key-env", "K"])
    assert res.exit_code == 2


def test_provider_set_unset_env_variable_is_an_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NOT_SET_KEY", raising=False)
    res = CliRunner().invoke(main, ["provider", "set", "--preset", "openai", "--api-key-env", "NOT_SET_KEY"])
    assert res.exit_code == 1


# --------------------------------------------------------------------------- UX-017 setup
def test_setup_without_a_terminal_requires_all(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import argus.adapters.discovery as discovery

    client = discovery.DiscoveredClient(
        key="cursor", display_name="Cursor", installed=True, configured=False, detection_reason="x", config_summary=""
    )
    monkeypatch.setattr(discovery, "discover_clients", lambda ws=".": [client])

    def boom(*a, **k):
        raise AssertionError("setup must not write other tools' configs without confirmation")

    monkeypatch.setattr(discovery, "connect_all_detected", boom)
    res = CliRunner().invoke(main, ["-w", str(tmp_path), "setup"])
    assert res.exit_code == 1
    assert "--all" in res.output


# --------------------------------------------------------------------------- doctor
def test_doctor_offline_never_pings_the_provider(monkeypatch: pytest.MonkeyPatch):
    _configure_stub_provider()

    def boom(self):
        raise AssertionError("doctor --offline must not send a request")

    monkeypatch.setattr("argus.cli.OpenAICompatibleAdapter.test_connection", boom)
    res = CliRunner().invoke(main, ["doctor", "--offline"])
    assert res.exit_code == 0, res.output
    assert "skipped" in res.output


def test_doctor_connectivity_failure_exits_nonzero(monkeypatch: pytest.MonkeyPatch):
    _configure_stub_provider()

    class _Result:
        success = False
        message = "unreachable"

    monkeypatch.setattr("argus.cli.OpenAICompatibleAdapter.test_connection", lambda self: _Result())
    res = CliRunner().invoke(main, ["doctor"])
    assert res.exit_code == 1
    assert "unreachable" in res.output
