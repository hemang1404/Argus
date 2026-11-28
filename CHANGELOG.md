# Changelog

All notable changes to Argus are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## Next release (not yet published)

### Changed
- The model configuration dialog keeps Cancel and Apply visible on short terminals and scrolls its model and API key fields.
- The help dialog keeps its Close button visible on short terminals and shows the correct shortcuts for policy settings and the command palette.
- **Groq is now a normal provider.** The earlier blanket prohibition was removed: a `groq` preset (OpenAI-compatible, default model `openai/gpt-oss-120b`, your own `GROQ_API_KEY`) works in the CLI, TUI, MCP server, Python API and gateway. Like every provider it is used only when you configure or name it, unknown models are still refused until priced, and `GROQ_` variables are still scrubbed from the test subprocess environment.

### Fixed
- `SubprocessTestRunner` now checks the original repository root (`venv_root`) for `.venv`/`venv` when executing tests in an isolated temp workspace copy, so projects using local virtualenvs keep their dependencies when Argus runs from a separate Python environment (#42).
- The gateway no longer bills a free local model as uncertain spend after each request. A model absent from the price registry served by Ollama, LM Studio or a loopback host passed the pre-flight price check at $0.00, but the commit after the request priced it by name only and charged the whole reservation as uncertain, so a few turns exhausted `per_task_usd` / `per_session_usd` and later free requests got HTTP 429 (`budget_exceeded`). The commit now prices with the routed provider and upstream URL, as the pre-flight check does, on the sync, streaming and Anthropic paths; an unpriced remote model still fails closed (#43).
- Patch validation now preserves real top-level `a/` and `b/` directories after Git diff prefix removal, preventing cross-file path confusion (#46).
- With the Antigravity CLI installed, a configured `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) now lists `gemini-2.5-flash` and `gemini-2.5-pro` as `Ready · API Key` in `discover_all_models()` and the model picker. A model ID the host CLI already lists keeps its host entry, so picker IDs stay unique; without a key, host users still see no "Needs API Key" Gemini entries (#22).

### Development
- `ruff check .` now enforces import sorting (rule `I`, with `argus` as first-party), and existing imports in `argus/` and `tests/` are sorted. The vendored Flask fixture in `tests/fixtures/` is excluded from ruff so it stays byte-for-byte identical to upstream (#33).

### Tests
- Added `tests/test_cascade_reachability.py`, which pins the current behaviour that the CLI and MCP server always send the stored provider's model (so the cheap-first cascade applies only to library use of `ArgusConfig`).

---

## [0.2.0b2] - 2026-11-16

Changes from a full engineering audit (November 2026). Version `0.2.0b2` was tagged `v0.2.0b2` and published to PyPI on 2026-11-16; the older `0.2.0b1` package predates all of this.

### Changed (behaviour)
- **Model dispatch.** The configured model is authoritative. A named model is pinned for every attempt (no substitution, no escalation). The cheap-first cascade (openai gpt-4o-mini -> gpt-4o, deepseek deepseek-flash -> deepseek-chat, gemini gemini-2.5-flash -> gemini-2.5-pro, antigravity gemini-3.8-flash-low -> gemini-3.8-flash-high) applies only when no model is named. The old fallback to a small local model on budget pressure is gone (`BUDGET_EXCEEDED`). The `argus/auto` pseudo-model means "no model". Antigravity no longer downgrades pro-class names to flash, and the executed model is recorded per attempt.
- **Patch engine.** Tier 3 is now token-equal matching (only whitespace and comments may differ), so near misses such as `<` vs `<=`, `return a` vs `return b` or `3` vs `5` are refused. Tier 1 requires a line/token boundary. LF/CRLF/CR/mixed endings, BOMs and PEP 263 encodings are preserved; undecodable files are refused (`UNSUPPORTED_ENCODING`). Truncated or malformed SEARCH/REPLACE blocks apply nothing and are reported. The validation gate is `ast.parse` plus `compile` and is labelled syntactic. Already-applied replacements report `REFUSED_ALREADY_APPLIED`.
- **Workspace.** The copy snapshot is Git-tracked plus untracked-not-ignored files (or a full walk); `data/`, `build/`, `dist/`, `results/`, `packages/`, `logs/`, `scratch/` and archives are no longer silently dropped. Size-bounded (`snapshot_max_bytes`, default 2 GiB). A target outside an explicit workspace root is refused. `.argus/.gitignore` is created. Concurrent applies of one run are serialized; a second apply says "already applied".
- **Signed candidates.** `metadata.json` is HMAC-signed with a key in the user config directory; unsigned or edited runs give `CANDIDATE_SIGNATURE_INVALID`; run IDs are validated (`INVALID_RUN_ID`). Runs from earlier versions must be re-created.
- **Project policy** (`argus.yaml`, `.argus.yaml`, `[tool.argus]`) is read from the workspace root, not the current directory, and may only tighten limits; `model`/`provider` keys are ignored with a note.
- **Cost safety.** Unknown model prices are refused (`CONFIG_ERROR`) unless the provider is local or host-native; non-finite or negative budgets and invalid token counts are rejected; an unknown provider is a `CONFIG_ERROR`. The controller no longer uses a process-wide budget singleton.
- **Duplicate calls.** A `TypeError` from an adapter no longer triggers a second, silent paid call.
- **MCP.** Workspace confined to the server root; budget capped at `$0.25` (`ARGUS_MCP_MAX_BUDGET_USD`); there is still no approval tool, and approval is now unforgeable by file edits. Payloads carry the host-execution notice.
- **Gateway.** The `PYTEST_CURRENT_TEST` / `ARGUS_TEST_MODE` authentication and Host bypasses were removed; the run-ID header is validated and no longer read from an API key header; budget reservations expire.
- **Launcher.** The Windows fallback no longer uses a shell string.

### Removed
- **Git worktree isolation** (it was unreachable from any CLI, API, MCP or TUI path). The temporary copy is the only mode. Earlier documentation claiming worktrees was wrong.

### Documentation
- Corrected the quickstart command, SDK snippet and example, `argus mcp serve` -> `argus mcp run`, MCP install targets (antigravity, opencode, gemini), nonexistent paths and flags (`.argus/artifacts`, `applier.py`, `--container`), tier descriptions, the model ladder text, "tested providers" claims, PyPI statements and the host-execution trust statement.
- Added `docs/RESEARCH_STATUS.md`, a claim-by-claim research status table. Removed or corrected unsupported research claims (26 -> 50, 77% cost, 200-400x, 92%, "fix rate", "zero incorrect applications" without scope).
- Test counts: earlier documents quoted 471 and 489 passing tests by hand and disagreed. The only figure measured for this release: on 2026-11-16, Windows 11, Python 3.14, `pytest tests -m "not real_host and not live_provider and not live_antigravity and not tokenizer_download"` with `ARGUS_OFFLINE=1` and an isolated `ARGUS_CONFIG_DIR` gave 806 passed, 11 skipped, 8 deselected (215 s), 0 failed. It is a point-in-time measurement on one machine, not a guarantee; CI is the source of truth.

## [0.2.0b1] - 2026-11-06

> Errata for this release (found by the audit): it did not use git worktrees by default (copy mode only); artifacts live in `.argus/runs/<run_id>/`, not `.argus/artifacts/`; the applier is in `argus/runtime/candidate.py`; the "471 passing tests" and "0 incorrect applications" lines below describe the author's environment and the synthetic corpus respectively; Tier 3 matched by character similarity, which could apply a wrong-location edit (fixed above).

### Added
- **Multi-Tier Resilient Patch Engine:**
  - Tier 1: Byte-for-byte exact character matching.
  - Tier 2: Whitespace and leading indentation normalization.
  - Tier 3: Character-level sliding-window similarity matching with ambiguity margin checks.
  - Tier 4: Experimental anchor-based prefix/suffix matching (disabled by default, opt-in via `--enable-tier4`).
  - Tier 5: Safe refusal mechanism when patches are ambiguous or below similarity thresholds.
  - Python Abstract Syntax Tree (`ast.parse`) validation gating all successful patch candidates.
- **Session-Aware Workspace Management:**
  - Ephemeral project copy mode in `%TEMP%/argus_ws_*` with heavy directory exclusion (`.git`, `node_modules`, `venv`, `build`).
  - Git worktree mode (`git worktree add --detach`) with automatic uncommitted change overlay.
  - Process start-time identity tracking (`kernel32.GetProcessTimes` on Windows, `/proc/{pid}/stat` field 22 on POSIX) preventing false retention from OS PID recycling.
  - Atomic rename directory claiming preventing concurrent garbage collection collisions.
- **Bounded Subprocess Supervision:**
  - `BoundedOutputCollector` draining concurrent stdout/stderr streams with 4MB memory bounds, verified under 1 GB logical stream flood.
  - Hard process-tree killing on timeout (`taskkill /F /T` on Windows, `os.killpg` on POSIX) verified down to child and grandchild worker processes.
  - Direct execution with `shell=False` and strict tokenization rejecting shell chaining operators and redirection.
- **Authoritative Candidate Application & TOCTOU Defense:**
  - Cryptographic candidate artifacts written to `.argus/artifacts/<run_id>.json` recording baseline and candidate SHA-256 hashes.
  - `AuthoritativePatchApplier` verifying file integrity before application and executing atomic commits via `os.replace`.
- **Developer Interfaces:**
  - Command-Line Interface (`argus run`, `argus recover`, `argus candidates`, `argus diff`, `argus apply`).
  - Interactive Textual TUI cockpit (`argus tui`) with command palette, timeline stream, and diff review.
  - FastMCP stdio server (`argus mcp`) exposing tool definitions for upstream agents.
- **Experimental Gateway Proxy (`argus gateway`):**
  - OpenAI- and Anthropic-compatible reverse proxy with dynamic two-phase budget reservation using exact integer micro-dollars.
  - Loopback Host and Origin validation protecting against DNS rebinding.
  - Multi-client identity-based rate limiting on `127.0.0.1` preventing cross-tool starvation.
  - Mandatory constant-time Bearer token authentication (`ARGUS_GATEWAY_API_KEY`).
  - Strict loopback binding default; non-loopback exposure requires explicit `--allow-remote`.
- **Comprehensive Quality Gates:**
  - 471 passing tests in the full offline test suite.
  - 121 passing tests in the expanded core gate across three consecutive runs.
  - 500-case synthetic adversarial patch corpus with 0 incorrect applications.
  - POSIX process group and PID start-time verification under WSL2.

### Security
- Complete Groq prohibition: rejected across all components, configs, and gateway routing.
- Subprocess environment and error traceback secret scrubbing (`scrub_secrets`).
- Fail-closed container isolation: refuses host execution when container mode is requested and container engine is unavailable.
- Information disclosure protection: Gateway `/health` suppresses internal telemetry for unauthenticated callers; `/v1/models` requires valid bearer token.
