# Changelog

All notable changes to Argus are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.2.0b1] - 2026-11-06

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
