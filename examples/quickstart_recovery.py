"""Minimal verified example: running an isolated patch recovery using the Argus Python API."""

from pathlib import Path
from argus.config import ArgusConfig
from argus.runtime.controller import RecoveryController

def main() -> None:
    workspace = Path.cwd()
    config = ArgusConfig(
        budget_limit_usd=0.03,
        max_attempts=3,
        isolation_mode="worktree",
    )
    controller = RecoveryController(workspace_root=workspace, config=config)
    print("Argus RecoveryController initialized successfully.")
    print(f"Budget ceiling: ${config.budget_limit_usd:.2f} USD")
    print(f"Sandbox mode: {config.isolation_mode}")

if __name__ == "__main__":
    main()
