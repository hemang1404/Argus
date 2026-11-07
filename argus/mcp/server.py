"""Argus Model Context Protocol (MCP) Server for Host Agents.

Provides stdio-based tool execution for Antigravity, OpenCode, and Gemini CLI.
Stdout is strictly reserved for JSON-RPC 2.0 protocol traffic; all diagnostics go to stderr.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from argus.config import ArgusConfig
from argus.providers.models import ProviderConfig
from argus.providers.storage import ProviderStorage
from argus.runtime.candidate import AuthoritativePatchApplier, CandidateArtifactStore
from argus.runtime.controller import RecoveryController, RecoveryOutcome, validate_target_file

# Conditionally load .env only if explicitly enabled
from argus.providers.storage import maybe_load_dotenv

maybe_load_dotenv()

# Configure logging exclusively to sys.stderr so stdout remains pure JSON-RPC
logger = logging.getLogger("argus.mcp")
logger.setLevel(logging.INFO)
if not logger.handlers:
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter("[argus-mcp] %(levelname)s: %(message)s"))
    logger.addHandler(stderr_handler)


def create_mcp_server() -> FastMCP:
    """Instantiate and configure the Argus FastMCP server with tools."""
    server = FastMCP(
        name="argus",
        instructions="Argus: Execution-feedback test recovery and resilient patch runtime.",
    )

    @server.tool(name="argus_status", description="Report Argus provider configuration, active model, and runtime readiness without exposing secrets.")
    def argus_status() -> str:
        """Report runtime health, active model, and provider configuration status."""
        try:
            storage = ProviderStorage()
            active_cfg = storage.get_active_provider()
            git_found = shutil.which("git") is not None
            pytest_found = shutil.which("pytest") is not None

            # Count candidates in current workspace
            candidates = CandidateArtifactStore.list_candidates()
            verified_count = sum(1 for c in candidates if c.verified)
            approved_count = sum(1 for c in candidates if c.approved)

            from argus.providers.antigravity import find_antigravity_cli
            host_cli = find_antigravity_cli()
            host_cli_found = host_cli is not None

            # Status reports host availability but never selects it implicitly.
            # Explicit provider selection belongs to argus_recover's caller.
            if active_cfg is None:
                status_payload = {
                    "runtime_ready": False,
                    "active_provider": {
                        "configured": False,
                        "name": None,
                        "base_url": None,
                        "model": None,
                        "key_configured": False,
                        "masked_key": "(none)",
                    },
                    "environment": {
                        "python": sys.version.split()[0],
                        "git_available": git_found,
                        "pytest_available": pytest_found,
                        "antigravity_cli_available": host_cli_found,
                    },
                    "workspace": {
                        "total_candidates": len(candidates),
                        "verified_candidates": verified_count,
                        "approved_candidates": approved_count,
                    },
                    "message": "Argus is installed, but no LLM provider is configured. Run 'argus provider set' to configure a provider.",
                }
                return json.dumps(status_payload, indent=2)

            status_payload = {
                "runtime_ready": True,
                "active_provider": {
                    "configured": True,
                    "name": active_cfg.name,
                    "base_url": active_cfg.base_url,
                    "model": active_cfg.model,
                    "key_configured": bool(active_cfg.api_key),
                    "masked_key": active_cfg.masked_key,
                },
                "environment": {
                    "python": sys.version.split()[0],
                    "git_available": git_found,
                    "pytest_available": pytest_found,
                    "antigravity_cli_available": host_cli_found,
                },
                "workspace": {
                    "total_candidates": len(candidates),
                    "verified_candidates": verified_count,
                    "approved_candidates": approved_count,
                },
                "message": f"Argus ready. Active provider: {active_cfg.name} ({active_cfg.model}).",
            }
            return json.dumps(status_payload, indent=2)
        except Exception as ex:
            logger.error(f"Error in argus_status: {ex}")
            return json.dumps({"runtime_ready": False, "error": str(ex)}, indent=2)

    @server.tool(
        name="argus_recover",
        description="Run an isolated recovery attempt on a failing test. If target_file is omitted, Argus auto-localizes the buggy file. Uses the user's platform subscription (e.g. Antigravity) or explicitly configured provider/model without developer keys.",
    )
    def argus_recover(
        test_command: str,
        target_file: Optional[str] = None,
        test_file: Optional[str] = None,
        workspace: Optional[str] = None,
        max_attempts: int = 3,
        budget: float = 0.05,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> str:
        """Execute a non-destructive recovery loop in an isolated workspace.
        
        Args:
            test_command: Command used to execute tests (e.g. 'pytest tests/test_calc.py').
            target_file: Optional path to the buggy source file to repair. If omitted, Argus auto-localizes the file from test failures.
            test_file: Optional path to the test file to copy to the isolated workspace.
            workspace: Optional root path of the project. Defaults to current working directory.
            max_attempts: Maximum recovery turns (capped at 3).
            budget: Maximum dollar budget for inference (e.g. 0.05).
            model: Optional model ID to use (e.g. 'gemini-2.5-pro' or 'claude-3-5-sonnet'). Defaults to platform model.
            provider: Optional provider name (e.g. 'antigravity', 'openai', 'deepseek').
        """
        logger.info(f"argus_recover triggered: test='{test_command}', target='{target_file or '(auto-localize)'}', provider={provider}, model={model}")
        try:
            ws_root = Path(workspace).resolve() if workspace else Path.cwd().resolve()
            target_path = None
            if target_file:
                target_path = Path(target_file)
                if not target_path.is_absolute():
                    target_path = ws_root / target_path

                if not target_path.is_file():
                    return json.dumps({
                        "status": "TARGET_NOT_FOUND",
                        "error": f"Target file '{target_file}' does not exist on disk.",
                    }, indent=2)

            target_validation_err = (
                validate_target_file(
                    target_path=target_path,
                    test_file=test_file,
                    test_command=test_command,
                    workspace_root=ws_root,
                )
                if target_path is not None
                else None
            )
            if target_validation_err:
                return json.dumps({
                    "status": "INVALID_TARGET",
                    "verified": False,
                    "run_id": None,
                    "message": f"SECURITY REFUSAL: {target_validation_err}",
                }, indent=2)

            # Cap max_attempts to at most 3 turns
            bounded_attempts = max(1, min(int(max_attempts), 3))

            storage = ProviderStorage()
            active_cfg: Optional[ProviderConfig] = None

            # Explicit provider selection policy. Only the explicit 'auto'
            # request may discover the host runtime; missing configuration never
            # silently selects Antigravity or any other provider.
            if provider:
                p_norm = provider.strip().lower()
                if p_norm == "groq":
                    return json.dumps({
                        "status": "CONFIG_ERROR",
                        "error": "Groq is strictly prohibited as an inference provider in Argus. Please configure an authorized provider (e.g. ollama, openai, deepseek, antigravity).",
                    }, indent=2)
                elif p_norm == "auto":
                    active_cfg = storage.get_active_provider()
                    if active_cfg is None:
                        from argus.providers.antigravity import find_antigravity_cli
                        if find_antigravity_cli() is not None:
                            active_cfg = ProviderConfig.from_preset("antigravity", model=model)
                    if active_cfg is None:
                        return json.dumps({
                            "status": "CONFIG_ERROR",
                            "error": "Explicit 'auto' provider discovery found no active provider or host runtime.",
                        }, indent=2)
                elif p_norm == "antigravity":
                    active_cfg = ProviderConfig.from_preset("antigravity", model=model)
                else:
                    active_cfg = storage.get_provider(provider)
                    if active_cfg and model:
                        active_cfg.model = model
                if active_cfg is None:
                    return json.dumps({
                        "status": "CONFIG_ERROR",
                        "error": f"Requested provider '{provider}' is not configured in Argus. Run 'argus provider set {provider}' or specify an authorized provider.",
                    }, indent=2)
            else:
                active_cfg = storage.get_active_provider()
                if active_cfg is not None and active_cfg.name.lower() == "groq":
                    active_cfg = None
                if active_cfg is not None and model:
                    active_cfg.model = model

            # Fail fast if no authorized provider is available (never silent Groq fallback).
            if active_cfg is None:
                return json.dumps({
                    "status": "CONFIG_ERROR",
                    "error": "No eligible inference provider configured. Specify 'provider', use 'auto' explicitly, or configure via 'argus provider set'.",
                }, indent=2)

            config = ArgusConfig(
                max_attempts=bounded_attempts,
                budget_limit_usd=budget,
                provider_config=active_cfg,
            )
            controller = RecoveryController(config=config)

            outcome: RecoveryOutcome = controller.run(
                target_file=target_path,
                test_command=test_command,
                test_file=test_file,
                workspace_root=ws_root,
            )

            result_payload = {
                "run_id": outcome.run_id,
                "status": outcome.status,
                "verified": outcome.verified,
                "effort_used": outcome.effort_used,
                "routing_reasons": outcome.routing_reasons,
                "attempts": outcome.total_attempts,
                "cost_usd": outcome.total_cost_usd,
                "duration_ms": outcome.total_duration_ms,
                "target_file": outcome.target_file or str(target_file or ""),
                "changed_files": outcome.changed_files,
                "tests_executed": outcome.tests_executed,
                "diff": outcome.unified_diff,
                "diagnostics": outcome.diagnostics,
                "message": outcome.message,
                "next_step": (
                    f"Candidate '{outcome.run_id}' created. Your original file remains untouched. "
                    f"To apply this patch, the user must review the diff and run 'argus approve {outcome.run_id}' "
                    f"in their terminal before calling 'argus_apply_candidate'."
                    if outcome.verified else "Recovery attempt did not produce a passing patch."
                ),
            }
            return json.dumps(result_payload, indent=2)
        except Exception as ex:
            logger.error(f"Error in argus_recover: {ex}")
            return json.dumps({"status": "ERROR", "error": str(ex)}, indent=2)

    @server.tool(name="argus_get_candidate", description="Retrieve the details, metadata, and unified diff for a recovery candidate.")
    def argus_get_candidate(run_id: str, workspace: Optional[str] = None) -> str:
        """Inspect a specific candidate patch generated by Argus."""
        try:
            ws_root = Path(workspace).resolve() if workspace else Path.cwd().resolve()
            metadata, _ = CandidateArtifactStore.load_candidate(run_id, workspace_root=ws_root)
            diff_text = CandidateArtifactStore.get_candidate_diff(run_id, workspace_root=ws_root)

            payload = {
                "run_id": metadata.run_id,
                "target_file": metadata.target_file,
                "test_command": metadata.test_command,
                "status": metadata.status,
                "verified": metadata.verified,
                "approved": metadata.approved,
                "approved_at": metadata.approved_at,
                "applied": metadata.applied,
                "applied_at": metadata.applied_at,
                "attempts": metadata.attempts,
                "cost_usd": metadata.cost_usd,
                "created_at": metadata.created_at,
                "diff": diff_text,
            }
            return json.dumps(payload, indent=2)
        except Exception as ex:
            return json.dumps({"error": f"Failed to load candidate '{run_id}': {ex}"}, indent=2)

    @server.tool(name="argus_apply_candidate", description="Apply a verified candidate patch that has already received human approval to the source tree.")
    def argus_apply_candidate(run_id: str, workspace: Optional[str] = None) -> str:
        """Authoritatively apply an approved candidate patch to the target source file.
        
        Enforces:
        - Candidate must have passed all tests (verified == True).
        - Candidate must have received explicit human review and approval (approved == True).
        - Target file baseline hash must match the pre-repair hash (no stale modifications).
        """
        logger.info(f"argus_apply_candidate invoked for run_id='{run_id}'")
        try:
            ws_root = Path(workspace).resolve() if workspace else Path.cwd().resolve()
            apply_res = AuthoritativePatchApplier.apply(
                run_id=run_id,
                workspace_root=ws_root,
                require_approval=True,
            )

            payload = {
                "success": apply_res.success,
                "run_id": apply_res.run_id,
                "target_file": apply_res.target_file,
                "message": apply_res.message,
                "error_code": apply_res.error_code,
                "applied_at": apply_res.applied_at,
            }
            return json.dumps(payload, indent=2)
        except Exception as ex:
            logger.error(f"Error in argus_apply_candidate: {ex}")
            return json.dumps({"success": False, "error": str(ex)}, indent=2)

    return server


def run_mcp_server() -> None:
    """Launch the Argus MCP server over stdio."""
    # Ensure stdout encoding is utf-8
    if sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    server = create_mcp_server()
    server.run(transport="stdio")
