"""Close-out regression tests: ARCH-05 (no test run in the real tree before validation),
FIN-05 (signing key hardening) and runner environment scrubbing."""

from __future__ import annotations

from pathlib import Path

import pytest

from argus.config import ArgusConfig
from argus.providers.adapter import CompletionResponse, ConnectionTestResult, ProviderAdapter
from argus.runtime.controller import RecoveryController


class NoopAdapter(ProviderAdapter):
    def __init__(self) -> None:
        self.calls = 0

    def chat_completion(self, messages, temperature=None, max_tokens=None, effort=None, model=None, **kwargs):
        self.calls += 1
        return CompletionResponse(
            content="no patch", prompt_tokens=10, completion_tokens=5, duration_ms=1.0, model=model or "m"
        )

    def test_connection(self) -> ConnectionTestResult:
        return ConnectionTestResult(True, 200, 1.0, "m", "ok")


MARKER_TEST = (
    "import os\n"
    "open(os.path.join(os.getcwd(), 'side_effect.txt'), 'w').write('ran')\n"
    "from mypkg.calculator import compute_total\n"
    "assert compute_total(3, 4) == 12\n"
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "calculator.py").write_text("def compute_total(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "test_side.py").write_text(MARKER_TEST, encoding="utf-8")
    return tmp_path


def _tree(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_no_file_run_validates_config_before_running_any_test_command(repo, monkeypatch):
    # provider configured without any stored credentials and no injected adapter
    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(repo / "_nocfg"))
    before = _tree(repo)
    controller = RecoveryController(config=ArgusConfig(provider="openai", model="gpt-4o-mini"))
    out = controller.run(test_command="python test_side.py", workspace_root=repo)
    assert out.status == "CONFIG_ERROR"
    assert not (repo / "side_effect.txt").exists()
    assert _tree(repo) == before


def test_no_file_run_executes_tests_in_the_isolated_copy_only(repo):
    before = _tree(repo)
    adapter = NoopAdapter()
    cfg = ArgusConfig(provider="openai", model="gpt-4o-mini", max_attempts=1)
    controller = RecoveryController(config=cfg, adapter=adapter)
    out = controller.run(test_command="python test_side.py", workspace_root=repo)
    # localization worked from the copy: the run reached the model
    assert out.target_file in ("mypkg/calculator.py", "mypkg\\calculator.py")
    assert adapter.calls >= 1
    # the real tree was never used as the test cwd and is unchanged
    assert not (repo / "side_effect.txt").exists()
    after = {k: v for k, v in _tree(repo).items() if not k.startswith(".argus")}
    assert after == before


def test_no_file_run_with_container_evaluator_refuses_host_localization(repo):
    class FakeContainer:
        def evaluate(self, *a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("container must not be used for localization")

    controller = RecoveryController(
        config=ArgusConfig(provider="openai", model="gpt-4o-mini"),
        adapter=NoopAdapter(),
        container_evaluator=FakeContainer(),
    )
    out = controller.run(test_command="python test_side.py", workspace_root=repo)
    assert out.status == "LOCALIZATION_FAILED"
    assert "--file" in out.message
    assert not (repo / "side_effect.txt").exists()


# --- FIN-05: signing key hardening ---------------------------------------------------------


def test_empty_or_short_signing_key_file_is_regenerated(monkeypatch, tmp_path):
    from argus.runtime import candidate

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(cfg))
    key_path = cfg / "artifact_signing.key"
    for bad in ("", "abcd", "zz" * 32, "\n"):
        key_path.write_text(bad, encoding="utf-8")
        key = candidate._load_signing_key()
        assert isinstance(key, bytes) and len(key) >= 32
        # persisted so later calls agree
        assert candidate._load_signing_key() == key


# --- FIN-01(a): environment scrubbing of Argus config location -----------------------------


def test_test_command_environment_hides_argus_config_location(monkeypatch, tmp_path):
    from argus.runtime.runner import get_sanitized_environment

    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ARGUS_KEYRING_SERVICE", "svc")
    monkeypatch.setenv("ARGUS_OFFLINE", "1")
    env = get_sanitized_environment(tmp_path)
    assert "ARGUS_CONFIG_DIR" not in env
    assert "ARGUS_KEYRING_SERVICE" not in env
    assert env.get("ARGUS_OFFLINE") == "1"  # unrelated settings still pass through


# --- FIN-02: TUI model selection pins the model ---------------------------------------------


def test_pin_model_overrides_provider_cascade():
    cfg = ArgusConfig(provider="openai")
    assert cfg.ladder_source == "provider-cascade"
    cascade = [cfg.model_for_tier(t) for t in (1, 2, 3)]
    assert len(set(cascade)) > 1  # small -> large rungs differ
    cfg.pin_model("gpt-4o")
    assert [cfg.model_for_tier(t) for t in (1, 2, 3)] == ["gpt-4o"] * 3
    assert cfg.ladder_source == "pinned-model"
    cfg.pin_model("")  # un-pin returns to the documented provider default/cascade
    assert cfg.ladder_source in ("provider-cascade", "provider-default")


@pytest.mark.asyncio
async def test_tui_model_choice_pins_the_model(tmp_path):
    from argus.tui.app import ArgusTUIApp

    app = ArgusTUIApp(workspace=str(tmp_path), config=ArgusConfig(provider="openai"))
    async with app.run_test() as pilot:
        await pilot.pause()
        captured = {}
        real_push = app.push_screen
        app.push_screen = lambda screen, callback=None: captured.update(cb=callback)  # type: ignore[assignment]
        app._open_config_modal("model")
        app.push_screen = real_push  # type: ignore[assignment]
        captured["cb"]({"type": "model", "value": "gpt-4o"})
        assert [app.config.model_for_tier(t) for t in (1, 2, 3)] == ["gpt-4o"] * 3


def test_tui_persisted_policy_model_pins_the_model(tmp_path, monkeypatch):
    import json

    from argus.tui.app import ArgusTUIApp

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "policy.json").write_text(json.dumps({"model": "gpt-4o"}), encoding="utf-8")
    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(cfg_dir))
    app = ArgusTUIApp(workspace=str(tmp_path), config=ArgusConfig(provider="openai"))
    assert [app.config.model_for_tier(t) for t in (1, 2, 3)] == ["gpt-4o"] * 3


def test_set_ladder_configures_escalation_and_clears_pin():
    cfg = ArgusConfig(provider="openai")
    cfg.pin_model("gpt-4o")
    assert cfg.ladder_source == "pinned-model"

    cfg.set_ladder("gpt-4o-mini", "gpt-4o")
    assert cfg.ladder_source == "explicit-ladder"
    assert cfg.model_for_tier(1) == "gpt-4o-mini"
    assert cfg.model_for_tier(2) == "gpt-4o-mini"
    assert cfg.model_for_tier(3) == "gpt-4o"

    cfg.set_ladder(None, None)
    assert cfg.ladder_source == "provider-cascade"


@pytest.mark.asyncio
async def test_tui_policy_auto_preserves_escalation_across_save_and_reload(tmp_path, monkeypatch):
    from unittest.mock import patch

    from argus.tui.app import ArgusTUIApp

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(cfg_dir))

    tiers = {
        "t1": {"name": "T1 FAST", "model": "m1"},
        "t2": {"name": "T2 STRONG", "model": "m2"},
        "t3": {"name": "T3 FRONTIER", "model": "m3"},
    }
    app = ArgusTUIApp(workspace=str(tmp_path), config=ArgusConfig(provider="openai"))
    with patch("argus.tui.modals.policy.resolve_dynamic_tiers", return_value=tiers):
        async with app.run_test() as pilot:
            await pilot.pause()
            app.open_policy_modal()
            await pilot.pause()
            app.screen.dismiss(
                {
                    "tier": "argus/auto",
                    "budget": 0.50,
                    "attempts": 3,
                    "t1_model": "m1",
                    "t2_model": "m2",
                    "t3_model": "m3",
                }
            )
            await pilot.pause()

    assert app.current_tier == "argus/auto"
    assert app.config.ladder_source == "explicit-ladder"
    assert app.config.model_for_tier(1) == "m1"
    assert app.config.model_for_tier(2) == "m1"
    assert app.config.model_for_tier(3) == "m3"

    reloaded = ArgusTUIApp(workspace=str(tmp_path), config=ArgusConfig(provider="openai"))
    assert reloaded.current_tier == "argus/auto"
    assert reloaded.config.ladder_source == "explicit-ladder"
    assert reloaded.config.model_for_tier(1) == "m1"
    assert reloaded.config.model_for_tier(3) == "m3"


def test_tui_reloaded_legacy_auto_policy_does_not_pin_model(tmp_path, monkeypatch):
    import json

    from argus.tui.app import ArgusTUIApp

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "policy.json").write_text(
        json.dumps({"tier": "argus/auto", "model": "gpt-4o-mini"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ARGUS_CONFIG_DIR", str(cfg_dir))
    app = ArgusTUIApp(workspace=str(tmp_path), config=ArgusConfig(provider="openai"))
    assert app.config.ladder_source == "provider-cascade"
    assert app.config.model_for_tier(1) == "gpt-4o-mini"
    assert app.config.model_for_tier(3) == "gpt-4o"


@pytest.mark.asyncio
async def test_tui_config_tier_auto_restores_escalation_after_pinned_model(tmp_path):
    from unittest.mock import patch

    from argus.tui.app import ArgusTUIApp

    tiers = {
        "t1": {"name": "T1 FAST", "model": "m1"},
        "t2": {"name": "T2 STRONG", "model": "m2"},
        "t3": {"name": "T3 FRONTIER", "model": "m3"},
    }
    app = ArgusTUIApp(workspace=str(tmp_path), config=ArgusConfig(provider="openai", model="gpt-4o"))
    assert app.config.ladder_source == "pinned-model"

    with patch("argus.providers.discovery.resolve_dynamic_tiers", return_value=tiers):
        async with app.run_test() as pilot:
            await pilot.pause()
            captured = {}
            real_push = app.push_screen
            app.push_screen = lambda screen, callback=None: captured.update(cb=callback)  # type: ignore[assignment]
            app._open_config_modal("tier")
            app.push_screen = real_push  # type: ignore[assignment]
            captured["cb"]({"type": "tier", "value": "argus/auto"})

    assert app.current_tier == "argus/auto"
    assert app.config.ladder_source == "explicit-ladder"
    assert app.config.model_for_tier(1) == "m1"
    assert app.config.model_for_tier(3) == "m3"


# --- UX-021 / ARCH-12 partials ---------------------------------------------------------------


def test_persistence_failure_is_reported_not_swallowed(repo, monkeypatch):
    from argus.runtime.candidate import CandidateArtifactStore

    def boom(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(CandidateArtifactStore, "save_run_artifact", staticmethod(boom))
    controller = RecoveryController(
        config=ArgusConfig(provider="openai", model="gpt-4o-mini", max_attempts=1), adapter=NoopAdapter()
    )
    out = controller.run(
        target_file="mypkg/calculator.py", test_command="python test_side.py", workspace_root=repo
    )
    assert "run artifacts could not be saved" in out.message
    assert "disk full" in out.message


@pytest.mark.parametrize("status,label", [(503, "Provider error (503)"), (504, "Inference timeout")])
def test_post_dispatch_failures_are_labelled_by_kind(repo, status, label):
    from argus.providers.adapter import ProviderError

    class Failing(NoopAdapter):
        def chat_completion(self, *a, **k):
            raise ProviderError("upstream failed", status_code=status)

    controller = RecoveryController(
        config=ArgusConfig(provider="openai", model="gpt-4o-mini", max_attempts=1), adapter=Failing()
    )
    out = controller.run(
        target_file="mypkg/calculator.py", test_command="python test_side.py", workspace_root=repo
    )
    assert out.history and out.history[0].error_summary.startswith(label)


# --- ARCH-09: the core never imports the optional surfaces ------------------------------------------


def test_core_import_does_not_load_optional_surfaces():
    import os
    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parent.parent
    code = (
        "import sys, argus, argus.cli, argus.runtime.controller\n"
        "bad = sorted(m for m in sys.modules if m.startswith(('argus.gateway', 'argus.api', 'argus.ui', 'argus.tui')))\n"
        "print('BAD:' + ','.join(bad))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(repo_root))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("BAD:"), proc.stdout
