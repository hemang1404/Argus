# Security Policy

Argus takes security and repository integrity seriously. This document outlines our threat model, trust boundaries, execution safety guarantees, known limitations, and vulnerability reporting procedures.

---

## 1. Primary Security Assets

Argus is designed to protect three primary assets:
1. **Developer Workstation & Host Integrity:** Preventing arbitrary command execution, system compromise, and resource exhaustion.
2. **Source Code & Git Working Tree:** Preventing destructive file modifications, accidental Git working tree contamination, and stale file overwrites.
3. **Secrets & Provider Credentials:** Preventing the exfiltration, persistence, or logging of API keys, tokens, and private authentication materials.

---

## 2. Threat Model & Trust Boundaries

Argus operates across four distinct trust boundaries:

```
[Untrusted: LLM Output & Repositories] ──► [Boundary 1: Sanitization & AST Validation]
                                                     │
                                                     ▼
                                        [Boundary 2: Ephemeral Workspace]
                                                     │
                                                     ▼
                                      [Boundary 3: Subprocess Supervision]
                                                     │ (Tests Pass)
                                                     ▼
                                        [Boundary 4: SHA-256 Artifacts]
                                                     │ (Explicit Approval)
                                                     ▼
                                            [Developer Repository]
```

### Threat Actors
- **Compromised / Hallucinating LLM:** Models generating syntactically corrupt code, malicious shell payload injections, path traversal attempts, or destructive modifications.
- **Malicious Upstream Repository / Test Suites:** Repositories designed to abuse test commands (e.g. `pytest`) to execute malicious host payloads during evaluation.
- **Untrusted Local Network Attackers:** Malicious web pages or local network peers attempting DNS rebinding or cross-origin requests against local Gateway endpoints.

### Attack Surfaces & Mitigations

| Attack Surface | Threat Vector | Mitigation in Argus |
|---|---|---|
| **Test Command Execution** | Shell injection (`cmd && rm -rf /`) | Direct subprocess invocation with `shell=False`. Strict tokenization rejecting shell operators (`&&`, `||`, `;`, `|`, `>`, `<`). |
| **Test Output Flood** | Memory exhaustion (DoS via stdout flood) | `BoundedOutputCollector` continuously drains pipes with 4MB memory buffer; verified under 1 GB output flood. |
| **Runaway Worker Processes** | CPU / Thread starvation on timeout | Hard process-tree killing via `taskkill /F /T` (Windows) and `os.killpg` (POSIX) terminating parent and all descendants. |
| **Path Traversal in Patches** | Overwriting system files (`/etc/passwd`, `C:\Windows`) | Absolute paths, directory traversal (`../`), null bytes, and non-canonical paths are strictly rejected in `validate_patch_blocks`. |
| **Concurrent File Modification** | Time-of-check to time-of-use (TOCTOU) file race | Baseline SHA-256 hash verified before patch application; atomic replacement via `os.replace`. |
| **Secret Exfiltration** | Leaking API keys in error logs or test runs | `scrub_secrets` pattern redaction across environments, stdout/stderr, and traceback outputs. |
| **Gateway DNS Rebinding** | Malicious websites querying `http://127.0.0.1:8000` | Loopback CORS enforcement (`allow_origin_regex`), Host header validation (rejecting foreign hosts), and mandatory Bearer token authentication. |

---

## 3. Host Privilege Implications & Execution Honesty

> [!WARNING]
> **Host Execution Runs With Local User Privileges:**
> When running `SubprocessTestRunner` on the host, test commands execute with your **current developer user privileges**.
> 
> Workspace copying (`WorkspaceManager(mode="copy")` or `mode="worktree"`) isolates repository files against accidental Git contamination, but **does not constitute an operating system security sandbox**. If a test script executes malicious code, it will execute on the host with your privileges.
> 
> For untrusted repositories, use containerized execution (`ContainerEvaluator`) with Docker or Podman.

### Container Limitations
Docker/Podman container isolation (`ContainerEvaluator`):
- Provides process namespace, filesystem, and network isolation (`--network=none`).
- Enforces strict memory (`--memory`), CPU (`--cpus`), and PID limits (`--pids-limit`).
- Transfers candidate patches exclusively via in-memory tar stream over stdin (zero host directory mounts).
- **Limitation:** Standard container execution shares the host OS kernel and does not provide hardware-isolated microVM security (e.g. Firecracker/gVisor).

---

## 4. Gateway Authentication & Isolation (Experimental)

The Argus Gateway proxy (`argus gateway`) enforces defense-in-depth:
- **Mandatory Authentication:** Requires `ARGUS_GATEWAY_API_KEY`. Verification uses constant-time comparison (`hmac.compare_digest`) to prevent timing attacks.
- **Fail-Closed Startup:** Starts in unauthenticated mode only when explicitly running under unit test runners. CLI and ASGI lifespan refuse execution if unconfigured.
- **Loopback Enforcement:** Binds strictly to `127.0.0.1` by default. Binding to non-loopback interfaces (`0.0.0.0`, `::`) requires explicit `--allow-remote` and displays a security warning.
- **Rate Limiting:** Sliding-window rate limiter keyed by truncated SHA-256 token hash and client identifier. Local developer tools on `127.0.0.1` have independent buckets without cross-client starvation.

---

## 5. Candidate Integrity & Approval Semantics

- **No Unsolicited Modifications:** Argus never overwrites user code without verified test passage and explicit developer approval (`argus apply <run_id>`).
- **Cryptographic Provenance:** Candidate artifacts in `.argus/artifacts/<run_id>.json` record:
  - `baseline_sha256`: SHA-256 digest of original target file before modification.
  - `candidate_sha256`: SHA-256 digest of verified candidate file.
  - `run_id`: Unique execution identifier.
  - Test command, timestamp, duration, and token telemetry.
- **Atomic Commits:** File updates are written to a temporary sibling file (`.<filename>.tmp.<uuid>`) and committed atomically via `os.replace`.

---

## 6. Known Security Limitations

1. **Python-Only AST Verification:** AST syntax verification is currently implemented for Python (`ast.parse`). Non-Python source files cannot be AST-verified.
2. **Single-File Scope:** Multi-file atomic patch transactions are unsupported. Multi-file patches are safely refused.
3. **Host Privilege Boundary:** As noted above, host execution does not isolate against malicious test suites that execute destructive system commands.
4. **Client-ID Rotation:** In the experimental Gateway, rate limiting on loopback combines the token hash with client identifier headers; deliberate header rotation within a valid token allocation could fragment client rate-limiting state.

---

## 7. Reporting a Vulnerability

If you discover a security vulnerability in Argus, please report it responsibly:

- **Do NOT create a public GitHub issue.**
- Email details to: `security@argus-recovery.org` (or contact the maintainers privately via GitHub Security Advisories).
- Please include:
  - Description of the vulnerability.
  - Reproduction steps or proof of concept.
  - Affected components, platforms, and versions.
- We will acknowledge receipt within 48 hours and coordinate remediation before public disclosure.
