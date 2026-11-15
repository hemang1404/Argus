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
  <a href="#interactive-terminal-cockpit">TUI</a> ·
  <a href="#model-context-protocol-mcp">MCP Server</a> ·
  <a href="#local-gateway-proxy-experimental">Gateway</a> ·
  <a href="docs/SECURITY_MODEL.md">Security</a> ·
  <a href="docs/RESEARCH_STATUS.md">Research status</a> ·
  <a href="examples/">Examples</a> ·
  <a href="https://argus.hemangsharrma.workers.dev">Documentation Site</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/argus-recovery/"><img src="https://img.shields.io/pypi/v/argus-recovery.svg?color=blue" alt="PyPI version"></a>
  <a href="https://pypi.org/project/argus-recovery/"><img src="https://img.shields.io/pypi/pyversions/argus-recovery.svg" alt="Python versions"></a>
  <a href="https://github.com/hemang1404/Argus/actions/workflows/ci.yml"><img src="https://github.com/hemang1404/Argus/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"></a>
</p>

<p align="center">
  <strong>A bounded, test-gated recovery runtime for model-generated code edits.</strong>
</p>

---

When a failing test is handed to a model, the model's edit often fails for reasons that have nothing to do with the bug: the search text has drifted, the indentation is off, the match is ambiguous, or the result no longer parses. Argus is the runtime around that step. You give it a failing test command. It copies the project into a temporary workspace, asks **your own configured model** for an edit, reconciles that edit against the file, re-runs your test command in the copy, and, if it passes, stores the result as a **candidate**. Nothing touches your real files until you approve the candidate and apply it.

Argus is a local tool, not a service. It has no Argus-owned keys and no hidden fallback: it only calls a provider you configured, and with none configured it stops with an actionable error.

## What it does

- **Tiered patch reconciliation.** SEARCH/REPLACE blocks are located in the file through exact matching, then whitespace/indentation-tolerant matching, then (default on) a token-equal match that may differ only in whitespace and comments. Tier 4 (anchor matching) is experimental and off unless you pass `--enable-tier4`. Anything ambiguous or not found is refused, never guessed. See [the tier table](#the-patch-engine).
- **Syntax gate, not a correctness proof.** Every result must parse (`ast.parse` plus `compile`) before it is tried. That only means "valid Python syntax". Whether the change is right is decided by your tests, and even they are only test-relative.
- **Isolated attempts.** Each run works in a temporary copy of your project (by default), so failed attempts never touch your working tree, including the preliminary test run that localizes the file when `--file` is omitted (that run also happens in the copy). The copy is not a security sandbox; see [Trust model](#trust-model-read-this-first).
- **Feedback loop with a bounded budget.** Failed attempts feed the test output back to the model. Attempts, time, output size and spend all have ceilings. A model with no known price is refused rather than assumed free, unless the provider is local.
- **Approval-gated apply.** Candidates are stored under `.argus/runs/<run_id>/` with baseline and candidate SHA-256 hashes and an HMAC signature. `argus apply` refuses unless the candidate is verified, approved, unmodified, and the target file still matches its baseline.
- **Interfaces.** CLI, a Textual TUI, an MCP stdio server for agents, and an experimental local gateway.

### Success levels, kept separate

Argus reports four different things. They are not interchangeable:

| Level | Meaning | Who establishes it |
| :--- | :--- | :--- |
| Applied | The edit was located and written into the workspace copy | Patch engine |
| Syntactic | The patched file parses as Python | `ast.parse` + `compile`; says nothing about behaviour |
| Tests pass | The single test command you supplied passed in the copy | Your test command ("verified" in Argus output) |
| Semantically correct | The change does what you meant | You, by reading the diff. Argus does not claim this |

"Verified" in Argus output always means *tests pass* for the command you gave it, nothing more.

### Who this is for

Developers and agent builders who want model edits to fail safely: bounded, reviewable, never applied silently. It is not an LLM chat agent, not an IDE autocomplete tool and not an autonomous developer. Scope today is single-file, Python-source recovery.

---

## Trust model (read this first)

> **Argus runs your test command, including any model-generated code it exercises, directly on your machine with your user permissions. It is not sandboxed.** The temporary workspace copy protects your working tree from half-applied edits; it does not stop a test (or a malicious repository) from reading your files, using the network or deleting things you own. Only run it on repositories you trust. `argus run` prints this warning before it starts.

Argus also never sends anything to a provider you did not configure, and never applies a candidate you did not approve. Provider credentials live in your OS keyring (with a restricted-permission fallback file). See [SECURITY.md](SECURITY.md) and [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).

---

## Quickstart

### 1. Install

Requires Python 3.11 or newer.

```bash
pip install argus-recovery            # CLI, MCP server, Python API
pip install "argus-recovery[tui]"     # adds the Textual terminal UI
pip install "argus-recovery[gateway]" # adds the experimental local gateway
pip install "argus-recovery[tokens]"  # optional exact token counting (downloads a vocabulary once)
# or, isolated: pipx install argus-recovery
```

The `0.2.0b1` release on PyPI predates the audit remediation described in [CHANGELOG.md](CHANGELOG.md); that work is version `0.2.0b2`, not yet published. To get the current behaviour documented here, install from the repository:

```bash
pip install "git+https://github.com/hemang1404/Argus.git"
```

### 2. Configure a provider you own

Argus uses only a provider you configure. The hidden prompt keeps the key out of your shell history:

```bash
argus provider set --preset openai      # prompts for the API key (hidden input)
argus provider list
```

Presets: `antigravity` (host CLI, no key), `ollama`, `lmstudio`, `openai`, `deepseek`, `gemini`, `openrouter`, `custom` (any OpenAI-compatible endpoint). Anthropic models are reachable through `openrouter` or a `custom` endpoint; there is no dedicated Anthropic preset. Groq is refused.

### 3. Check the environment

```bash
argus doctor
```

`doctor` reports Python, Git, pytest, the Antigravity CLI, a local Ollama endpoint and the active provider. By default it makes one small live request to the active provider, which can be billable; use `argus doctor --offline` to skip that connectivity check.

If the Antigravity host CLI was installed through npm and only a `.cmd`/`.bat` shim exists, Argus refuses to run it (Windows would re-parse the prompt in `cmd.exe`). Point `ARGUS_AGY_BIN` at the real `agy.exe`.

### 4. Run a recovery

Give Argus the failing test command. `--file` names the file to repair; omit it to let Argus localize the file from the traceback.

```bash
argus run --test "pytest tests/test_payment.py" --file src/payment.py
```

`recover` is an alias of `run`. The test command is run without a shell: operators such as `&&`, `|`, `;`, `>` are rejected.

### 5. Review, approve, apply

A successful run prints a run ID. Nothing has changed in your repository yet.

```bash
argus candidates                  # list recent runs
argus inspect <run_id>            # metadata, patch tier used, diff
argus diff <run_id>               # the unified diff
argus approve <run_id>            # records your approval (asks for confirmation)
argus apply --run-id <run_id>     # re-checks hashes and syntax, then writes atomically
```

`argus apply` without `--run-id` uses the most recent run. Approval is recorded separately from verification, and a candidate whose target file changed since the run started is refused (`STALE_TARGET_CONFLICT`). A second `apply` reports that the run was already applied. Run directories under `.argus/` are not cleaned up automatically; Argus writes `.argus/.gitignore` so they stay out of `git status`, but they can contain source context, so delete old runs yourself.

### 6. Python API

The same controller backs every interface. The example below is a real, runnable script that uses a stub provider so it works offline; see [`examples/quickstart_recovery.py`](examples/quickstart_recovery.py).

```python
from pathlib import Path
from argus.config import ArgusConfig
from argus.runtime.controller import RecoveryController
from argus.runtime.candidate import AuthoritativePatchApplier, CandidateArtifactStore

config = ArgusConfig(max_attempts=3, budget_limit_usd=0.03)   # uses your stored active provider
controller = RecoveryController(config=config)
outcome = controller.run(
    test_command="pytest tests/test_calculator.py",
    target_file="src/calculator.py",          # omit to auto-localize
    workspace_root=Path.cwd(),
)
print(outcome.status, outcome.verified, outcome.run_id, outcome.tier_used)
print(outcome.unified_diff)

if outcome.status == "SUCCESS":
    CandidateArtifactStore.set_approval(outcome.run_id, approved=True, workspace_root=Path.cwd())
    result = AuthoritativePatchApplier.apply(outcome.run_id, workspace_root=Path.cwd())
    print(result.success, result.error_code)
```

`run()` returns a `RecoveryOutcome`. Only `status == "SUCCESS"` yields an applicable candidate, and `verified` means the test command passed in the copy.

---

## Model selection and cost

- **A model you name is the model Argus uses.** `--model X` (or `ArgusConfig(model=X)`) pins one model for every attempt, with no substitution and no escalation.
- **Cheap-first cascade, only when you name no model.** For providers with a documented ladder, attempts 1-2 use the cheaper model and attempt 3 the stronger one: `openai` gpt-4o-mini then gpt-4o; `deepseek` deepseek-flash then deepseek-chat; `gemini` gemini-2.5-flash then gemini-2.5-pro; `antigravity` gemini-3.8-flash-low then gemini-3.8-flash-high. Other providers use their default model with no escalation. You can set `small_model`/`large_model` explicitly.
- **Prices are never guessed.** A model that is not in the built-in price table, and is not served by a local provider, is refused with `CONFIG_ERROR` until you supply a price (`--price MODEL=IN,OUT` in USD per 1M tokens on the CLI, or `ArgusConfig.model_prices`). Spend that cannot be established after a failed call is quarantined against the budget.
- **Limits.** Defaults: 3 attempts (1 to 10 allowed), `$0.03` budget (at most `$10` on the CLI), 30 s per test run, 300 s task deadline. The per-run summary shows the model ladder that was actually used.
- **Precedence.** Caller values (CLI flags, API/MCP arguments) win. A project file (`argus.yaml`, `.argus.yaml`, or `[tool.argus]` in `pyproject.toml` in the **workspace root**) may only tighten limits (lower attempts, budget or timeout; turn escalation off); it cannot choose a model or provider or raise a cap, and Argus prints what it applied or ignored. Environment variables supply credentials and, as a last resort, the provider name.

---

## The patch engine

| Tier | Strategy | Default | What it accepts |
| :--- | :--- | :--- | :--- |
| 1 | Exact | on | The search text occurs verbatim at a line or token boundary; never inside a longer identifier, comment or string it does not fully cover. Whole-line matches are preferred. |
| 2 | Whitespace/indentation | on | Whole lines that match after normalizing indentation and trailing whitespace. |
| 3 | Token-equal | on (`enable_tier3=False` to disable) | A unique region whose identifier, operator, number and string tokens are **identical** to the search block; only whitespace and comments may differ. `<` vs `<=`, `return a` vs `return b`, `3` vs `5` are refused. |
| 4 | Anchor | **off** | Experimental. Prefix/suffix anchors around a drifted interior. Opt in with `--enable-tier4`. |
| 5 | Refusal | n/a | Not an algorithm: the outcome when nothing unambiguous matches. The file is left unchanged. |

Line endings (LF, CRLF, CR, mixed), UTF-8 BOMs and PEP 263 encodings are preserved; undecodable files are refused instead of rewritten. A malformed or truncated response applies nothing, and the model is told which block was damaged.

What the evidence supports about these tiers is on the [research status page](docs/RESEARCH_STATUS.md). In short: most of the gain over a plain exact matcher comes from Tiers 1-2; Tier 3 (in its earlier fuzzy form) produced one extra solve and one wrong-location application on a 99-task sample, which is why it is now token-equal rather than similarity-based; Tier 4 has shown no benefit.

---

## Interactive Terminal Cockpit

```bash
pip install "argus-recovery[tui]"
argus tui
```

A keyboard-driven Textual interface: enter a test command, watch the attempt timeline, review the diff, and approve. Approving and applying from the diff screen goes through the same signed-candidate checks as the CLI. The command palette (`/` or `Ctrl+P`) reaches provider setup, `/connect`, diagnostics and update checks.

---

## Model Context Protocol (MCP)

`argus mcp run` starts a stdio MCP server so an agent can request recoveries:

```bash
argus mcp verify      # check the handshake and tool list
argus mcp run         # start the stdio server
argus mcp install     # write config for: antigravity, opencode, gemini (or all)
```

Manual configuration for any MCP client:

```json
{ "mcpServers": { "argus": { "command": "argus", "args": ["mcp", "run"] } } }
```

`mcp install` currently knows three hosts: Antigravity, OpenCode and Gemini CLI. For other MCP clients (Claude Desktop, Cursor and so on) use the manual entry above.

Tools: `argus_status`, `argus_recover`, `argus_get_candidate`, `argus_apply_candidate`. There is deliberately **no approval tool**: an agent can propose and request apply, but a human records approval with `argus approve`, and approval records are signed with a key in your configuration directory, which stops forgery by editing files in the workspace but not by code that runs as your OS user: the test command (including a `test_command` an MCP agent supplies) is such code, so only expose Argus over MCP for repositories and agents you trust. See [SECURITY.md](SECURITY.md). The server confines its workspace to the directory it was started in (or `ARGUS_MCP_WORKSPACE_ROOT`) and caps an agent-supplied budget at `$0.25` (override ceiling with `ARGUS_MCP_MAX_BUDGET_USD`). MCP responses carry the host-execution notice.

---

## Local Gateway Proxy (experimental)

`argus gateway` (alias `argus serve`) starts a loopback OpenAI- and Anthropic-compatible proxy with budget reservation and streaming patch healing. It needs the `gateway` extra and `ARGUS_GATEWAY_API_KEY`; it refuses to start without a key, binds to `127.0.0.1` by default, validates `Host`/`Origin`, and needs `--allow-remote` for non-loopback binding. Unknown models are priced fail-closed. It is outside the recovery loop and is not required for anything else.

**Experimental, not started by the CLI.** The package also contains a local desktop HTTP/SSE API (`argus.api.create_app`) and a static web app (`argus/ui`). No `argus` command starts either of them, and they are not covered by the supported-surface claims above (the supported surfaces are the CLI, TUI, MCP server and Python API). They need the `gateway` extra (`fastapi`, `uvicorn`) and you would have to serve `create_app()` yourself. Treat them as unreleased.

---

## What is in this repository

| Path | What it is |
| :--- | :--- |
| [`argus/`](argus/) | The package (`argus-recovery`): controller, workspace manager, patch engine, providers, CLI, TUI, MCP server, gateway. |
| [`argus-website/`](argus-website/) | Source of the documentation site at [argus.hemangsharrma.workers.dev](https://argus.hemangsharrma.workers.dev) (Astro/Starlight). |
| [`docs/`](docs/) | [Architecture](docs/ARCHITECTURE.md), [security model](docs/SECURITY_MODEL.md), [benchmark methodology](docs/BENCHMARK_METHODOLOGY.md), [research status](docs/RESEARCH_STATUS.md). |
| [`examples/`](examples/) | Runnable offline examples (`quickstart_recovery.py`, `custom_provider_config.py`). |
| [`tests/`](tests/) | The offline test suite, including a 500-case synthetic patch corpus. |

The experiment data and reports behind the research claims are **not** in this repository; see [docs/RESEARCH_STATUS.md](docs/RESEARCH_STATUS.md).

---

## Research status in one paragraph

Argus's patch-apply layer is a tolerant SEARCH/REPLACE matcher. In a separate, preregistered evaluation under official SWE-bench Verified grading (99 design tasks, target file given to the model, temperature 1.0), it raised gpt-oss-20b from 31 to 38 resolved tasks (7 gained, 0 lost, exact McNemar p = 0.016; +5.7 points, 95% CI 2.7-9.1), gpt-oss-120b by 2 tasks (not significant), and Qwen3.8 by about zero. Most of that gain comes from exact and indentation-tolerant matching that other edit tools also provide. Feeding test output back beat blind re-draws for gpt-oss-20b; a 20B-then-120B cascade matched or beat 120B alone at about 1.1x spend in an offline replay. No learned router is shipped or supported. "Resolved" means the target test and graded suite pass, not that the patch is correct. The raw data lives outside this repository, so none of this is reproducible from a clone; the per-claim table, with what is verified, partial, unsupported or incorrect, is in [docs/RESEARCH_STATUS.md](docs/RESEARCH_STATUS.md).

---

## Platforms and testing

Python 3.11 to 3.14. Argus is developed and exercised mainly on Windows; the CI workflow runs the offline suite on Linux, Windows and macOS (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)). Behaviour on POSIX that is not covered by CI (process-group termination, file modes) has had less real-world use.

```bash
pip install -e ".[dev,tui,gateway]"
ARGUS_OFFLINE=1 pytest -q -m "not live_provider and not live_antigravity and not real_host and not tokenizer_download"   # offline suite; no model calls
ruff check .
```

The marker selector excludes tests that call a locally installed Antigravity CLI or a live provider (real inference calls) and tests that download the tiktoken vocabulary; `ARGUS_OFFLINE=1` makes token estimates use the byte heuristic instead of a download. Tests that need Docker skip themselves when Docker is absent. The 500-case adversarial patch corpus (`tests/test_patch_adversarial_corpus.py`) is synthetic (seven templated mutation families): "zero incorrect applications" is a statement about that corpus, and one wrong-location application has been observed on real model output.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md), [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Security issues: [`SECURITY.md`](SECURITY.md).

## License

Argus is released under the [MIT License](LICENSE).
