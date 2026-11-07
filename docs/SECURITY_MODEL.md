# Argus Security Architecture & Detailed Threat Model

**Classification:** High-Integrity Local Developer Runtime  
**Auditor:** Principal Software Architect & Security Reviewer  
**Scope:** Core Recovery Runtime, Ephemeral Workspaces, Subprocess Execution, Patch Application, Artifact Provenance, FastMCP, and Experimental Gateway Proxy

---

## 1. Threat Landscape & Boundary Analysis

Argus operates across two distinct modes of execution:
1. **Core In-Process Recovery (CLI & TUI):** Operates entirely in the local terminal process without exposing network listening sockets.
2. **Experimental Auxiliary Gateway (`argus gateway`):** Exposes an HTTP reverse proxy (`127.0.0.1:8000`) for upstream developer tools (Continue, Cursor, Open-WebUI).

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                THREAT MATRIX & DEFENSES                                │
├──────┬─────────────────────────────────────┬───────────────────┬───────────────────────┤
│ ID   │ Threat Scenario                     │ Inherent Severity │ Implemented Defense   │
├──────┼─────────────────────────────────────┼───────────────────┼───────────────────────┤
│ T-01 │ Malicious Browser CSRF / Localhost  │ CRITICAL          │ Loopback CORS regex   │
│ T-02 │ DNS Rebinding against Gateway       │ CRITICAL          │ Strict Host check     │
│ T-03 │ Directory Traversal in Patches      │ HIGH              │ validate_patch_blocks │
│ T-04 │ Runaway Subprocess Tree / Fork Bomb │ HIGH              │ Process-tree kill     │
│ T-05 │ Subprocess Output Memory Exhaustion │ HIGH              │ BoundedOutputCollector│
│ T-06 │ Stale Patch Race Condition (TOCTOU) │ MEDIUM            │ SHA-256 validation    │
│ T-07 │ Secret Exfiltration in Tracebacks   │ HIGH              │ scrub_secrets filter  │
│ T-08 │ Host Execution Privilege Escalation │ HIGH              │ Explicit warning/docs │
│ T-09 │ Prohibited Provider (Groq) Leak     │ MEDIUM            │ Fail-closed rejection │
└──────┴─────────────────────────────────────┴───────────────────┴───────────────────────┘
```

---

## 2. In-Depth Attack Vectors & Defenses

### Threat T-03: Directory Traversal Path Attacks
- **Vector:** An LLM or malicious patch contains file paths like `../../../../Windows/System32/drivers/etc/hosts`, `/etc/passwd`, or null bytes `a/\x00mod.py`.
- **Argus Defense:**
  1. [`validate_patch_blocks()`](file:///c:/coding/Argus/argus/patch/paths.py) normalizes diff prefixes (`a/`, `b/`) and strictly rejects absolute paths (`/etc/passwd`, `C:\Windows`), relative parent traversal (`../`), null paths (`/dev/null`), and invalid characters.
  2. Multi-file patches targeting multiple files are rejected to prevent cross-file side effects.
  3. Symlinks pointing outside the workspace boundary are explicitly resolved and rejected.

### Threat T-04 & T-05: Subprocess Fork Bombs, Infinite Loops & Memory Exhaustion
- **Vector:** A test command executes an infinite loop (`while True: pass`) or floods stdout/stderr with gigabytes of data to crash the parent runtime via Out-of-Memory (OOM).
- **Argus Defense:**
  1. **Bounded Timeout:** Configurable execution deadline (default 30 seconds).
  2. **Process-Tree Hard Termination:** On timeout, [`SubprocessTestRunner._kill_process_tree()`](file:///c:/coding/Argus/argus/runtime/runner.py#L455) issues `taskkill /F /T /PID` (Windows) or `os.killpg(pid, signal.SIGKILL)` (POSIX), terminating the parent process and all child/grandchild worker processes.
  3. **Bounded Memory Output Collector:** [`BoundedOutputCollector`](file:///c:/coding/Argus/argus/runtime/runner.py#L32) drains stdout and stderr in 64KB chunks concurrently in background threads. Output buffers are strictly bounded to 4MB, retaining head and tail logs while omitting intermediate volume with an explicit truncation marker.

### Threat T-06: Stale Patch Race Condition (TOCTOU)
- **Vector:** A developer or agent edits a file concurrently while a 20-second recovery loop is running. Applying the generated candidate could silently overwrite newer manual edits.
- **Argus Defense:**
  1. **SHA-256 Baseline Precondition:** When recovery begins, [`WorkspaceManager`](file:///c:/coding/Argus/argus/runtime/workspace.py) records `baseline_sha256`.
  2. [`AuthoritativePatchApplier`](file:///c:/coding/Argus/argus/patch/applier.py) re-hashes the live file before applying. If the hash does not match `baseline_sha256`, the operation aborts immediately without touching the file.
  3. **Atomic Replacement:** Updates are staged to `.<filename>.tmp.<uuid>` and committed via atomic `os.replace`.

### Threat T-07: Secret & Credential Protection
- **Vector:** Provider API keys or tokens are passed into child subprocesses, printed in error tracebacks, or leaked in artifacts.
- **Argus Defense:**
  1. **Environment Scrubbing:** Subprocess test execution explicitly strips environment variables matching patterns (`KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `AUTH`, `CREDENTIAL`).
  2. **Traceback & Error Redaction:** [`scrub_secrets()`](file:///c:/coding/Argus/argus/utils/redaction.py) scrubs regex patterns matching API keys (OpenAI `sk-*`, Anthropic `sk-ant-*`, GitHub tokens, AWS keys) before logging or presenting to the user.
  3. **No Unauthenticated Provider Persistence:** Credentials in `ProviderStorage` are isolated per provider with restrictive file permissions.

### Threat T-08: Host Execution Privilege Implications
- **Vector:** Executing untrusted test suites that attempt to delete files or run host-level malware.
- **Argus Reality:**
  - `SubprocessTestRunner` executes directly on the host using the local developer's user privileges.
  - Workspace copying (`WorkspaceManager(mode="copy")` or `mode="worktree"`) protects active Git files from accidental pollution, but **does not isolate system resources, networks, or host filesystems**.
  - Argus never misrepresents host subprocess execution as containerization or sandboxing.
  - When container isolation is requested (`--container`), Argus fails closed (exit code 125) if Docker is unavailable, rather than silently falling back to the host.

### Threat T-09: Prohibited Provider Enforcement (Groq)
- **Vector:** Accidental or malicious configuration of Groq provider keys or endpoints.
- **Argus Defense:**
  - `argus provider set groq ...` is rejected on CLI.
  - Gateway middleware immediately rejects `x-upstream-provider: groq` or `Bearer gsk_*` with HTTP 400 (`prohibited_provider`).
  - SSRF validator in Gateway blocks all `api.groq.com` and `groq.com` destinations.

---

## 3. Gateway Threat Model & Defenses (Experimental Reverse Proxy)

When the optional Gateway proxy is running:
- **T-01 (CORS):** Browser cross-origin requests are restricted to `^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$`. Foreign browser origins receive HTTP 403.
- **T-02 (DNS Rebinding):** HTTP middleware inspects the `Host` header. Hostnames outside `{127.0.0.1, localhost, ::1}` are rejected with HTTP 400. `testserver` is admitted strictly when running under test suites.
- **Mandatory Authentication:** All sensitive and inference endpoints (`/v1/chat/completions`, `/v1/messages`, `/v1/models`) require a valid Bearer token verified via timing-safe `hmac.compare_digest`.
- **Identity-Based Rate Limiting:** Sliding-window rate limiting keys by token hash and client identifier, ensuring multiple local tools on `127.0.0.1` have independent allocations.
- **Monetary Budget Guard:** Dynamic reservations use integer micro-dollars ($1\text{ USD} = 1{,}000{,}000\ \mu\text{USD}$) to eliminate IEEE 754 floating-point drift and enforce hard budget ceilings under multi-threaded concurrency.
