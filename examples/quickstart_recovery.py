"""Runnable example: one bounded recovery run through the Argus Python API.

This script is self-contained and offline. It creates a throw-away project with a
failing test, then runs the real RecoveryController with a *stub* provider that
returns a fixed SEARCH/REPLACE block instead of calling a model. That shows the
whole flow (isolated workspace, patch reconciliation, test run, candidate
artifact, approval, apply) without any credentials, network or spend.

To use a real model, remove the ``adapter=`` argument and configure a provider
first with ``argus provider set`` (or pass ``provider_config=``). Argus never
picks a provider for you: with none configured the run ends with ``CONFIG_ERROR``.

The test command runs on THIS machine with your user permissions (it is not
sandboxed); here it only runs ``pytest`` against the temporary project.

Run with:  python examples/quickstart_recovery.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
from pathlib import Path

# Keep the example away from the user's real Argus configuration and keyring.
os.environ["ARGUS_CONFIG_DIR"] = tempfile.mkdtemp(prefix="argus_example_cfg_")

from argus.config import ArgusConfig  # noqa: E402
from argus.providers.adapter import CompletionResponse, ConnectionTestResult, ProviderAdapter  # noqa: E402
from argus.runtime.candidate import AuthoritativePatchApplier, CandidateArtifactStore  # noqa: E402
from argus.runtime.controller import RecoveryController  # noqa: E402

BUGGY = "def add(a, b):\n    return a - b\n"
TEST = textwrap.dedent(
    """\
    from calc import add


    def test_add():
        assert add(2, 3) == 5
    """
)
PATCH = textwrap.dedent(
    """\
    <<<<<<< SEARCH calc.py
    def add(a, b):
        return a - b
    =======
    def add(a, b):
        return a + b
    >>>>>>> REPLACE
    """
)


class StubAdapter(ProviderAdapter):
    """Stands in for a model: always answers with the fixed patch above."""

    def chat_completion(self, messages, temperature=None, max_tokens=None, effort=None, **kwargs):
        return CompletionResponse(
            content=PATCH,
            prompt_tokens=100,
            completion_tokens=40,
            duration_ms=1.0,
            model=kwargs.get("model") or "stub-model",
        )

    def test_connection(self) -> ConnectionTestResult:
        return ConnectionTestResult(success=True, status_code=200, message="stub", latency_ms=0.0)


def main() -> int:
    project = Path(tempfile.mkdtemp(prefix="argus_example_project_"))
    (project / "calc.py").write_text(BUGGY, encoding="utf-8")
    (project / "test_calc.py").write_text(TEST, encoding="utf-8")

    # provider="ollama" is a local provider, so the stub model needs no price entry.
    config = ArgusConfig(
        provider="ollama",
        model="stub-model",
        max_attempts=3,
        budget_limit_usd=0.03,
    )
    controller = RecoveryController(config=config, adapter=StubAdapter())
    outcome = controller.run(
        test_command=f'"{sys.executable}" -m pytest -q test_calc.py',
        target_file="calc.py",
        workspace_root=project,
    )

    print(f"status={outcome.status} verified={outcome.verified} tier_used={outcome.tier_used}")
    if outcome.status != "SUCCESS":
        print(outcome.message or outcome.diagnostics)
        return 1
    print(outcome.unified_diff)

    # "verified" means the supplied test command passed in the workspace copy.
    # Applying still needs an explicit approval, then re-checks hashes and syntax.
    CandidateArtifactStore.set_approval(
        outcome.run_id,
        approved=True,
        workspace_root=project,
        approved_by="interactive:example",
        approval_source="example",
    )
    result = AuthoritativePatchApplier.apply(outcome.run_id, workspace_root=project, require_approval=True)
    print(f"applied={result.success} code={result.error_code}")
    print((project / "calc.py").read_text(encoding="utf-8"))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
