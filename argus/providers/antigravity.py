"""Antigravity Host Runtime Adapter.

Provides native inference and adaptive reasoning effort routing through the host's
authenticated Antigravity CLI without requiring separate third-party API keys.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from argus.providers.adapter import CompletionResponse, ConnectionTestResult, ProviderAdapter, ProviderError
from argus.providers.models import ProviderConfig
from argus.providers.storage import scrub_secrets

logger = logging.getLogger("argus.providers.antigravity")

# Effort to model tier mapping for environments utilizing agentapi or tiered aliases
EFFORT_TIER_MAP = {
    "low": "flash_lite",
    "medium": "flash",
    "high": "pro",
}

DEFAULT_ANTIGRAVITY_MODEL = "gemini-2.5-flash"


def is_antigravity_cli_available(custom_path: Optional[str | Path] = None) -> bool:
    """Return True if Antigravity CLI ('agy') binary or script is found on the host."""
    return find_antigravity_cli(custom_path) is not None


def find_antigravity_cli(custom_path: Optional[str | Path] = None) -> Optional[Path]:
    """Locate the Antigravity CLI ('agy') binary or script.
    
    Resolution order:
    1. Explicit custom_path passed to function
    2. ARGUS_AGY_BIN environment variable
    3. System PATH lookup for 'agy', 'agy.cmd', 'agy.exe', 'agy.bat'
    4. Well-known user profile locations:
       - ~/AppData/Local/agy/bin/agy.exe (.cmd, .bat) (Official Windows installation)
       - ~/.gemini/antigravity/bin/agy.cmd (.exe, .bat)
       - ~/.gemini/bin/agy.cmd (.exe, .bat)
       - ~/AppData/Roaming/npm/agy.cmd
       - ~/.gemini/antigravity-ide/bin/agentapi.bat
    5. Local repository bin/ directory:
       - ./bin/agy.cmd or ./bin/agy
    """
    if custom_path:
        p = Path(custom_path)
        if p.is_file() or (shutil.which(str(p)) is not None):
            return p.resolve() if p.is_file() else Path(shutil.which(str(p)))
        return None

    env_bin = os.getenv("ARGUS_AGY_BIN")
    if env_bin:
        p = Path(env_bin)
        if p.is_file() or (shutil.which(str(p)) is not None):
            return p.resolve() if p.is_file() else Path(shutil.which(str(p)))
        return None

    for candidate in ("agy", "agy.exe", "agy.cmd", "agy.bat"):
        found = shutil.which(candidate)
        if found:
            return Path(found).resolve()

    home = Path.home()
    known_locations = [
        home / "AppData" / "Local" / "agy" / "bin" / "agy.exe",
        home / "AppData" / "Local" / "agy" / "bin" / "agy.cmd",
        home / "AppData" / "Local" / "agy" / "bin" / "agy.bat",
        home / ".gemini" / "antigravity" / "bin" / "agy.cmd",
        home / ".gemini" / "antigravity" / "bin" / "agy.exe",
        home / ".gemini" / "antigravity" / "bin" / "agy.bat",
        home / ".gemini" / "bin" / "agy.cmd",
        home / ".gemini" / "bin" / "agy.exe",
        home / "AppData" / "Roaming" / "npm" / "agy.cmd",
        Path.cwd() / "bin" / "agy.cmd",
        Path.cwd() / "bin" / "agy",
        home / ".gemini" / "antigravity-ide" / "bin" / "agentapi.bat",
    ]
    for loc in known_locations:
        if loc.is_file():
            return loc.resolve()

    for candidate in ("agentapi", "agentapi.bat"):
        found = shutil.which(candidate)
        if found:
            return Path(found).resolve()

    return None


class AntigravityHostAdapter(ProviderAdapter):
    """Adapter for host-native inference via Antigravity authenticated runtime."""

    def __init__(
        self,
        config: Optional[ProviderConfig] = None,
        cli_path: Optional[str | Path] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self.config = config or ProviderConfig(
            name="antigravity",
            base_url="host://antigravity-cli",
            model=DEFAULT_ANTIGRAVITY_MODEL,
        )
        self._custom_cli_path = cli_path
        self._cli_path: Optional[Path] = None
        self._timeout_seconds = timeout_seconds or getattr(self.config, "timeout_seconds", 60.0)
        self._supported_efforts: Optional[set[str]] = None
        self._available_models: Optional[list[str]] = None
        self._is_agentapi: bool = False

    def get_cli_path(self) -> Path:
        """Resolve CLI path or raise clear descriptive ProviderError."""
        if self._cli_path and self._cli_path.is_file():
            return self._cli_path

        resolved = find_antigravity_cli(self._custom_cli_path)
        if resolved is None:
            raise ProviderError(
                "Antigravity CLI ('agy') is not found on this machine. "
                "Please verify Antigravity is installed and accessible in PATH, "
                "or set the ARGUS_AGY_BIN environment variable to its location.",
                status_code=404,
            )

        self._cli_path = resolved
        self._is_agentapi = "agentapi" in resolved.name.lower()
        return self._cli_path

    def _get_base_cmd(self) -> List[str]:
        """Return base command list. On Windows, directly invoke companion python script with sys.executable if present."""
        cli = self.get_cli_path()
        if cli.suffix.lower() in (".cmd", ".bat") and (cli.parent / f"{cli.stem}.py").is_file():
            return [sys.executable, str(cli.parent / f"{cli.stem}.py")]
        return [str(cli)]

    def discover_capabilities(self) -> Dict[str, Any]:
        """Probe the Antigravity CLI for available models and effort options."""
        cli = self.get_cli_path()
        capabilities: Dict[str, Any] = {
            "cli_path": str(cli),
            "supports_effort": True,
            "models": [],
            "is_agentapi": self._is_agentapi,
        }

        # Probe CLI models/help
        try:
            # First try 'agy models'
            probe_cmd = self._get_base_cmd() + ["models"]
            res = subprocess.run(
                probe_cmd,
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
                stdin=subprocess.DEVNULL,
            )
            if res.returncode == 0 and res.stdout.strip():
                models = [line.strip().split()[0] for line in res.stdout.strip().splitlines() if line.strip()]
                capabilities["models"] = models
                self._available_models = models
        except Exception:
            pass

        # Check effort support in help
        try:
            help_cmd = [str(cli), "--help"]
            res_help = subprocess.run(
                help_cmd,
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
                stdin=subprocess.DEVNULL,
            )
            out_help = res_help.stdout + res_help.stderr
            if "--effort" in out_help:
                capabilities["supports_effort"] = True
                self._supported_efforts = {"low", "medium", "high"}
            elif self._is_agentapi or "--model" in out_help:
                capabilities["supports_effort"] = True
                self._supported_efforts = {"low", "medium", "high"}
            else:
                # Conservative fallback if CLI is a legacy variant
                capabilities["supports_effort"] = False
                self._supported_efforts = set()
        except Exception:
            pass

        return capabilities

    def test_connection(self) -> ConnectionTestResult:
        """Verify reachability and authentication of the Antigravity host runtime."""
        start_time = time.perf_counter()
        try:
            cli = self.get_cli_path()
        except ProviderError as ex:
            return ConnectionTestResult(
                success=False,
                status_code=404,
                latency_ms=0.0,
                model="(none)",
                message=str(ex),
            )

        # Probe CLI execution
        try:
            # Check models or version
            probe_args = self._get_base_cmd() + (["models"] if not self._is_agentapi else ["--help"])
            res = subprocess.run(
                probe_args,
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
                stdin=subprocess.DEVNULL,
            )
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)

            # Check for authentication errors
            full_out = (res.stdout + " " + res.stderr).strip()
            auth_keywords = ["unauthenticated", "login required", "not authenticated", "invalid credentials", "permission denied"]
            if any(k in full_out.lower() for k in auth_keywords) or (res.returncode != 0 and "auth" in full_out.lower()):
                return ConnectionTestResult(
                    success=False,
                    status_code=401,
                    latency_ms=latency_ms,
                    model=self.config.model or "host-default",
                    message="Antigravity host is unauthenticated. Please open Antigravity or run 'agy' to authenticate.",
                )

            if res.returncode != 0:
                return ConnectionTestResult(
                    success=False,
                    status_code=502,
                    latency_ms=latency_ms,
                    model=self.config.model or "host-default",
                    message=f"Antigravity CLI probe failed (exit code {res.returncode}): {full_out[:120].strip()}",
                    response_sample=full_out[:120].strip(),
                )

            active_model = self.config.model or DEFAULT_ANTIGRAVITY_MODEL
            return ConnectionTestResult(
                success=True,
                status_code=200,
                latency_ms=latency_ms,
                model=active_model,
                message=f"Antigravity host runtime verified online via '{cli.name}'.",
                response_sample=full_out[:120].strip(),
            )
        except subprocess.TimeoutExpired:
            return ConnectionTestResult(
                success=False,
                status_code=504,
                latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                model="(timeout)",
                message=f"Antigravity CLI probe timed out after 10s at '{cli}'.",
            )
        except Exception as ex:
            return ConnectionTestResult(
                success=False,
                status_code=500,
                latency_ms=round((time.perf_counter() - start_time) * 1000, 2),
                model="(error)",
                message=f"Failed to execute Antigravity host CLI: {ex}",
            )

    def _build_command(
        self,
        full_prompt: str,
        effort_val: str,
        active_model: Optional[str],
        output_format: str = "json",
    ) -> tuple[List[str], str]:
        cmd_args: List[str] = self._get_base_cmd()

        if self._is_agentapi:
            tier_model = EFFORT_TIER_MAP.get(effort_val, "flash")
            cmd_args.extend(["new-conversation", f"--model={tier_model}", full_prompt])
            return cmd_args, active_model or tier_model
        else:
            cmd_args.extend(["--output-format", output_format])
            # Strict safety flags: enforce terminal restrictions and disable slash commands/skills
            cmd_args.append("--sandbox")
            cmd_args.append("--disable-slash-commands")

            model_alias_map = {
                "gemini-2.5-flash": "gemini-3.8-flash-low",
                "gemini-2.5-flash-lite": "gemini-3.8-flash-low",
                "gemini-2.5-pro": "gemini-3.8-flash-high",
                "gemini-3-flash": "gemini-3.8-flash-medium",
                "gemini-3-pro": "gemini-3.8-flash-high",
                "gemini-3.8-flash": f"gemini-3.8-flash-{effort_val}",
                "gemini-3.8-pro": "gemini-3.8-flash-high",
                "flash": f"gemini-3.8-flash-{effort_val}",
                "pro": "gemini-3.8-flash-high",
                "cheap": "gemini-3.8-flash-low",
                "strong": "gemini-3.8-flash-medium",
                "frontier": "gemini-3.8-flash-high",
            }

            resolved_model = model_alias_map.get((active_model or "").lower(), active_model) if active_model else f"gemini-3.8-flash-{effort_val}"
            if resolved_model and resolved_model not in ("host-default", "antigravity"):
                cmd_args.extend(["--model", resolved_model])

            if not any(resolved_model.endswith(f"-{e}") for e in ("low", "medium", "high", "xhigh", "max")):
                cmd_args.extend(["--effort", effort_val])

            cmd_args.extend(["-p", full_prompt])
            return cmd_args, resolved_model

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        effort: Optional[str] = None,
        model: Optional[str] = None,
        cwd: Optional[str | Path] = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        """Dispatch inference through Antigravity host CLI with per-run effort."""
        cli = self.get_cli_path()

        # Extract prompt content from messages
        prompt_parts: List[str] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                prompt_parts.append(f"System Instructions:\n{content}\n")
            elif role == "user":
                prompt_parts.append(f"User Request:\n{content}\n")
            elif role == "assistant":
                prompt_parts.append(f"Previous Assistant Response:\n{content}\n")

        full_prompt = "\n".join(prompt_parts).strip()
        if not full_prompt and messages:
            full_prompt = messages[-1].get("content", "")
        full_prompt = scrub_secrets(full_prompt)

        # Determine effort parameter
        effort_val = (effort or "medium").strip().lower()
        if effort_val not in ("low", "medium", "high"):
            effort_val = "medium"

        active_model = model or kwargs.get("model") or self.config.model

        # Enforce working directory containment
        effective_cwd = (
            cwd
            or kwargs.get("workspace_root")
            or getattr(self.config, "workspace_root", None)
        )
        if effective_cwd:
            effective_cwd = str(Path(effective_cwd).resolve())
        else:
            import tempfile
            effective_cwd = tempfile.gettempdir()

        cmd_args, resolved_model = self._build_command(
            full_prompt=full_prompt,
            effort_val=effort_val,
            active_model=active_model,
            output_format="json",
        )

        start_time = time.perf_counter()

        try:
            # Launch process without shell string concatenation or shell=True
            proc = subprocess.run(
                cmd_args,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                shell=False,
                cwd=effective_cwd,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            dur = (time.perf_counter() - start_time) * 1000
            err_msg = f"Antigravity host CLI invocation timed out after {self._timeout_seconds}s."
            raise ProviderError(err_msg, status_code=504) from None
        except Exception as ex:
            dur = (time.perf_counter() - start_time) * 1000
            err_msg = scrub_secrets(f"Failed to launch Antigravity CLI process: {ex}")
            raise ProviderError(err_msg) from None

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        # Inspect return code and errors
        if proc.returncode != 0:
            stderr_preview = proc.stderr.strip()[:300] or proc.stdout.strip()[:300] or f"Exit code {proc.returncode}"
            scrubbed_err = scrub_secrets(stderr_preview)

            # Check if failure is due to unsupported --effort flag, apply documented fallback
            if "--effort" in cmd_args and ("unknown flag" in scrubbed_err.lower() or "unrecognized option" in scrubbed_err.lower()):
                logger.warning(f"Antigravity CLI did not accept '--effort {effort_val}'. Retrying without explicit effort flag.")
                # Fallback: Retry once without --effort
                retry_args = [arg for arg in cmd_args if arg not in ("--effort", effort_val)]
                try:
                    proc_retry = subprocess.run(
                        retry_args,
                        capture_output=True,
                        text=True,
                        timeout=self._timeout_seconds,
                        shell=False,
                        cwd=effective_cwd,
                        stdin=subprocess.DEVNULL,
                    )
                    if proc_retry.returncode == 0:
                        proc = proc_retry
                    else:
                        raise ProviderError(
                            f"Antigravity CLI failed during fallback invocation (exit code {proc_retry.returncode}): "
                            f"{scrub_secrets(proc_retry.stderr[:200])}",
                            status_code=proc_retry.returncode,
                        )
                except Exception as retry_ex:
                    raise ProviderError(f"Antigravity CLI fallback failed: {retry_ex}") from None

            if proc.returncode != 0:
                # Check for authentication errors
                if any(w in scrubbed_err.lower() for w in ["unauthenticated", "login required", "not authenticated", "auth"]):
                    raise ProviderError(
                        f"Antigravity authentication error: {scrubbed_err}. "
                        "Please authenticate via the Antigravity CLI ('agy') or Antigravity IDE.",
                        status_code=401,
                    )
                raise ProviderError(
                    f"Antigravity CLI process failed with code {proc.returncode}: {scrubbed_err}",
                    status_code=proc.returncode,
                )

        output_text = proc.stdout.strip()
        parsed_content = output_text
        prompt_tokens = 0
        completion_tokens = 0
        cache_read_tokens = 0
        thinking_tokens = 0
        recorded_model = (resolved_model if not self._is_agentapi else active_model) or DEFAULT_ANTIGRAVITY_MODEL

        # Parse JSON output from headless CLI
        if output_text.startswith("{") and output_text.endswith("}"):
            try:
                data = json.loads(output_text)
                if isinstance(data, dict):
                    status = data.get("status")
                    if status and status != "SUCCESS":
                        err_detail = data.get("error") or f"CLI returned status '{status}'"
                        raise ProviderError(f"Antigravity CLI execution failed: {err_detail}", status_code=500)
                    parsed_content = data.get("response", "")
                    if "duration_seconds" in data and isinstance(data["duration_seconds"], (int, float)):
                        duration_ms = round(float(data["duration_seconds"]) * 1000, 2)
                    usage = data.get("usage", {})
                    if isinstance(usage, dict):
                        prompt_tokens = int(usage.get("input_tokens") or 0)
                        completion_tokens = int(usage.get("output_tokens") or 0)
                        cache_read_tokens = int(usage.get("cache_read_tokens") or 0)
                        thinking_tokens = int(usage.get("thinking_tokens") or 0)
            except json.JSONDecodeError:
                pass

        if prompt_tokens == 0:
            prompt_tokens = max(1, len(full_prompt) // 4)
        if completion_tokens == 0:
            completion_tokens = max(1, len(parsed_content) // 4)

        # Base wrapper in isolated tempdir is ~9200 tokens
        net_prompt = max(0, prompt_tokens - 9200)

        return CompletionResponse(
            content=parsed_content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=duration_ms,
            model=recorded_model,
            cache_read_tokens=cache_read_tokens,
            thinking_tokens=thinking_tokens,
            net_prompt_tokens=net_prompt,
            raw_response={
                "provider": "antigravity",
                "cli_path": str(cli),
                "effort": effort_val,
                "exit_code": proc.returncode,
                "cwd": effective_cwd,
                "cache_read_tokens": cache_read_tokens,
                "thinking_tokens": thinking_tokens,
                "net_prompt_tokens": net_prompt,
            },
        )

    def stream_chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        effort: Optional[str] = None,
        model: Optional[str] = None,
        cwd: Optional[str | Path] = None,
        **kwargs: Any,
    ) -> Iterator[str]:
        """Stream incremental text deltas from Antigravity CLI in real-time using stream-json."""
        cli = self.get_cli_path()
        if self._is_agentapi:
            resp = self.chat_completion(messages, temperature, max_tokens, effort, model, cwd=cwd, **kwargs)
            yield resp.content
            return

        prompt_parts: List[str] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                prompt_parts.append(f"System Instructions:\n{content}\n")
            elif role == "user":
                prompt_parts.append(f"User Request:\n{content}\n")
            elif role == "assistant":
                prompt_parts.append(f"Previous Assistant Response:\n{content}\n")

        full_prompt = "\n".join(prompt_parts).strip()
        if not full_prompt and messages:
            full_prompt = messages[-1].get("content", "")
        full_prompt = scrub_secrets(full_prompt)

        effort_val = (effort or "medium").strip().lower()
        if effort_val not in ("low", "medium", "high"):
            effort_val = "medium"

        active_model = model or kwargs.get("model") or self.config.model

        effective_cwd = (
            cwd
            or kwargs.get("workspace_root")
            or getattr(self.config, "workspace_root", None)
        )
        if effective_cwd:
            effective_cwd = str(Path(effective_cwd).resolve())
        else:
            import tempfile
            effective_cwd = tempfile.gettempdir()

        cmd_args, resolved_model = self._build_command(
            full_prompt=full_prompt,
            effort_val=effort_val,
            active_model=active_model,
            output_format="stream-json",
        )

        try:
            proc = subprocess.Popen(
                cmd_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                cwd=effective_cwd,
                stdin=subprocess.DEVNULL,
                bufsize=1,
            )
        except Exception as ex:
            raise ProviderError(f"Failed to launch Antigravity streaming process: {ex}") from None

        try:
            if proc.stdout:
                for line in proc.stdout:
                    line_str = line.strip()
                    if line_str.startswith("{") and line_str.endswith("}"):
                        try:
                            event = json.loads(line_str)
                            if event.get("event") == "step_update":
                                step = event.get("step_update", {})
                                delta = step.get("text_delta")
                                if delta:
                                    yield delta
                        except json.JSONDecodeError:
                            pass
            proc.wait(timeout=self._timeout_seconds)
        except Exception as ex:
            proc.kill()
            raise ProviderError(f"Antigravity streaming error: {ex}") from None
