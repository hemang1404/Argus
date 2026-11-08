<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/argus-wordmark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/argus-wordmark.svg">
    <img src="assets/argus-wordmark.svg" alt="ARGUS" width="480">
  </picture>
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> ·
  <a href="https://pypi.org/project/argus-recovery/">PyPI</a> ·
  <a href="docs/ARCHITECTURE.md">Architecture</a> ·
  <a href="#interactive-terminal-cockpit">TUI Cockpit</a> ·
  <a href="#model-context-protocol-mcp">MCP Server</a> ·
  <a href="#local-gateway-proxy">Gateway</a> ·
  <a href="docs/SECURITY_MODEL.md">Security</a> ·
  <a href="examples/">Examples</a> ·
  <a href="benchmarks/">Benchmarks</a> ·
  <a href="https://argus.hemangsharrma.workers.dev">Documentation Site</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/argus-recovery/"><img src="https://img.shields.io/pypi/v/argus-recovery.svg?color=blue" alt="PyPI version"></a>
  <a href="https://pypi.org/project/argus-recovery/"><img src="https://img.shields.io/pypi/pyversions/argus-recovery.svg" alt="Python versions"></a>
  <a href="https://github.com/hemang1404/Argus/actions/workflows/ci.yml"><img src="https://github.com/hemang1404/Argus/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"></a>
</p>

<p align="center">
  <strong>The missing bounded, test-gated recovery runtime for AI coding agents.</strong>
</p>

---

Most autonomous coding agents and IDE assistants generate raw patch edits directly into your live working tree and leave you to manually untangle syntax breaks, whitespace rejections, merge conflicts, or test regressions.

**Argus is the other half:** a local, bounded, test-gated recovery runtime that intercepts failing tests, virtualizes workspaces in transient git worktrees, reconciles drifted patch blocks using bounded fuzzy sliding-window matching, and guarantees that candidate repairs pass your test suite before a single byte touches your active repository.

- **5-tier resilient patch reconciler (Tiers 1–5):** Exact byte match, indentation/whitespace normalization, character-level sliding-window fuzzy matching (0.82 similarity threshold, 0.10 ambiguity margin), anchor matching, and safe refusal. Verified across our 500-case synthetic adversarial patch corpus with zero incorrect applications.
- **Transient worktree & ephemeral sandbox isolation:** Runs test executions in isolated Git worktrees or temporary filesystem mirrors (`$TEMP/.argus-worktrees/`). Your uncommitted changes and active branch are completely protected during recovery loops.
- **Authoritative approval-gated patch application:** Cryptographic SHA-256 pre- and post-condition hashes protect against Time-of-Check to Time-of-Use (TOCTOU) race conditions. Approved patches are committed to disk atomically (`os.replace`).
- **Dynamic tier-based model routing & budget ceilings:** Configurable escalation across T1 Fast (`gemini-2.5-flash`), T2 Strong (`claude-3-7-sonnet` / `gemini-2.5-pro`), and T3 Frontier (`claude-3-opus`) with strict per-task and per-session hard dollar caps. Prohibited providers (Groq) are strictly refused at runtime.
- **Interactive Textual TUI Cockpit (`argus tui`):** A keyboard-driven terminal cockpit featuring real-time execution timelines, phase spinners, model tier switcher, and a side-by-side / unified diff reviewer with approval gates.
- **Model Context Protocol (FastMCP) server:** 4 native recovery tools (`argus_status`, `argus_recover`, `argus_get_candidate`, `argus_apply_candidate`) exposing Argus recovery directly to Claude Code, Cursor, Windsurf, or custom agent frameworks via stdio.
- **Local OpenAI & Anthropic compatible gateway:** A loopback proxy (`127.0.0.1:8000/v1`) with two-phase budget reservation, exponential backoff retries, and hardened SSRF / DNS rebinding defenses.
- **100% local execution & privacy first:** Runs entirely on your machine. Zero cloud telemetry, zero prompt tracking, MIT-licensed, with native Antigravity host integration or standard Bring-Your-Own-Key (BYOK) APIs.

**Who this is for:** Developers, engineers, and teams running autonomous coding agents (Claude Code, Cursor, Aider, OpenCode, Antigravity) who need reliable, test-verified patch repairs without letting an LLM break their working tree or blow through API quotas.

**Who this is NOT for:** Argus is not an LLM chat agent, an IDE autocomplete widget, or an open-ended autonomous software developer. If you just want a chatbot in your terminal, Argus is the wrong layer.

---

## Start Here

If you are new to the repository, use this decision tree to find the right path:

| I want to... | Use this path |
| :--- | :--- |
| **Fix a failing test right now** | Use `argus recover "pytest tests/..."` from the [Quickstart](#quickstart) below. |
| **Launch an interactive terminal dashboard** | Run `argus tui` to launch the Textual cockpit with visual diff reviews and model routing. |
| **Connect Argus to Cursor, Claude Code, or Aider** | Run `argus setup` or `/connect` in the TUI, or run `argus init <client>`. |
| **Expose recovery tools to my agent via MCP** | Run `argus mcp install` or configure `argus mcp` as a stdio server in your agent's config. |
| **Run the local OpenAI/Anthropic gateway proxy** | Use `argus gateway` to route agent traffic through Argus budget and rate guards. |
| **Inspect the technical architecture & security model** | Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md). |
| **Explore the developer documentation portal** | Visit the live site at [argus.hemangsharrma.workers.dev](https://argus.hemangsharrma.workers.dev) or inspect [`argus-website/`](argus-website/). |

---

## Quickstart

### 1. Installation

Install `argus-recovery` directly from [PyPI](https://pypi.org/project/argus-recovery/) using your preferred Python package manager (requires Python `>=3.11`):

```bash
# Using uv (recommended)
uv pip install argus-recovery

# Using pip
pip install argus-recovery

# With optional interactive terminal UI cockpit
pip install "argus-recovery[all]"

# Using pipx (isolated standalone CLI application)
pipx install argus-recovery
```

### 2. Verify Your Environment

Run `argus doctor` to verify system prerequisites, local Git installation, test runners, and active LLM provider connectivity:

```bash
argus doctor
```

Output:
```text
argus doctor (v0.2.0b1)

  ✓ Python          3.14.2
  ✓ Git             C:\Program Files\Git\cmd\git.EXE
  ✓ Pytest          .venv\Scripts\pytest.EXE
  ✓ Antigravity     C:\Users\dell\AppData\Local\agy\bin\agy.exe
  - Ollama          not detected on localhost:11434
  ✓ Provider        antigravity (gemini-3.8-flash-medium)
  ✓ Credentials     host/local (key not required)
  ✓ Connectivity    3679.5ms (gemini-3.8-flash-medium)
```

### 3. Run Test-Gated Recovery

Point Argus at a failing test command. Argus intercepts the failure, parses the stack trace, spins up an isolated Git worktree, iterates on a patch candidate, verifies it passes tests, and stages an approved diff:

```bash
# Run recovery on a failing test suite
argus recover "pytest tests/test_payment.py"
```

Once verified, inspect the generated patch and authoritatively apply it to your repository:

```bash
# Inspect recent recovery candidates
argus candidates

# View unified diff for the latest candidate
argus diff

# Apply the approved candidate to your working copy
argus apply
```

### 4. Python SDK Quickstart

You can also embed the Argus recovery controller directly inside your own tools or agent scripts:

```python
import asyncio
from pathlib import Path
from argus.runtime.controller import RecoveryController
from argus.config import ArgusConfig

async def main():
    # Configure budget ceiling, attempt limits, and sandbox strategy
    config = ArgusConfig(
        max_budget_usd=0.03,
        max_attempts=3,
        similarity_threshold=0.82,
    )
    controller = RecoveryController(config=config, workspace_root=Path("."))

    # Execute recovery inside an isolated temporary worktree
    candidate = await controller.recover_async(
        test_command="pytest tests/test_calculator.py"
    )

    if candidate.verified:
        print(f"✓ Recovery succeeded (Run ID: {candidate.run_id})")
        print(f"Cost: ${candidate.cost_usd:.4f} | Turns: {candidate.turns}")
        print(candidate.diff)

        # Authoritatively apply the patch with SHA-256 pre-condition checks
        candidate.apply()
    else:
        print(f"✗ Recovery refused: {candidate.failure_reason}")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Interactive Terminal Cockpit

Argus includes a dedicated, full-screen Terminal User Interface (TUI) built on Textual:

```bash
argus tui
```

```text
 ┌────────────────────────────────────────────────────────────────────────┐
 │ ARGUS                                                                  │
 ├────────────────────────────────────────────────────────────────────────┤
 │ ❯ Run test command... "pytest tests/test_auth.py" or "/" for palette   │
 │   Recover · argus/auto · $0.03 cap        tab policy   ctrl+p commands │
 └────────────────────────────────────────────────────────────────────────┘
```

### Key Cockpit Features
- **Real-Time Execution Timeline:** Watch live subprocess execution, frame localization, candidate generation, and verification test phases with spinner status.
- **Side-by-Side & Unified Diff Viewer:** Review patch candidates with syntax highlighting, line numbers, and token change counts before accepting.
- **Model Policy Modal (`Tab`):** Dynamically pin execution tiers (`1` Fast, `2` Strong, `3` Frontier), configure per-run budget caps, and toggle worktree sandbox isolation.
- **Command Palette (`/` or `Ctrl+P`):** Fast keyboard navigation to connect IDEs, run doctor diagnostics, inspect session telemetry, or check for updates.
- **Authoritative Apply Gate (`Enter`):** One-key atomic application guarded by pre-computed SHA-256 baseline hashes.

---

## The 5-Tier Resilient Patch Engine

When coding models emit patches, formatting shifts (tabs vs spaces, modified line wraps, or surrounding code drift) frequently cause standard `diff` and `git apply` tools to fail. Argus resolves this with a 5-tier matching engine:

| Tier | Matching Strategy | Description | Safety Gate |
| :--- | :--- | :--- | :--- |
| **Tier 1** | **Exact Match** | Character-for-character byte match against the source file. | Exact byte boundary |
| **Tier 2** | **Indentation & Whitespace** | Normalizes leading indentation and trailing line whitespace while preserving block structure. | AST syntax validation |
| **Tier 3** | **Sliding-Window Fuzzy** | Character-level sliding-window similarity (threshold 0.82) with an ambiguity margin check (0.10) to reject ambiguous matches. | AST syntax validation + margin check |
| **Tier 4** | **Anchor-Based Matching** | Matches surrounding prefix and suffix code anchors when intermediate code has drifted. | Explicit `--enable-tier4` opt-in required |
| **Tier 5** | **Safe Refusal** | **Not an algorithm.** When active tiers fail or multiple ambiguous matches exist, Argus refuses to modify code. | Fail-closed defense |

> [!NOTE]
> Every candidate patch accepted by Tiers 1–4 must successfully parse into a valid Python Abstract Syntax Tree (`ast.parse`) before being dispatched to the test verification runner. Malformed syntax is discarded immediately.

---

## Model Context Protocol (MCP)

Argus includes a built-in FastMCP stdio server that exposes code recovery capabilities as native tools to any MCP-compliant coding assistant (Claude Code, Cursor, Windsurf, OpenCode):

```bash
# Verify MCP server handshake and tools
argus mcp verify

# Auto-install Argus into detected client configs (Claude Desktop, Cursor, etc.)
argus mcp install
```

### Exposed MCP Tools
1. `argus_status`: Query active workspace status, detected test runners, and recent recovery candidates.
2. `argus_recover`: Execute non-destructive, test-gated recovery on a failing test command.
3. `argus_get_candidate`: Inspect a candidate's unified diff, verification status, and SHA-256 provenance hashes.
4. `argus_apply_candidate`: Authoritatively apply an approved candidate patch to the live working tree.

### Client Configuration

Add Argus to your assistant's MCP configuration:

**Claude Desktop (`claude_desktop_config.json`) / Cursor (`cursor.json`):**
```json
{
  "mcpServers": {
    "argus": {
      "command": "argus",
      "args": ["mcp", "serve"]
    }
  }
}
```

---

## Local Gateway Proxy

Argus provides an authenticated, local inference proxy that speaks both the **OpenAI** (`/v1/chat/completions`) and **Anthropic** (`/v1/messages`) protocols:

```bash
# Launch the gateway on localhost
argus gateway --port 8000
```

### Gateway Architecture & Safety
- **Dual-Protocol Translation:** Transparently translates requests between OpenAI and Anthropic message formats based on the target upstream provider.
- **Two-Phase Budget Reservation:** Pre-calculates token costs against published model pricing sheets, locks budget before dispatch, and releases unspent reservations on failure.
- **SSRF & DNS Rebinding Protection:** Actively blocks link-local addresses, IPv6 representations (`[::ffff:169.254.169.254]`), and cloud metadata endpoints (`169.254.169.254`, `metadata.google.internal`).
- **Loopback Enforcement:** Binds strictly to `127.0.0.1` and validates the `Host` header to protect against browser-based local port rebinding attacks.

---

## What's in This Repo

This repository is organized into distinct, focused modules:

| Directory / Package | What it is |
| :--- | :--- |
| [`argus/`](argus/) (`argus-recovery` on PyPI) | **The Core Runtime:** Recovery controller, workspace sandbox, 5-tier patch engine, provider adapters, Textual TUI, local gateway, and FastMCP server. |
| [`argus-website/`](argus-website/) | **Documentation Website:** Production documentation portal deployed at [argus.hemangsharrma.workers.dev](https://argus.hemangsharrma.workers.dev), built with Astro & Starlight, featuring custom monochromatic design, TUI simulator, and dark/light modes. |
| [`docs/`](docs/) | **Architectural Blueprints:** Technical specifications for [Architecture](docs/ARCHITECTURE.md), [Security Model](docs/SECURITY_MODEL.md), and [Benchmark Methodology](docs/BENCHMARK_METHODOLOGY.md). |
| [`examples/`](examples/) | **Runnable Cookbook:** Ready-to-run examples for [quickstart recovery](examples/quickstart_recovery.py) and [custom provider configuration](examples/custom_provider_config.py). |
| [`benchmarks/`](benchmarks/) | **Benchmark Suites:** Evaluation harnesses, verified task runners, and reproducible benchmark configurations. |
| [`tests/`](tests/) | **Verification Test Suite:** 489 passing offline tests, 0 failures, 500-case adversarial patch corpus, and cross-platform quality gates. |

---

## Security Model

Because Argus executes reproduction test commands and edits code, security is designed as layered defense in depth:

1. **Transient Worktree Sandboxing:** All recovery attempts execute in ephemeral git worktrees created in temporary directories. The live repository is never modified during generation or validation.
2. **Authoritative TOCTOU Gate:** Each candidate patch records `baseline_sha256` and `candidate_sha256`. The applier refuses to touch the target file if its on-disk hash has changed since the recovery run started.
3. **Subprocess Argument Validation:** Shell chaining operators (`&&`, `||`, `;`, `|`), backgrounding (`&`), and redirections (`>`, `<`) are strictly rejected. Commands execute without `shell=True`.
4. **Strict Credential Redaction:** Sensitive API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, tokens) are scrubbed before subprocess dispatch and sanitized from error tracebacks.
5. **Provider Prohibition:** Prohibited providers (specifically Groq) are rejected at configuration, CLI, and network levels to prevent unmetered rate-limit failures and maintain reproducible environments.

See [`SECURITY.md`](SECURITY.md) for full vulnerability reporting procedures and threat boundary disclosures.

---

## Published Packages & Supported Platforms

| Distribution Package | Registry | Description |
| :--- | :--- | :--- |
| **`argus-recovery`** | [PyPI](https://pypi.org/project/argus-recovery/) | Complete Argus CLI, TUI cockpit, MCP server, and Python SDK. |

### Platform Matrix

| Operating System | Architectures | Status |
| :--- | :--- | :--- |
| **Linux** (Ubuntu, Debian, Fedora, Arch) | `x86_64`, `aarch64` | Verified |
| **macOS** (Ventura, Sonoma, Sequoia) | Apple Silicon (`arm64`), Intel (`x86_64`) | Verified |
| **Windows** (10, 11 via PowerShell / Terminal) | `x86_64` | Verified |

- **Python Requirements:** Python `>=3.11`, `3.12`, `3.13`, `3.14`.
- **Tested Model Providers:** Google Antigravity Host (`agy`), Google Gemini (`gemini-2.5-flash`, `gemini-2.5-pro`), Anthropic Claude (`claude-3-5-haiku`, `claude-3-7-sonnet`, `claude-3-opus`), OpenAI (`gpt-4o`, `gpt-4o-mini`), and Local Ollama.

---

## Testing & Verification

Argus is verified against a rigorous offline test suite:

```bash
# Run core security and isolation quality gates
pytest tests/test_runtime_and_workspace_safety.py \
       tests/test_gateway_security.py \
       tests/test_wp6_deep_verification.py \
       tests/test_wp7_deep_verification.py -v

# Run the 500-case synthetic adversarial patch corpus
pytest tests/test_patch_adversarial_corpus.py -v

# Run the complete offline verification suite (489 tests)
pytest -q --ignore=tests/test_real_host_integration.py
```

```text
.................................................................sssss.. [ 14%]
............................s........................................... [ 28%]
...............................sss...................................... [ 42%]
......................s...............................ss................ [ 57%]
........................................................................ [ 71%]
................................................s....................... [ 85%]
.....s.................................................................  [100%]
489 passed, 15 skipped in 175.09s
```

---

## Contributing

Contributions are welcome! Please review:
- [`CONTRIBUTING.md`](CONTRIBUTING.md) for local development setup, coding standards, and test guidelines.
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) for community standards.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for deep dive into the runtime architecture.

---

## License

Argus is released under the [MIT License](LICENSE).
