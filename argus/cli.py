"""Argus Command Line Interface: Developer UX with Rich Terminal Formatting."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

import click
import rich.box
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from argus import __version__
from argus.config import ArgusConfig
from argus.providers.adapter import OpenAICompatibleAdapter
from argus.providers.models import ProviderConfig, PROVIDER_PRESETS
from argus.providers.storage import ProviderStorage
from argus.runtime.candidate import AuthoritativePatchApplier, CandidateArtifactStore
from argus.runtime.controller import AttemptRecord, RecoveryController, RecoveryOutcome
from argus.runtime.events import JsonLinesEmitter

# Ensure UTF-8 output on Windows consoles to prevent cp1252 charmap encoding errors
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

console = Console(legacy_windows=False)


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="argus")
@click.option("--workspace", "-w", default=".", type=click.Path(exists=True), help="Active workspace path.")
@click.pass_context
def main(ctx: click.Context, workspace: str):
    """Argus: Bounded, test-gated Python code recovery and patch runtime."""
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        console.print("\n[dim]Interactive UI: run 'argus tui' to launch the full-screen terminal app.[/dim]")



# =============================================================================
# Diagnostics & System Doctor
# =============================================================================

@main.command()
def doctor():
    """Run environment diagnostics and check active provider connectivity."""
    console.print(f"[bold]argus doctor[/bold] (v{__version__})\n")

    # 1. Python Environment
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 11):
        console.print(f"  [green]✓[/green] Python          {py_ver}")
    else:
        console.print(f"  [red]✗[/red] Python          {py_ver} (< 3.11 required)")

    # 2. Git
    git_path = shutil.which("git")
    if git_path:
        console.print(f"  [green]✓[/green] Git             {git_path}")
    else:
        console.print("  [yellow]![/yellow] Git             not found in PATH")

    # 3. Pytest
    pytest_path = shutil.which("pytest")
    if pytest_path:
        console.print(f"  [green]✓[/green] Pytest          {pytest_path}")
    else:
        console.print("  [yellow]![/yellow] Pytest          not found in PATH")

    # 4. Antigravity Host Runtime
    from argus.providers.antigravity import find_antigravity_cli
    agy_cli = find_antigravity_cli()
    if agy_cli:
        console.print(f"  [green]✓[/green] Antigravity     {agy_cli}")
    else:
        console.print("  [dim]-[/dim] Antigravity     not detected in PATH")

    # 5. Local Ollama Endpoint Detection
    try:
        import httpx
        with httpx.Client(timeout=1.0) as client:
            ollama_res = client.get("http://localhost:11434/api/tags")
            if ollama_res.status_code == 200:
                console.print("  [green]✓[/green] Ollama          running (http://localhost:11434)")
            else:
                console.print("  [dim]-[/dim] Ollama          not responding (port 11434)")
    except Exception:
        console.print("  [dim]-[/dim] Ollama          not detected on localhost:11434")

    # 6. Active Provider Configuration
    storage = ProviderStorage()
    active_cfg = storage.get_active_provider()
    if active_cfg is None:
        console.print("  [yellow]![/yellow] Provider        none configured (run 'argus provider set')")
    else:
        console.print(f"  [green]✓[/green] Provider        {active_cfg.name} ({active_cfg.model})")
        if active_cfg.api_key:
            console.print(f"  [green]✓[/green] Credentials     configured ({active_cfg.masked_key})")
        else:
            preset = PROVIDER_PRESETS.get(active_cfg.name.lower())
            if preset and preset.requires_key:
                console.print(f"  [red]✗[/red] Credentials     missing key for {active_cfg.name}")
            else:
                console.print("  [green]✓[/green] Credentials     host/local (key not required)")

        with console.status(f"[dim]Pinging {active_cfg.name}...[/dim]"):
            if active_cfg.name.lower() == "antigravity":
                from argus.providers.antigravity import AntigravityHostAdapter
                adapter = AntigravityHostAdapter(active_cfg)
            else:
                adapter = OpenAICompatibleAdapter(active_cfg)
            test_res = adapter.test_connection()
            if test_res.success:
                console.print(f"  [green]✓[/green] Connectivity    {test_res.latency_ms:.1f}ms ({test_res.model})")
            else:
                console.print(f"  [red]✗[/red] Connectivity    offline: {test_res.message[:60]}")

    console.print()


# =============================================================================
# Provider Management
# =============================================================================

@main.group("provider")
def provider_group():
    """Configure, select, and test LLM inference providers."""
    pass


@provider_group.command("list")
def provider_list():
    """List configured providers and built-in presets."""
    storage = ProviderStorage()
    providers = storage.list_providers()
    active_cfg = storage.get_active_provider()

    table = Table(title="Configured Providers", show_header=True, header_style="bold", box=rich.box.SIMPLE)
    table.add_column("Active", justify="center")
    table.add_column("Name")
    table.add_column("Model")
    table.add_column("Base URL", style="dim")
    table.add_column("Key Status")

    for p in providers:
        is_active = "*" if (active_cfg and p.name == active_cfg.name) else ""
        table.add_row(is_active, p.name, p.model, p.base_url, p.masked_key)

    console.print()
    console.print(table)

    presets_table = Table(title="Available Presets", show_header=True, header_style="bold", box=rich.box.SIMPLE)
    presets_table.add_column("Preset", width=14)
    presets_table.add_column("Display Name", width=28)
    presets_table.add_column("Default Model", style="dim", width=26)
    presets_table.add_column("Key Required", width=14)

    for k, pr in PROVIDER_PRESETS.items():
        req_str = "Yes" if pr.requires_key else "No (Local)"
        presets_table.add_row(k, pr.display_name, pr.default_model, req_str)

    console.print()
    console.print(presets_table)
    console.print("\n[dim]To add/update: argus provider set --preset <preset> --api-key <key>[/dim]")
    console.print("[dim]To switch:     argus provider select <name>[/dim]\n")


@provider_group.command("set")
@click.option("--name", "-n", default=None, help="Provider name or preset identifier.")
@click.option("--preset", "-p", default=None, help="Preset key (openai, openrouter, deepseek, ollama, lmstudio, custom).")
@click.option("--base-url", "-u", default=None, help="API Base URL (e.g. https://api.openai.com/v1).")
@click.option("--model", "-m", default=None, help="Model identifier (e.g. gpt-4o-mini, llama-3.3-70b-versatile).")
@click.option("--api-key", "-k", default=None, help="API Key (optional; prompted interactively with hidden input by default).")
@click.option("--timeout", "-t", default=60.0, type=float, help="Request timeout in seconds.")
@click.option("--active", is_flag=True, default=True, help="Set as currently active provider.")
def provider_set(
    name: Optional[str],
    preset: Optional[str],
    base_url: Optional[str],
    model: Optional[str],
    api_key: Optional[str],
    timeout: float,
    active: bool,
):
    """Add or update an LLM provider configuration."""
    storage = ProviderStorage()
    preset_key = (preset or name or "custom").lower()

    if preset_key == "groq" or (name and name.lower() == "groq"):
        console.print("[red]✗ Groq is strictly prohibited as an inference provider in Argus.[/red]")
        console.print("[dim]Please choose an authorized provider (e.g. ollama, openai, deepseek, antigravity).[/dim]")
        sys.exit(1)

    if preset_key in PROVIDER_PRESETS:
        p_def = PROVIDER_PRESETS[preset_key]
        resolved_name = name or p_def.key
        resolved_url = base_url or p_def.default_base_url
        resolved_model = model or p_def.default_model
        headers = dict(p_def.extra_headers)
        requires_key = p_def.requires_key
    else:
        resolved_name = name or "custom"
        resolved_url = base_url or "http://localhost:8000/v1"
        resolved_model = model or "custom-model"
        headers = {}
        requires_key = True

    # Check for existing configured credentials
    existing = storage.get_provider(resolved_name)
    existing_key = existing.api_key if existing else None

    # Collect API key via hidden interactive prompt if not supplied via option
    if api_key is None and requires_key:
        prompt_label = f"Enter API key for '{resolved_name}'"
        if existing_key:
            prompt_label += f" [leave blank to keep existing: {existing.masked_key}]"
        try:
            entered = click.prompt(prompt_label, hide_input=True, default="", show_default=False)
            if entered.strip():
                api_key = entered.strip()
            elif existing_key:
                api_key = existing_key
            else:
                api_key = None
        except Exception:
            api_key = existing_key

    cfg = ProviderConfig(
        name=resolved_name,
        base_url=resolved_url,
        model=resolved_model,
        api_key=api_key,
        timeout_seconds=timeout,
        extra_headers=headers,
    )

    storage.save_provider(cfg)
    if active:
        storage.set_active_provider(cfg.name)

    active_str = " (active)" if active else ""
    console.print(f"[green]✓[/green] Provider '{cfg.name}' Saved{active_str}")
    console.print(f"  base_url: {cfg.base_url}")
    console.print(f"  model:    {cfg.model}")
    console.print(f"  api_key:  {cfg.masked_key}\n")


@provider_group.command("select")
@click.argument("name")
def provider_select(name: str):
    """Select the active provider by name."""
    storage = ProviderStorage()
    if storage.set_active_provider(name):
        cfg = storage.get_active_provider()
        console.print(f"[green]✓[/green] Active provider set to '{cfg.name}' (model: {cfg.model})")
    else:
        console.print(f"[red]✗[/red] Provider '{name}' not found. Run 'argus provider list' to see available providers.")


@provider_group.command("test")
@click.argument("name", required=False, default=None)
def provider_test(name: Optional[str]):
    """Test connection and model response for the active or named provider."""
    storage = ProviderStorage()
    cfg = storage.get_provider(name) if name else storage.get_active_provider()
    if not cfg:
        target = f"'{name}'" if name else "Active provider"
        console.print(f"[red]✗[/red] {target} is not configured. Run 'argus provider set' first.")
        return

    with console.status(f"[dim]Pinging {cfg.name}...[/dim]"):
        adapter = OpenAICompatibleAdapter(cfg)
        res = adapter.test_connection()

    if res.success:
        console.print(f"[green]✓[/green] Connected Successfully ({res.latency_ms:.1f}ms)")
        console.print(f"  model:    {res.model}")
        if res.response_sample:
            console.print(f"  sample:   [dim]{res.response_sample[:80]}[/dim]")
    else:
        console.print(f"[red]✗[/red] Connection failed: {res.message}")
        if res.status_code:
            console.print(f"  status:   {res.status_code}")


@provider_group.command("delete")
@click.argument("name")
def provider_delete(name: str):
    """Delete a configured provider and its stored credentials."""
    storage = ProviderStorage()
    if storage.delete_provider(name):
        console.print(f"[green]✓[/green] Provider '{name}' removed.")
    else:
        console.print(f"[yellow]![/yellow] Provider '{name}' not found.")


# =============================================================================
# Code Recovery Execution
# =============================================================================

@main.command()
@click.option("--file", "-f", "--target", "target_file", default=None, type=click.Path(exists=True), help="Target source file to fix (optional; auto-localized from test if omitted).")
@click.option("--test", "-t", "test_command", required=True, type=str, help="Test command to run (e.g. 'pytest tests/test_calc.py').")
@click.option("--test-file", type=click.Path(exists=True), default=None, help="Optional test file to copy to isolated workspace.")
@click.option("--provider", "-p", default=None, help="Override provider name for this run.")
@click.option("--model", "-m", default=None, help="Override model ID for this run.")
@click.option("--effort", "-e", type=click.Choice(["low", "medium", "high"], case_sensitive=False), default=None, help="Explicit reasoning effort.")
@click.option("--no-adaptive", is_flag=True, default=False, help="Disable adaptive difficulty routing.")
@click.option("--attempts", "-a", default=3, type=int, help="Maximum recovery attempts (max 3 turns).")
@click.option("--budget", "-b", default=0.05, type=float, help="Maximum budget limit in USD.")
@click.option("--workspace", "-w", default=None, help="Active workspace path.")
@click.option("--enable-tier4", "--allow-tier4", is_flag=True, default=False, help="Enable experimental Tier 4 anchor-based patch matching (disabled by default).")
@click.option("--apply", is_flag=True, default=False, help="Apply a verified patch only when --yes-reviewed is also supplied.")
@click.option(
    "--yes-reviewed",
    is_flag=True,
    default=False,
    help="Explicit non-interactive authorization to apply a verified patch after review. Recorded as automation, not human approval.",
)
@click.option("--json-events", is_flag=True, default=False, help="Emit versioned JSON Lines IPC events to stdout.")
def run(
    target_file: Optional[str],
    test_command: str,
    test_file: Optional[str],
    provider: Optional[str],
    model: Optional[str],
    effort: Optional[str],
    no_adaptive: bool,
    attempts: int,
    budget: float,
    workspace: Optional[str],
    enable_tier4: bool,
    apply: bool,
    yes_reviewed: bool,
    json_events: bool,
):
    """Run the Argus non-destructive code recovery loop on a failing file."""
    ws_root = Path(workspace).resolve() if workspace else Path.cwd().resolve()
    storage = ProviderStorage()
    p_name = (provider or "").strip().lower()

    if p_name == "auto":
        # 'auto' is an explicit request for deterministic discovery: prefer the
        # stored active provider, then the host runtime. Never infer from keys.
        p_cfg = storage.get_active_provider()
        if not p_cfg:
            from argus.providers.antigravity import find_antigravity_cli
            if find_antigravity_cli() is not None:
                p_cfg = ProviderConfig.from_preset("antigravity", model=model)
        if not p_cfg:
            console.print(
                "[bold red]No discoverable provider for '--provider auto'.[/bold red] "
                "Configure one or select 'antigravity' explicitly."
            )
            sys.exit(1)
        console.print(f"[dim]Auto-selected provider: {p_cfg.name}[/dim]")
    elif p_name == "antigravity":
        p_cfg = ProviderConfig.from_preset("antigravity", model=model)
    elif provider:
        p_cfg = storage.get_provider(provider)
        if not p_cfg:
            console.print(f"[bold red]Provider '{provider}' not configured.[/bold red] Run 'argus provider set' first.")
            sys.exit(1)
    else:
        p_cfg = storage.get_active_provider()
        if not p_cfg:
            console.print(
                "[bold red]No LLM provider configured.[/bold red] Run 'argus provider set' first, "
                "specify '--provider <name>', or explicitly request '--provider auto'."
            )
            sys.exit(1)

    if model:
        p_cfg.model = model

    bounded_attempts = max(1, min(attempts, 3))

    config = ArgusConfig(
        provider=p_cfg.name,
        model=p_cfg.model,
        max_attempts=bounded_attempts,
        budget_limit_usd=budget,
        provider_config=p_cfg,
        effort=effort,
        adaptive_routing=not no_adaptive,
        enable_tier4=enable_tier4,
    )
    controller = RecoveryController(config=config)

    # If --json-events is requested, output purely machine-readable JSON Lines to stdout
    if json_events:
        emitter = JsonLinesEmitter(sys.stdout)
        outcome: RecoveryOutcome = controller.run(
            target_file=target_file,
            test_command=test_command,
            test_file=test_file,
            workspace_root=ws_root,
            event_emitter=emitter,
        )
        if apply and outcome.status == "SUCCESS" and outcome.run_id:
            if not yes_reviewed:
                sys.stderr.write(
                    "Refusing to apply: --apply requires explicit --yes-reviewed authorization.\n"
                    f"Review the diff, then run 'argus approve {outcome.run_id}' followed by 'argus apply'.\n"
                )
                sys.exit(1)
            CandidateArtifactStore.set_approval(
                outcome.run_id,
                approved=True,
                workspace_root=ws_root,
                approved_by="automation:--yes-reviewed",
                approval_source="cli-run-json-apply",
            )
            apply_res = AuthoritativePatchApplier.apply(
                run_id=outcome.run_id,
                workspace_root=ws_root,
                expected_target=outcome.target_file or target_file,
                require_approval=True,
            )
            if not apply_res.success:
                sys.exit(1)
        if outcome.status not in ("SUCCESS", "ALREADY_PASSING"):
            sys.exit(1)
        return

    effort_label = effort.upper() if effort else ("disabled" if no_adaptive else "adaptive")
    display_file = target_file or "(auto-localizing from test)"
    console.print(f"[bold]argus run[/bold]: {test_command}")
    if target_file:
        console.print(f"  target:   {target_file}")
    else:
        console.print(f"  target:   {display_file}")
    console.print(f"  provider: {p_cfg.name} ({p_cfg.model}, effort: {effort_label})\n")

    def on_attempt_start(attempt: int, max_att: int):
        pass

    def on_attempt_end(record: AttemptRecord):
        tier_label = f"tier {record.tier_used}" if record.tier_used else "turn"
        tier_model = getattr(record, "model_used", None) or p_cfg.model
        if record.test_passed:
            status_text = f"[green]ok[/green] ({record.duration_ms:.0f}ms, ${record.cost_usd:.4f})"
        else:
            status_text = f"[red]failed[/red] ({record.duration_ms:.0f}ms)"

        console.print(f"  [{record.attempt}/{bounded_attempts}] {tier_label} ({tier_model})... {status_text}")
        if not record.test_passed and record.error_summary:
            first_err_line = record.error_summary.strip().splitlines()[0][:85]
            console.print(f"      [dim]{first_err_line}[/dim]")

    with console.status("[dim]Running recovery loop in isolated workspace...[/dim]"):
        outcome: RecoveryOutcome = controller.run(
            target_file=target_file,
            test_command=test_command,
            test_file=test_file,
            workspace_root=ws_root,
            on_attempt_start=on_attempt_start,
            on_attempt_end=on_attempt_end,
        )

    console.print()
    if outcome.status == "SUCCESS":
        console.print(f"[bold green]✓ Verified passing[/bold green] ({outcome.total_attempts} attempts, {outcome.total_duration_ms:.0f}ms, ${outcome.total_cost_usd:.4f})")
        console.print(f"  Candidate: [dim]{outcome.run_id}[/dim]")
        if outcome.target_file:
            console.print(f"  Target:    {outcome.target_file}")
        console.print()

        if outcome.unified_diff:
            syntax = Syntax(outcome.unified_diff, "diff", theme="monokai", line_numbers=False)
            console.print(syntax)
            console.print()

            dot_argus = ws_root / ".argus"
            dot_argus.mkdir(exist_ok=True)
            (dot_argus / "last_run.diff").write_text(outcome.unified_diff, encoding="utf-8")

            if apply and not yes_reviewed:
                console.print(
                    "[bold red]Refusing to apply without explicit authorization.[/bold red]"
                )
                console.print(
                    f"[dim]Review the diff, then run 'argus approve {outcome.run_id}' and "
                    "'argus apply', or pass --yes-reviewed for reviewed non-interactive use.[/dim]\n"
                )
            elif apply:
                console.print(f"[dim]Applying patch from {outcome.run_id}...[/dim]")
                CandidateArtifactStore.set_approval(
                    outcome.run_id,
                    approved=True,
                    workspace_root=ws_root,
                    approved_by="automation:--yes-reviewed",
                    approval_source="cli-run-apply",
                )
                apply_result = AuthoritativePatchApplier.apply(
                    run_id=outcome.run_id,
                    workspace_root=ws_root,
                    expected_target=outcome.target_file or target_file,
                    require_approval=True,
                )
                if apply_result.success:
                    console.print(f"[green]✓ {apply_result.message}[/green]")
                else:
                    console.print(f"[red]✗ Patch rejected: {apply_result.message}[/red]")
            else:
                console.print(f"[dim]To approve: argus approve {outcome.run_id}[/dim]")
                console.print(f"[dim]To apply:   argus apply[/dim]\n")

    elif outcome.status == "ALREADY_PASSING":
        console.print(f"[green]✓ Test already passes[/green] without modifications. ({outcome.total_duration_ms:.0f}ms)\n")
    else:
        console.print(f"[bold red]✗ Recovery failed[/bold red] after {outcome.total_attempts} attempts (${outcome.total_cost_usd:.4f})")
        console.print(f"  Status:  {outcome.status}")
        if outcome.message:
            console.print(f"  Message: {outcome.message}\n")
        elif outcome.diagnostics:
            first_err = outcome.diagnostics.strip().splitlines()[0][:100]
            console.print(f"  Details: {first_err}\n")
        sys.exit(1)



# =============================================================================
# Task Execution (Adaptive Difficulty Inference)
# =============================================================================

@main.command("exec")
@click.argument("task_prompt", required=False)
@click.option("--prompt", "-p", "flag_prompt", default=None, help="Task prompt or query.")
@click.option("--file", "-f", "target_file", default=None, type=click.Path(exists=True), help="Optional target source file for context.")
@click.option("--effort", "-e", type=click.Choice(["low", "medium", "high"], case_sensitive=False), default=None, help="Explicit reasoning effort override.")
@click.option("--no-adaptive", is_flag=True, default=False, help="Disable adaptive difficulty routing.")
@click.option("--provider", default=None, help="Provider name (defaults to 'antigravity').")
@click.option("--model", "-m", default=None, help="Override model ID.")
def exec_task(
    task_prompt: Optional[str],
    flag_prompt: Optional[str],
    target_file: Optional[str],
    effort: Optional[str],
    no_adaptive: bool,
    provider: Optional[str],
    model: Optional[str],
):
    """Execute a coding or analysis task through Antigravity's authenticated runtime with adaptive reasoning effort."""
    full_prompt = flag_prompt or task_prompt
    if not full_prompt:
        console.print("[bold red]Error:[/bold red] Please provide a task prompt as an argument or with -p/--prompt.")
        sys.exit(1)

    code_content = None
    if target_file:
        tf_path = Path(target_file)
        if tf_path.is_file():
            code_content = tf_path.read_text(encoding="utf-8", errors="replace")

    # 1. Difficulty Classification
    from argus.routing.difficulty import DifficultyClassifier
    classifier = DifficultyClassifier()
    decision = classifier.classify(
        prompt=full_prompt,
        code=code_content,
        files=[target_file] if target_file else [],
        explicit_override=effort,
        disable_adaptive=no_adaptive,
    )

    # 2. Resolve Provider (Antigravity by default or configured provider)
    storage = ProviderStorage()
    p_name = (provider or "").strip().lower()
    if p_name == "antigravity":
        p_cfg = ProviderConfig.from_preset("antigravity", model=model)
    elif provider:
        p_cfg = storage.get_provider(provider)
        if not p_cfg:
            console.print(f"[bold red]Unknown provider '{provider}'.[/bold red]")
            sys.exit(1)
        if model:
            p_cfg.model = model
    else:
        active = storage.get_active_provider()
        if active:
            p_cfg = active
            if model:
                p_cfg.model = model
        else:
            p_cfg = ProviderConfig.from_preset("antigravity", model=model)

    console.print(f"[bold]argus exec[/bold]: {p_cfg.name} ({p_cfg.model}, effort: {decision.effort})")
    if target_file:
        console.print(f"  context:  {target_file}")
    if decision.reasons:
        console.print(f"  routing:  {', '.join(decision.reasons)} (confidence: {decision.confidence:.2f})")
    console.print()

    # Build prompt messages
    messages = [
        {
            "role": "system",
            "content": (
                "You are Argus, an expert software engineering assistant. "
                "Provide direct, high-quality, technically precise solutions."
            ),
        }
    ]
    user_body = full_prompt
    if code_content and target_file:
        user_body = f"Context file: {target_file}\n```\n{code_content}\n```\n\nTask: {full_prompt}"
    messages.append({"role": "user", "content": user_body})

    # Execute via adapter
    if p_cfg.name.lower() == "antigravity":
        from argus.providers.antigravity import AntigravityHostAdapter
        adapter = AntigravityHostAdapter(config=p_cfg)
    else:
        from argus.providers.adapter import OpenAICompatibleAdapter
        adapter = OpenAICompatibleAdapter(config=p_cfg)

    with console.status(f"[dim]Executing task through {p_cfg.name}...[/dim]"):
        try:
            resp = adapter.chat_completion(
                messages=messages,
                effort=decision.effort,
            )
        except Exception as ex:
            console.print(f"\n[bold red]Execution failed:[/bold red] {ex}")
            sys.exit(1)

    console.print(resp.content)
    console.print()
    console.print(
        f"[dim]{resp.model} · {resp.duration_ms:.0f}ms · "
        f"~{resp.prompt_tokens} in / ~{resp.completion_tokens} out[/dim]\n"
    )


# =============================================================================
# Candidate Review, Approval, and Application
# =============================================================================

@main.command("candidates")
@click.option("--limit", "-n", default=10, type=int, help="Maximum number of candidates to list (default 10).")
@click.option("--all", "-a", "show_all", is_flag=True, default=False, help="List all candidate runs.")
@click.option("--workspace", "-w", default=".", type=click.Path(exists=True), help="Active workspace path.")
def candidates_cmd(limit: int, show_all: bool, workspace: str):
    """List recent recovery candidate patches with verification and approval status."""
    ws_root = Path(workspace).resolve()
    candidates = CandidateArtifactStore.list_candidates(workspace_root=ws_root)

    if not candidates:
        console.print("[dim]No candidate runs found in this workspace. Execute 'argus run' first.[/dim]")
        return

    displayed = candidates if show_all or limit <= 0 else candidates[:limit]

    table = Table(show_header=True, header_style="bold", box=rich.box.SIMPLE)
    table.add_column("Run ID", no_wrap=True)
    table.add_column("Target")
    table.add_column("Status", justify="center")
    table.add_column("Approval", justify="center")
    table.add_column("Applied", justify="center")
    table.add_column("Cost", justify="right")
    table.add_column("Created", style="dim")

    for c in displayed:
        ver_str = "[green]PASS[/green]" if c.verified else "[red]FAIL[/red]"
        app_str = "[green]APPROVED[/green]" if c.approved else "[yellow]PENDING[/yellow]"
        apl_str = "[green]YES[/green]" if c.applied else "[dim]NO[/dim]"
        created_short = c.created_at[:16].replace("T", " ") if c.created_at else ""

        display_target = c.target_file
        try:
            p = Path(c.target_file)
            if p.is_relative_to(ws_root):
                display_target = str(p.relative_to(ws_root)).replace("\\", "/")
            elif "Temp" in str(p) or "tmp" in str(p):
                display_target = p.name
        except Exception:
            pass

        table.add_row(
            c.run_id,
            display_target,
            ver_str,
            app_str,
            apl_str,
            f"${c.cost_usd:.4f}",
            created_short,
        )

    console.print()
    console.print(table)
    if len(candidates) > len(displayed):
        console.print(f"[dim]Showing {len(displayed)} of {len(candidates)} runs. Pass '--all' to view full history.[/dim]")
    console.print("\n[dim]To inspect: argus inspect <run_id>[/dim]")
    console.print("[dim]To approve: argus approve <run_id>[/dim]")
    console.print("[dim]To apply:   argus apply --run-id <run_id>[/dim]\n")


@main.command("inspect")
@click.argument("run_id")
@click.option("--workspace", "-w", default=".", type=click.Path(exists=True), help="Active workspace path.")
def inspect_cmd(run_id: str, workspace: str):
    """Inspect candidate patch details and view its unified diff."""
    ws_root = Path(workspace).resolve()
    try:
        metadata, _ = CandidateArtifactStore.load_candidate(run_id, workspace_root=ws_root)
        diff_text = CandidateArtifactStore.get_candidate_diff(run_id, workspace_root=ws_root)

        ver_str = "[green]verified (passed)[/green]" if metadata.verified else "[red]unverified (failed)[/red]"
        app_str = "[green]approved[/green]" if metadata.approved else "[yellow]pending[/yellow]"

        console.print(f"\n[bold]Candidate:[/bold] {metadata.run_id}")
        console.print(f"  Target File:  {metadata.target_file}")
        console.print(f"  Test Command: {metadata.test_command}")
        console.print(f"  Status:       {ver_str}")
        console.print(f"  Approval:     {app_str}")
        console.print(f"  Applied:      {metadata.applied}")
        console.print(f"  Attempts:     {metadata.attempts} (${metadata.cost_usd:.4f})")
        console.print(f"  Created:      {metadata.created_at}\n")

        if diff_text:
            console.print("[bold]Candidate Diff:[/bold]")
            syntax = Syntax(diff_text, "diff", theme="monokai", line_numbers=False)
            console.print(syntax)
        else:
            console.print("[dim]No diff recorded for this candidate.[/dim]")
    except Exception as ex:
        console.print(f"[red]Failed to inspect candidate '{run_id}':[/red] {ex}")


@main.command()
@click.argument("run_id", required=False, default=None)
@click.option("--workspace", "-w", default=".", type=click.Path(exists=True), help="Active workspace path.")
def diff(run_id: Optional[str], workspace: str):
    """Display the unified diff for a candidate run (defaults to last run)."""
    ws_root = Path(workspace).resolve()
    target_id = run_id or CandidateArtifactStore.get_last_run_id(workspace_root=ws_root)
    if not target_id:
        diff_file = Path(".argus/last_run.diff")
        if diff_file.is_file():
            diff_content = diff_file.read_text(encoding="utf-8")
            syntax = Syntax(diff_content, "diff", theme="monokai", line_numbers=False)
            console.print(syntax)
            return
        console.print("[yellow]No recent candidate runs found.[/yellow]")
        return

    diff_text = CandidateArtifactStore.get_candidate_diff(target_id, workspace_root=ws_root)
    if diff_text:
        syntax = Syntax(diff_text, "diff", theme="monokai", line_numbers=False)
        console.print(syntax)
    else:
        console.print(f"[yellow]No diff recorded for run '{target_id}'.[/yellow]")


@main.command("approve")
@click.argument("run_id")
@click.option("--workspace", "-w", default=".", type=click.Path(exists=True), help="Active workspace path.")
@click.option("--yes", "-y", is_flag=True, default=False, help="Trusted-automation bypass: skip interactive confirmation (for non-interactive CI/scripts only; not a human approval boundary).")
@click.option("--force", is_flag=True, default=False, help="Allow approving an unverified candidate.")
def approve_cmd(run_id: str, workspace: str, yes: bool, force: bool):
    """Grant approval for a candidate patch after reviewing its diff.
    
    Security note:
    The --yes flag is a trusted-automation bypass intended for scripted/CI environments.
    It does not constitute a human-approval boundary.
    """
    ws_root = Path(workspace).resolve()

    # 1. Load candidate metadata
    try:
        metadata, _ = CandidateArtifactStore.load_candidate(run_id, workspace_root=ws_root)
    except Exception as ex:
        console.print(f"[red]Candidate '{run_id}' not found:[/red] {ex}")
        sys.exit(1)

    # 2. Check verification status
    if not metadata.verified and not force:
        console.print(f"[red][REFUSED] Candidate {run_id} failed verification (status: {metadata.status})[/red]")
        console.print("[dim]Argus refuses to approve unverified candidate patches by default. Pass '--force' to override.[/dim]")
        sys.exit(1)

    # 3. Present formatted review summary
    console.print(f"\n[bold]Candidate Review:[/bold] {run_id}")
    console.print(f"  Target File: {metadata.target_file}")
    ver_status = "[green]VERIFIED PASSING[/green]" if metadata.verified else "[red]UNVERIFIED / FAILING[/red]"
    console.print(f"  Status:      {ver_status}")
    console.print(f"  Attempts:    {metadata.attempts} (${metadata.cost_usd:.4f})")
    console.print(f"  Created:     {metadata.created_at}")

    # 4. Display unified diff for review
    diff_text = CandidateArtifactStore.get_candidate_diff(run_id, workspace_root=ws_root)
    if diff_text:
        console.print("\n[bold]Proposed Patch Diff:[/bold]")
        syntax = Syntax(diff_text, "diff", theme="monokai", line_numbers=False)
        console.print(syntax)
    else:
        console.print("\n[yellow]Notice: No unified diff recorded for this candidate.[/yellow]")

    # 5. Interactive confirmation flow
    if not yes:
        if sys.stdin.isatty():
            confirmed = click.confirm("\nDo you want to approve this patch candidate?", default=False)
            if not confirmed:
                console.print("[yellow]Approval cancelled by user.[/yellow]")
                return
        else:
            try:
                confirmed = click.confirm("\nDo you want to approve this patch candidate?", default=False)
                if not confirmed:
                    console.print("[yellow]Approval cancelled by user.[/yellow]")
                    return
            except Exception:
                console.print("[yellow]Non-interactive environment detected. Pass '-y' / '--yes' to confirm approval.[/yellow]")
                sys.exit(1)

    # 6. Persist approval state
    try:
        updated = CandidateArtifactStore.set_approval(
            run_id,
            approved=True,
            workspace_root=ws_root,
            approved_by="automation:--yes" if yes else "interactive:cli",
            approval_source="cli-approve-yes" if yes else "cli-approve-interactive",
        )
        console.print(f"\n[green]✓ [APPROVED] Candidate {run_id}[/green]")
        console.print(f"  Target File: {updated.target_file}")
        console.print(f"  Approved At: {updated.approved_at}")
        console.print(f"  To apply:    argus apply --run-id {run_id}\n")
    except Exception as ex:
        console.print(f"[red]Approval failed:[/red] {ex}")
        sys.exit(1)


@main.command("reject")
@click.argument("run_id")
@click.option("--workspace", "-w", default=".", type=click.Path(exists=True), help="Active workspace path.")
def reject_cmd(run_id: str, workspace: str):
    """Reject a candidate patch, revoking approval."""
    ws_root = Path(workspace).resolve()
    try:
        CandidateArtifactStore.set_approval(run_id, approved=False, workspace_root=ws_root)
        console.print(f"[yellow][REJECTED] Candidate {run_id} approval revoked.[/yellow]")
    except Exception as ex:
        console.print(f"[red]Rejection failed:[/red] {ex}")
        sys.exit(1)


@main.command("apply")
@click.option("--run-id", "-r", default=None, help="Specific recovery run ID to apply. If omitted, uses last run.")
@click.option("--workspace", "-w", default=".", type=click.Path(exists=True), help="Active workspace path.")
@click.option("--file", "-f", "target_file", default=None, help="Optional assertion that candidate targets this file.")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Bypass the approval gate only. Candidate hash, baseline hash, containment and AST checks still apply.",
)
def apply_cmd(run_id: Optional[str], workspace: str, target_file: Optional[str], force: bool):
    """Authoritatively apply a verified patch candidate to the source file."""
    ws_root = Path(workspace).resolve()
    resolved_run_id = run_id or CandidateArtifactStore.get_last_run_id(ws_root)
    if not resolved_run_id:
        console.print("[red]No candidate run found. Specify --run-id or execute 'argus run' first.[/red]")
        sys.exit(1)

    # When run directly by the user via CLI, require_approval is enforced unless --force is passed
    # or the user has already approved it.
    result = AuthoritativePatchApplier.apply(
        run_id=resolved_run_id,
        workspace_root=ws_root,
        expected_target=target_file,
        require_approval=not force,
    )
    if result.success:
        console.print(f"[green]✓ [APPLIED][/green] {result.message}")
    else:
        console.print(f"[red]✗ [REJECTED: {result.error_code}][/red] {result.message}")
        if result.error_code == "CANDIDATE_NOT_APPROVED":
            console.print(f"  [dim]Run 'argus approve {resolved_run_id}' or pass '--force' to apply directly.[/dim]")
        sys.exit(1)


# =============================================================================
# Model Context Protocol (MCP) Management
# =============================================================================

@main.group("mcp")
def mcp_group():
    """Model Context Protocol (MCP) server and host agent integrations."""
    pass


@mcp_group.command("run")
def mcp_run():
    """Start the Argus MCP server over stdio for host agents."""
    from argus.mcp.server import run_mcp_server
    run_mcp_server()


@mcp_group.command("install")
@click.argument("target", required=False, default="all", type=click.Choice(["antigravity", "opencode", "gemini", "all"]))
@click.option("--global/--workspace", "global_mode", default=True, help="Install globally in user profile or locally in current workspace.")
@click.option("--workspace-path", default=".", type=click.Path(exists=True), help="Target workspace path if using --workspace.")
def mcp_install(target: str, global_mode: bool, workspace_path: str):
    """Install Argus MCP server configuration into host agents (Antigravity, OpenCode, Gemini CLI)."""
    from argus.mcp.installer import (
        install_antigravity,
        install_gemini,
        install_opencode,
    )
    ws = Path(workspace_path).resolve()
    results = []

    if target in ("antigravity", "all"):
        cfg_path, bak = install_antigravity(global_mode=global_mode, workspace_root=ws)
        results.append(("Antigravity", cfg_path, bak))

    if target in ("opencode", "all"):
        cfg_path, bak = install_opencode(global_mode=global_mode, workspace_root=ws)
        results.append(("OpenCode", cfg_path, bak))

    if target in ("gemini", "all"):
        cfg_path, bak = install_gemini(global_mode=global_mode, workspace_root=ws)
        results.append(("Gemini CLI", cfg_path, bak))

    table = Table(title="MCP Integrations Installed", show_header=True, header_style="bold", box=rich.box.SIMPLE)
    table.add_column("Host Agent", width=16)
    table.add_column("Config File", width=42)
    table.add_column("Backup File", style="dim", width=36)

    for host, cfg_path, bak in results:
        table.add_row(host, str(cfg_path), str(bak) if bak else "(none)")

    console.print()
    console.print(table)
    console.print("\n[green]✓[/green] MCP server entries successfully configured.")
    console.print("[dim]Run 'argus mcp verify' to test the stdio initialization handshake.[/dim]\n")


@mcp_group.command("verify")
def mcp_verify():
    """Verify that the Argus MCP server starts, handshakes, and executes tools over stdio."""
    from argus.mcp.installer import verify_mcp_handshake

    with console.status("[dim]Probing Argus MCP stdio handshake...[/dim]"):
        res = verify_mcp_handshake()

    if res["success"]:
        console.print(f"[green]✓[/green] MCP Server Verified (server: {res['server_name']})")
        console.print(f"  provider:  {res.get('active_provider', 'none')}")
        console.print(f"  tools:     {', '.join(res['tools_found'])}")
        console.print(f"  status:    {res['message']}\n")
    else:
        console.print(f"[red]✗[/red] MCP Verification Failed ({res.get('stage')})")
        console.print(f"  error:     {res.get('error')}\n")
        sys.exit(1)


# =============================================================================
# Presets, Proxy Server, and Standalone App
# =============================================================================

@main.command("init")
@click.argument("client", required=False, default=None)
@click.option("--list", "-l", "list_all", is_flag=True, default=False, help="List all available client presets.")
@click.option("--print", "-p", "print_only", is_flag=True, default=False, help="Print configuration to stdout instead of writing file.")
@click.option("--overwrite", "-o", is_flag=True, default=False, help="Overwrite existing configuration file.")
@click.option("--dir", "-d", "target_dir", default=".", type=click.Path(file_okay=False), help="Target directory to write configuration.")
def init_cmd(client: Optional[str], list_all: bool, print_only: bool, overwrite: bool, target_dir: str):
    """Generate integration presets for external coding tools (Aider, Claude, OpenCode, Continue, Cursor)."""
    from argus.adapters.presets import export_preset, get_preset, list_presets

    if list_all or not client:
        table = Table(title="Available Tool Integration Presets", show_header=True, header_style="bold", box=rich.box.SIMPLE)
        table.add_column("Client Key", width=14)
        table.add_column("Tool Name", width=22)
        table.add_column("Config File", width=24)
        table.add_column("Description", style="dim")

        for p in list_presets():
            table.add_row(p.key, p.display_name, p.filename or "(None)", p.description)

        console.print()
        console.print(table)
        console.print("\n[dim]Usage: run 'argus init <client>' to generate configuration files.[/dim]\n")
        return

    preset = get_preset(client)
    if not preset:
        console.print(f"[red]✗[/red] Unknown client preset '{client}'. Run 'argus init --list' to see options.")
        return

    if print_only:
        console.print(f"\n[bold]Configuration for {preset.display_name}:[/bold]")
        syntax = Syntax(preset.content, "yaml" if (preset.filename and preset.filename.endswith(".yml")) else "json", theme="monokai")
        console.print(syntax)
        console.print(f"\n[dim]Launcher command: {preset.cli_command}[/dim]\n")
        return

    try:
        exported_path = export_preset(preset.key, target_dir=target_dir, overwrite=overwrite)
        console.print(f"[green]✓[/green] Created preset for {preset.display_name}")
        console.print(f"  file:         {exported_path}")
        console.print(f"  instructions: {preset.instructions}")
        console.print(f"  run:          {preset.cli_command}\n")
    except FileExistsError as ex:
        console.print(f"[yellow]![/yellow] File exists: {ex}")
    except Exception as ex:
        console.print(f"[red]✗[/red] Failed to export preset: {ex}")


@main.command("setup")
@click.option("--workspace", "-w", default=".", type=click.Path(exists=True), help="Active workspace path.")
@click.option("--all", "-a", "connect_all", is_flag=True, default=False, help="Automatically connect all detected clients without prompting.")
def setup_cmd(workspace: str, connect_all: bool):
    """Auto-detect installed IDEs & agents and connect them with zero manual configuration."""
    from argus.adapters.discovery import connect_all_detected, discover_clients

    ws_root = Path(workspace).resolve()
    clients = discover_clients(ws_root)

    console.print("\n[bold]Argus Client Auto-Discovery & Zero-Friction Setup[/bold]\n")

    table = Table(title="Detected Local Tools", show_header=True, header_style="bold", box=rich.box.SIMPLE)
    table.add_column("Client / Tool", width=24)
    table.add_column("Status", width=20)
    table.add_column("Details", style="dim")

    detected_keys = []
    for c in clients:
        if c.configured:
            status = "[green]✓ Connected[/green]"
        elif c.installed:
            status = "[yellow]● Ready to Connect[/yellow]"
            detected_keys.append(c.key)
        else:
            status = "[dim]○ Not Found[/dim]"
        table.add_row(c.display_name, status, c.detection_reason)

    console.print(table)
    console.print()

    if not detected_keys:
        console.print("[green]All detected tools are already configured with Argus![/green]\n")
        return

    if not connect_all:
        if sys.stdin.isatty():
            confirmed = click.confirm(f"Connect all {len(detected_keys)} ready tools automatically?", default=True)
            if not confirmed:
                console.print("[yellow]Setup cancelled by user.[/yellow]")
                return
        else:
            connect_all = True

    results = connect_all_detected(ws_root)
    for k, (ok, msg) in results.items():
        if ok:
            console.print(f"  [green]✓[/green] {msg}")
        else:
            console.print(f"  [red]✗[/red] {msg}")

    console.print("\n[bold green]✓ Auto-setup complete![/bold green] Launch the gateway with [bold]argus gateway[/bold] or use tools directly.\n")


@main.command("gateway")
@click.option("--port", "-p", default=8000, type=int, help="Port to bind Argus Gateway proxy.")
@click.option("--host", "-h", default="127.0.0.1", type=str, help="Host address to bind.")
@click.option("--workspace", "-w", default=".", type=click.Path(exists=True), help="Active workspace path for streaming patch healing.")
@click.option("--config", "-c", default=None, type=click.Path(), help="Path to custom argus.yaml configuration.")
@click.option("--allow-remote", is_flag=True, default=False, help="Allow non-loopback network exposure. Requires ARGUS_GATEWAY_API_KEY. WARNING: Exposes gateway to external network.")
def gateway_cmd(port: int, host: str, workspace: str, config: Optional[str], allow_remote: bool):
    """Launch local OpenAI-compatible inference proxy with streaming patch healing."""
    serve(port=port, host=host, workspace=workspace, config=config, allow_remote=allow_remote)


@main.command()
@click.option("--port", "-p", default=8000, type=int, help="Port to bind Argus Gateway proxy.")
@click.option("--host", "-h", default="127.0.0.1", type=str, help="Host address to bind.")
@click.option("--workspace", "-w", default=".", type=click.Path(exists=True), help="Active workspace path for streaming patch healing.")
@click.option("--config", "-c", default=None, type=click.Path(), help="Path to custom argus.yaml configuration.")
@click.option("--allow-remote", is_flag=True, default=False, help="Allow non-loopback network exposure. Requires ARGUS_GATEWAY_API_KEY. WARNING: Exposes gateway to external network.")
def serve(port: int, host: str, workspace: str, config: Optional[str], allow_remote: bool):
    """Launch local OpenAI-compatible inference proxy with streaming patch healing."""
    try:
        import uvicorn
        import fastapi
    except ImportError:
        console.print("[red]The Argus Gateway proxy requires the optional 'gateway' dependencies.[/red]")
        console.print('Please install them with: [bold]pip install "argus-recovery\\[gateway]"[/bold]\n')
        sys.exit(1)

    from argus.gateway.config import GatewayConfig
    from argus.gateway.server import app, init_gateway

    # Enforce loopback binding by default
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if host not in loopback_hosts and not allow_remote:
        console.print(f"[bold red]Security Refusal:[/bold red] Binding to non-loopback host '{host}' exposes the Gateway to the network.")
        console.print("Argus binds to 127.0.0.1 by default. Provide [bold]--allow-remote[/bold] if network exposure is intended.\n")
        sys.exit(1)

    cfg = GatewayConfig.load(config)
    cfg.host = host
    cfg.port = port
    cfg.allow_remote = allow_remote

    if not cfg.get_gateway_key():
        console.print("[bold red]Security Refusal:[/bold red] ARGUS_GATEWAY_API_KEY environment variable is required to start the Gateway.")
        console.print("Unauthenticated gateway execution is strictly prohibited. Please set ARGUS_GATEWAY_API_KEY.\n")
        sys.exit(1)

    init_gateway(config=cfg, workspace=workspace)

    if allow_remote:
        console.print("[bold yellow]WARNING: Gateway is exposed to non-loopback network interfaces. Mandatory authentication is active.[/bold yellow]")

    console.print(f"[bold]argus gateway[/bold] listening on http://{host}:{port}/v1")
    console.print(f"  health:    http://{host}:{port}/health")
    console.print(f"  models:    http://{host}:{port}/v1/models")
    console.print(f"  workspace: {Path(workspace).resolve()}\n")

    uvicorn.run(app, host=host, port=port, log_level="info")


@main.command("app")
@click.option("--workspace", "-w", default=".", type=click.Path(exists=True), help="Target workspace path.")
@click.option("--install-shortcut", is_flag=True, default=False, help="Install Windows desktop shortcut launcher.")
def app_cmd(workspace: str, install_shortcut: bool):
    """Launch dedicated Argus standalone terminal application window."""
    try:
        import textual
    except ImportError:
        console.print("[red]The Argus Terminal UI requires the optional 'tui' dependencies.[/red]")
        console.print('Please install them with: [bold]pip install "argus-recovery\\[tui]"[/bold]\n')
        sys.exit(1)

    from argus.launcher import install_desktop_shortcut, launch_terminal_app

    if install_shortcut:
        sc_path = install_desktop_shortcut()
        console.print(f"[bold green]Successfully installed desktop shortcut:[/bold green] {sc_path}")
        return

    launch_terminal_app(dedicated=True, workspace=workspace)


@main.command("tui")
@click.option("--workspace", "-w", default=".", type=click.Path(exists=True), help="Target workspace path.")
@click.option(
    "--renderer",
    "-r",
    type=click.Choice(["auto", "braille", "halfblock", "sixel", "kitty", "text"], case_sensitive=False),
    default=None,
    help="Logo rendering strategy (halfblock, text, braille).",
)
def tui_cmd(workspace: str, renderer: Optional[str] = None):
    """Launch Argus interactive Terminal User Interface (TUI) in foreground."""
    try:
        import textual
    except ImportError:
        console.print("[red]The Argus Terminal UI requires the optional 'tui' dependencies.[/red]")
        console.print('Please install them with: [bold]pip install "argus-recovery\\[tui]"[/bold]\n')
        sys.exit(1)

    if renderer:
        os.environ["ARGUS_LOGO_RENDERER"] = renderer.lower()

    from argus.launcher import launch_terminal_app
    launch_terminal_app(dedicated=False, workspace=workspace)


@main.command("logo-preview")
@click.option(
    "--mode",
    "-m",
    type=click.Choice(["all", "box", "halfblock", "text", "braille"], case_sensitive=False),
    default="all",
    help="Rendering mode to preview (default: all).",
)
def logo_preview_cmd(mode: str):
    """Preview Argus logo rendering modes directly in your terminal without opening full TUI."""
    from argus.tui.renderers.box_drawing import BoxDrawingLogoRenderer
    from argus.tui.renderers.braille import BrailleLogoRenderer
    from argus.tui.renderers.halfblock import HalfBlockLogoRenderer
    from argus.tui.renderers.text_fallback import TextFallbackLogoRenderer

    selected = mode.lower()
    modes_to_show = ["box", "text", "halfblock", "braille"] if selected == "all" else [selected]

    for m in modes_to_show:
        if m == "box":
            console.print("\n[bold #818cf8]═══ Mode: Wide Architectural Box (7-Column Wide Proportions, No Dots) ═══[/bold #818cf8]\n")
            r = BoxDrawingLogoRenderer()
            res = r.render(target_width=64, max_width=64, max_height=18)
            console.print(res.text)
        elif m == "halfblock":
            console.print("\n[bold #818cf8]═══ Mode: Half-Block (Solid 24-bit TrueColor, No Dots) ═══[/bold #818cf8]\n")
            r = HalfBlockLogoRenderer()
            res = r.render(target_width=64, max_width=64, max_height=12)
            console.print(res.text)
        elif m == "text":
            console.print("\n[bold #818cf8]═══ Mode: Clean Text (Minimalist Standard, No Dots) ═══[/bold #818cf8]\n")
            r = TextFallbackLogoRenderer()
            res = r.render(target_width=64, max_width=64, max_height=12)
            console.print(res.text)
        elif m == "braille":
            console.print("\n[bold #818cf8]═══ Mode: Braille (2x4 Subpixel Dot Matrix) ═══[/bold #818cf8]\n")
            r = BrailleLogoRenderer()
            res = r.render(target_width=64, max_width=64, max_height=12)
            console.print(res.text)


# Alias 'recover' to 'run'
main.add_command(run, name="recover")
# Alias 'status' to 'candidates'
main.add_command(candidates_cmd, name="status")
# Alias 'apply-candidate' to 'apply'
main.add_command(apply_cmd, name="apply-candidate")


@main.command("update")
@click.option(
    "--check",
    is_flag=True,
    help="Check for updates without installing.",
)
@click.option(
    "--method",
    "-m",
    type=click.Choice(["uv", "pipx", "pip"], case_sensitive=False),
    default=None,
    help="Explicitly specify package manager (uv, pipx, or pip).",
)
@click.option(
    "--pre",
    is_flag=True,
    help="Include pre-release and beta versions.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Automatically confirm update without interactive prompt.",
)
def update_cmd(check: bool, method: Optional[str], pre: bool, yes: bool):
    """Check for and apply updates to argus-recovery from PyPI."""
    from argus.update import (
        InstallMethod,
        apply_update,
        check_for_updates,
        verify_update,
    )

    console.print(f"[bold #818cf8]Argus Update Manager[/bold #818cf8] (current: [cyan]v{__version__}[/cyan])\n")

    target_method = InstallMethod(method.lower()) if method else None

    with console.status("[dim]Querying PyPI for latest version...[/dim]"):
        result = check_for_updates(include_prereleases=pre)

    if result.error:
        console.print(f"[bold red]✗ Update check failed:[/bold red] {result.error}\n")
        sys.exit(1)

    table = Table(show_header=True, header_style="bold #818cf8", box=rich.box.SIMPLE)
    table.add_column("Property", style="#64748b")
    table.add_column("Value", style="#f8fafc")
    table.add_row("Current Version", f"v{result.current_version}")
    table.add_row("Latest Available", f"v{result.latest_version}")
    table.add_row("Channel", result.channel.value.capitalize())
    table.add_row("Install Method", (target_method.value if target_method else result.install_method.value).upper())
    console.print(table)
    console.print()

    if result.warning:
        console.print(f"[bold yellow]![/bold yellow] {result.warning}\n")

    if not result.has_update:
        console.print("[bold green]✓ Argus is already up to date.[/bold green]\n")
        return

    console.print(f"[bold #3fb950]Update available:[/bold #3fb950] v{result.current_version} -> [bold #3fb950]v{result.latest_version}[/bold #3fb950]")
    console.print(f"Release notes: [link={result.release_notes_url}]{result.release_notes_url}[/link]\n")

    if check:
        console.print("Run [bold cyan]argus update[/bold cyan] to install this version.\n")
        return

    if result.is_source_checkout and not target_method:
        console.print(
            "[bold red]Refusing automated update:[/bold red] Argus is running from a git checkout.\n"
            "To prevent overwriting local code, automated updates are disabled.\n"
            "Use 'git pull' or specify [bold]--method pip[/bold] (or uv/pipx) to force.\n"
        )
        sys.exit(1)

    effective_method = target_method or result.install_method
    if not yes:
        proceed = click.confirm(
            f"Do you want to upgrade argus-recovery to v{result.latest_version} via {effective_method.value}?",
            default=True,
        )
        if not proceed:
            console.print("[dim]Update cancelled by user.[/dim]\n")
            return

    with console.status(f"[bold #818cf8]Upgrading argus-recovery via {effective_method.value}...[/bold #818cf8]"):
        success, msg = apply_update(
            method=effective_method,
            target_version=result.latest_version,
            pre=pre,
        )

    if not success:
        console.print(f"[bold red]✗ Update failed:[/bold red]\n{msg}\n")
        sys.exit(1)

    with console.status("[dim]Verifying updated CLI version...[/dim]"):
        verified, v_msg = verify_update(expected_version=result.latest_version)

    if verified:
        console.print(f"[bold green]✓ Successfully upgraded to Argus v{result.latest_version}![/bold green]\n")
    else:
        console.print(
            f"[bold green]✓ Package manager update completed.[/bold green]\n"
            f"[dim]Note on verification: {v_msg}[/dim]\n"
            f"Please restart your shell or run 'argus --version' to verify.\n"
        )


@main.command("test-graphics")
@click.option(
    "--protocol",
    "-p",
    type=click.Choice(["auto", "sixel", "kitty", "iterm2", "all"]),
    default="auto",
    help="Target graphics protocol to test (default: auto)",
)
@click.option(
    "--wordmark-only",
    is_flag=True,
    help="Render wordmark only (SHARY Thin) without emblem.",
)
def test_graphics_cmd(protocol: str, wordmark_only: bool):
    """Test terminal inline image rendering protocols (Sixel, Kitty, iTerm2)."""
    from argus.utils.terminal_graphics import detect_graphics_protocol, print_banner

    console.print("[bold #818cf8]Argus Terminal Inline Graphics Tester[/bold #818cf8]\n")
    detected = detect_graphics_protocol()
    console.print(f"Detected Protocol : [cyan]{detected or 'None (using Sixel default)'}[/cyan]")
    console.print(f"Active TERM       : [dim]{os.environ.get('TERM', 'unset')}[/dim]")
    console.print(f"TERM_PROGRAM      : [dim]{os.environ.get('TERM_PROGRAM', 'unset')}[/dim]")
    console.print(f"WT_SESSION        : [dim]{'Present (Windows Terminal)' if os.environ.get('WT_SESSION') else 'Not detected'}[/dim]\n")

    protocols_to_test = (
        ["sixel", "kitty", "iterm2"]
        if protocol == "all"
        else [detected or "sixel"]
        if protocol == "auto"
        else [protocol]
    )

    for p in protocols_to_test:
        console.print(f"--> Testing protocol [bold yellow][{p.upper()}][/bold yellow]:")
        success = print_banner(protocol=p, wordmark_only=wordmark_only)
        if success:
            console.print(f"    [dim]Sent [{p}] escape sequences to terminal.[/dim]\n")
        else:
            console.print(f"    [red]Failed to send [{p}].[/red]\n")

    console.print("-" * 64)
    console.print("[bold]Check your terminal above:[/bold]")
    console.print("If an image rendered, which protocol worked? (Sixel, Kitty, or iTerm2)")
    console.print("-" * 64)


if __name__ == "__main__":
    main()
