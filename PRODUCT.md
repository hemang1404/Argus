# Argus Product Specification & Capability Inventory

<!-- impeccable:product-schema 1 -->

## 1. Product Identity

- **Product Name:** Argus (Local AI-Assisted Code Recovery Runtime).
- **Category:** Developer Tooling / AI Code Reliability Infrastructure.
- **Core Purpose:** Recover from failed, broken, or malformed AI-generated code edits using bounded test-gated reproduction, multi-tier fuzzy patch reconciliation, and atomic candidate application.
- **Target Audience:** Software engineers, platform teams, and AI agent developers building with coding assistants (Cursor, VS Code, Claude Code, Aider, OpenCode) who require deterministic, test-verified patch recovery without git pollution or runaway inference spend.

---

## 2. Positioning & Boundaries

Argus is an execution and safety substrate that sits beneath coding assistants and developer workflows.

- **What Argus Is:** A local CLI runtime, interactive TUI cockpit, Model Context Protocol (FastMCP) server, and auxiliary reverse-proxy gateway.
- **What Argus Is Not:** It is not an AI model, not an autonomous end-to-end coding agent, not a general-purpose chat interface, not a cloud SaaS platform, and not a host-level security sandbox.

---

## 3. Capability Inventory: Implemented vs. Experimental vs. Deferred vs. Unsupported

### A. Implemented (Core & Production-Ready for Local Development)
1. **Multi-Tier Patch Reconciliation (Tiers 1–3, Tier 5):**
   - **Tier 1 (Exact Matching):** Byte-for-byte exact character replacement.
   - **Tier 2 (Whitespace/Indentation Normalization):** Reconciles leading indentation drift and trailing whitespace differences.
   - **Tier 3 (Fuzzy Sliding-Window Matching):** Sliding-window character similarity with threshold (0.82) and ambiguity margin enforcement (0.10).
   - **Tier 5 (Safe Refusal):** Terminal safe refusal when matches are ambiguous or similarity is below threshold.
   - **AST Syntax Gate:** Mandatory Abstract Syntax Tree validation (`ast.parse`) for Python source before accepting any match.
2. **Ephemeral Workspace Lifecycle:**
   - Isolated copy mode (`WorkspaceManager(mode="copy")`) in `%TEMP%/argus_ws_*` excluding heavy directories (`.git`, `node_modules`, `venv`, `build`).
   - Git worktree mode (`WorkspaceManager(mode="worktree")`) with detached HEAD and uncommitted file overlay.
   - Session-aware garbage collection validating host PID and process start-time (`kernel32.GetProcessTimes` on Windows, `/proc/{pid}/stat` on POSIX) to protect active runs against PID reuse.
   - Atomic directory rename claiming to prevent concurrent garbage collector collisions.
3. **Bounded Subprocess Execution:**
   - `SubprocessTestRunner` with direct invocation (`shell=False`).
   - Strict command tokenizer rejecting shell chaining operators (`&&`, `||`, `;`, `|`), redirection, and backgrounding.
   - Bounded output collection (`BoundedOutputCollector`) draining streams concurrently with 4MB memory bounds, tested under 1 GB logical stream flood.
   - Process-tree hard termination (`taskkill /F /T` on Windows, `os.killpg` on POSIX) on timeout.
4. **Authoritative Candidate Application & TOCTOU Defense:**
   - Candidate artifacts written to `.argus/artifacts/<run_id>.json` with baseline and candidate SHA-256 digests.
   - `AuthoritativePatchApplier` checks baseline file hashes before applying updates.
   - Atomic file commit via temporary sibling files and `os.replace`.
5. **Developer Interfaces:**
   - Command-Line Interface (`argus run`, `argus recover`, `argus candidates`, `argus diff`, `argus apply`).
   - Interactive Textual TUI cockpit (`argus tui`) with command palette, timeline, and diff viewer.
   - FastMCP stdio server (`argus mcp`) exposing tool definitions for upstream agents.
6. **Authorized Provider Adapters:**
   - OpenAI-compatible HTTP adapter with connection pooling and explicit lifecycle close.
   - Ollama local runtime adapter.
   - Antigravity Host Adapter (`agy` CLI integration).
   - Automatic secret scrubbing from subprocess environments and error messages.

### B. Experimental (Feature-Complete but Subject to Evolution)
1. **Tier 4 Anchor-Based Matching:**
   - Prefix/suffix anchor matching across drifted interior code lines.
   - **Status:** Implemented but disabled by default; requires explicit opt-in via `--enable-tier4`.
2. **Local Inference Gateway Proxy (`argus gateway` / `argus serve`):**
   - OpenAI- and Anthropic-compatible reverse proxy listening on `127.0.0.1:8000`.
   - Mandatory constant-time bearer authentication (`ARGUS_GATEWAY_API_KEY`).
   - Loopback CORS and Host header validation against DNS rebinding.
   - Identity-based rate limiting keying by token hash and client ID.
   - Two-phase dynamic budget reservation using integer micro-dollars.
   - In-memory streaming patch healer.
   - **Status:** Experimental auxiliary tool; separate from the core offline recovery loop.
3. **Containerized Repository Evaluator (`ContainerEvaluator`):**
   - Docker/Podman container lifecycle harness passing `--network=none`, CPU/memory bounds, and in-memory tar-stream candidate transfer.
   - **Status:** Implemented; fails closed when container engine is unresponsive. Evaluated separately from host execution.

### C. Deferred (Planned for Future Releases)
1. **Multi-Language AST Validation:** Tree-sitter parsers for TypeScript, JavaScript, Rust, and Go. (Currently restricted to Python).
2. **Transactional Multi-File Patches:** Multi-file atomic patch application across multiple target files. (Currently restricted to single-file targets).
3. **Hardware-Level MicroVM Isolation:** Integration with microVM hypervisors (Firecracker, Cloud Hypervisor) for untrusted code execution.

### D. Unsupported (Explicitly Excluded / Prohibited)
1. **Groq Inference Provider:** Permanently prohibited and blocked across all configurations, adapters, routing logic, and storage engines.
2. **Unauthenticated Network Exposure:** Starting the Gateway on non-loopback addresses (`0.0.0.0`) without `--allow-remote` and `ARGUS_GATEWAY_API_KEY` is strictly prohibited and refused on startup.
3. **Silent Patch Application:** Automatic working tree modification without verified test passage and human approval.
4. **Universal Correctness Claims:** Passing test suites proves adherence to test assertions; it does not guarantee universal software correctness.

---

## 4. Operational Invariants

1. **Working Tree Invariance:** The user's active git working tree is sacred. Iterative candidate attempts run strictly in isolated temporary directories.
2. **Fail-Closed Execution:** If requested isolation (container/sandbox) or authentication cannot be verified, execution halts immediately with an explicit refusal code.
3. **Bounded Computation:** Every recovery attempt has a hard turn limit (default 3), command timeout (default 30s), output buffer cap (default 4MB), and monetary reservation ceiling.
