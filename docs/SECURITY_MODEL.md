# Argus Security Model and Threat Analysis

**Scope:** recovery runtime, temporary workspaces, subprocess execution, candidate artifacts, MCP server and the experimental gateway. The short policy and reporting process are in [SECURITY.md](../SECURITY.md). This page is the longer threat analysis. It is the project's own description of intended boundaries, not an independent audit.

---

## 1. Two modes

1. **In-process recovery (CLI, TUI, MCP, Python API).** No listening socket. The one network egress is to the provider you configured.
2. **Experimental gateway (`argus gateway`).** A loopback HTTP proxy for developer tools. Optional (`gateway` extra) and outside the recovery loop.

## 2. Threat matrix

| ID | Threat | Defense | Residual risk |
| :--- | :--- | :--- | :--- |
| T-01 | Browser CSRF against the gateway | Loopback-only CORS regex; bearer auth required | Misconfigured `--allow-remote` |
| T-02 | DNS rebinding against the gateway | `Host` header must be loopback | none known |
| T-03 | Path traversal in patches | Reject absolute paths, `..`, NUL; single-file scope; target must stay inside the workspace | none known |
| T-04 | Runaway or forking test | Timeout; whole process tree killed | Escapes via detached daemons are not tracked |
| T-05 | Output flood | Concurrent draining, 4 MB bound, head/tail kept | none known |
| T-06 | Stale overwrite (TOCTOU) | Baseline SHA-256 at apply; atomic replace; per-run apply lock | none known |
| T-07 | Secret leakage | `scrub_secrets` on logs/artifacts, `scrub_prompt` on model input, subprocess env scrubbed, keys excluded from `repr` | Secrets inside source files are only scrubbed if token-shaped |
| T-08 | Host privilege (malicious test or repository) | **No technical defense.** Documented; warning shown before `argus run` | Full: code runs as you |
| T-09 | Unintended provider selection | Only a provider you configured or named is called; no key alone selects a provider; no hidden fallback | none known |
| T-10 | Forged approval / swapped patch (by file edits) | HMAC-signed metadata (key in user config dir; empty/short key files rejected and regenerated; `ARGUS_CONFIG_DIR` hidden from the test environment), candidate and baseline hash checks, strict run-ID validation | **Code running as your OS user can read the key and forge a valid approved run, including the test command and an MCP-supplied `test_command` (T-08).** Signing is not a boundary against host-level code |
| T-11 | Prompt-injection through the MCP caller | Workspace confined to server root; budget capped; no approval tool | The agent can still burn the capped budget |
| T-12 | Hidden provider/model substitution | Only the configured provider; a named model is never replaced; unknown prices refused | none known |
| T-13 | Untrusted repository raising its own limits | Project policy can only tighten limits and cannot choose model or provider | none known |
| T-14 | Launcher command injection | Windows console fallback uses an argv list, `shell=False` | none known |

## 3. Details

### Patch path and scope (T-03)
`validate_patch_blocks` normalizes diff prefixes and rejects absolute paths, parent traversal, null paths and invalid characters. Multi-file patches are refused. A target outside an explicitly supplied workspace root raises `TargetOutsideWorkspaceError` before any copy. Symlinks that leave the project are not followed into the copy.

### Process supervision (T-04, T-05)
`SubprocessTestRunner` starts the command without a shell and rejects shell operators. On timeout it terminates the process tree (`taskkill /F /T` on Windows, `os.killpg` on POSIX). The executable allow-list is a guard rail: any allowed interpreter can run arbitrary code, so it is not a boundary.

### Candidate integrity and approval (T-06, T-10)
Each run is stored in `.argus/runs/<run_id>/`. `metadata.json` is signed (`metadata.sig`, HMAC-SHA256). The key (`artifact_signing.key`) lives in the Argus configuration directory (`ARGUS_CONFIG_DIR` or the platform default) with mode 0600 on POSIX, and Argus refuses a key location inside the workspace. `load_candidate`, `set_approval`, `list_candidates` and `apply` verify the signature; unsigned or edited metadata gives `CANDIDATE_SIGNATURE_INVALID`, a malformed ID gives `INVALID_RUN_ID`. Apply also re-computes the candidate hash and compares the live file with `baseline_hash`. Runs created before signing existed are unlisted and cannot be applied; re-run recovery.

### Secrets (T-07)
Logs and stored artifacts pass `scrub_secrets` (broad patterns, including context patterns such as `api_key=...`). Text sent to the model passes `scrub_prompt`, which removes only exact known keys, token-shaped secrets and PEM blocks, so source such as `password = "x"` is not rewritten (that would change the code the model sees). `prompt.txt` and `raw_response.txt` persist scrubbed source context under `.argus/` for every run, including failed ones.

### Host execution (T-08)
Test commands run on the host with your permissions. The temporary copy protects your working tree from half-applied edits and nothing else. For untrusted code use a container or VM yourself. The library's `ContainerEvaluator` (`--network=none`, memory/CPU/PID limits, tar-over-stdin transfer) is opt-in, not exposed as a CLI flag, shares the host kernel and has not been independently audited.

### Provider policy (T-09, T-12, T-13)
Only the configured provider is called (Groq is a normal key-based preset). A pinned model is the only model sent. Cheap-first escalation applies only when no model is named. A model with no price is refused unless local. `argus.yaml` / `[tool.argus]` in the workspace root may lower `max_attempts`, `budget_limit_usd` and `test_timeout_seconds`, disable escalation and set `reasoning_effort: low` if unset (higher values are ignored); its `model`, `provider`, `small_model` and `large_model` keys are ignored with a note.

### MCP (T-11)
`workspace` must resolve inside the server root; `budget` is finite, positive and capped (default `$0.25`, `ARGUS_MCP_MAX_BUDGET_USD`). `argus_status` masks keys. There is no approval tool.

## 4. Gateway

- `ARGUS_GATEWAY_API_KEY` is required; compare is `hmac.compare_digest`. No `PYTEST_CURRENT_TEST` or `ARGUS_TEST_MODE` bypass exists in production code.
- Binds to `127.0.0.1` by default; `--allow-remote` is explicit and warns.
- Loopback CORS regex `^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$`; `Host` allow-list (`127.0.0.1`, `localhost`, `::1`).
- Upstream URL validation blocks private/link-local/metadata addresses (including IPv4-mapped IPv6).
- Rate limiting by token hash plus client identifier; the run-ID header is validated, never taken from an API key header; budget reservations expire (15-minute TTL). Money is integer micro-dollars. Unknown models are priced fail-closed.
- Limitation: client-identifier rotation can fragment rate-limit buckets.

## 5. What Argus does not claim

- Not a sandbox; not a malware scanner.
- The syntax gate (`ast.parse` + `compile`) does not establish correctness. A valid patch can weaken a check or special-case a test.
- A passing test is test-relative evidence. Independent grading of one model family found 8.3-12.0% of target-test passes broke other tests ([RESEARCH_STATUS.md](RESEARCH_STATUS.md)).
