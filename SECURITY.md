# Security Policy

This document states what Argus does and does not protect, its trust boundaries, known limitations, and how to report a vulnerability. Read the guarantees literally: they are narrower than a sandbox.

---

## 1. Assets

1. **Developer workstation and host integrity.**
2. **Source code and the Git working tree**: no destructive or stale writes.
3. **Secrets and provider credentials**: no exfiltration, persistence in artifacts, or logging.

---

## 2. Host execution is not sandboxed

> [!WARNING]
> **Argus runs your test command, including any model-generated code it exercises, directly on your machine with your user permissions, network access and files.** `argus run` prints this warning before starting; MCP responses carry it too.
>
> The temporary workspace is a **copy** of your project. It protects your working tree from half-applied edits. It does not isolate processes, the network or the rest of your filesystem. A malicious repository or test can read your files, use your credentials in the environment and delete anything your account can.
>
> Only run Argus on repositories you trust. For untrusted code use a container, VM or separate machine you control.

Consequences worth stating explicitly:

- The **signing key** for candidate approvals (below) lives in your user configuration directory. Code running as you, which includes the tests Argus runs, can read it. Signing stops accidental or file-level forgery by an agent that can only edit files in the workspace; it does not defend against code that executes on the host.
- The command runner rejects shell operators and runs without a shell (`shell=False`). That prevents naive injection. The executable allow-list in `runner.py` is a guard rail, not a security boundary: an allowed interpreter can run arbitrary code.
- Container evaluation exists in the library (`use_container_runner`, `ContainerEvaluator`, `--network=none`, resource limits, tar-over-stdin transfer) but is **opt-in, not exposed as a CLI flag, and not independently audited**. It shares the host kernel. Do not treat it as a validated boundary.

---

## 3. Trust boundaries

```
[Untrusted: model output, repositories, test suites]
        │
        ▼
[1: parse + reconcile + syntax gate (ast.parse + compile)]
        │
        ▼
[2: temporary workspace copy (not an OS sandbox)]
        │
        ▼
[3: supervised subprocess (bounded output, timeout, process-tree kill)]
        │  tests pass
        ▼
[4: signed candidate artifact (SHA-256 hashes + HMAC)]
        │  explicit human approval
        ▼
[your repository]
```

### Threat actors

- A model that emits malformed, destructive or misleading edits.
- A repository or test suite that tries to abuse the test command.
- An agent talking to the MCP server (an LLM; its arguments are untrusted).
- A web page or local peer attacking the optional gateway (CSRF, DNS rebinding).

### Mitigations

| Surface | Threat | Mitigation |
| :--- | :--- | :--- |
| Test command | Shell injection | No shell; `&&`, `\|\|`, `;`, `\|`, `>`, `<`, `&` rejected |
| Test output | Memory exhaustion | `BoundedOutputCollector` drains concurrently, 4 MB bound, head and tail kept |
| Runaway processes | Hang or fork storm | Timeout kills the whole process tree (`taskkill /F /T`, `os.killpg`) |
| Patch paths | Traversal / writes outside the workspace | Absolute paths, `..`, NUL and symlinks leaving the workspace rejected; single-file scope |
| Patch application | Wrong-location or ambiguous edit | Refuse on ambiguity; Tier 3 is token-equal only; syntax gate on the final state; tests decide correctness |
| Stale or concurrent edits | Overwriting newer user changes | Baseline SHA-256 checked at apply; atomic replace; per-run apply lock |
| Candidate forgery | Agent or file edit flips `approved` / swaps the patch | `metadata.json` is HMAC-signed (`metadata.sig`); candidate and baseline hashes re-checked; run IDs validated against a strict pattern (no traversal) |
| Secrets | Keys in logs, artifacts, prompts | `scrub_secrets` on logs/artifacts; `scrub_prompt` (known keys, token-shaped secrets, PEM blocks) on text sent to the model; subprocess environment scrubbed |
| Spend | Runaway cost | Per-run hard budget, reservation before each call, unknown model prices refused, non-finite or negative budgets rejected |
| Provider choice | Hidden fallback or silent model change | Only the configured provider is used; a pinned model is never substituted; absent provider = `CONFIG_ERROR` |
| Repository policy | Project file raising limits or choosing a model | `argus.yaml` / `[tool.argus]` in the workspace root may only tighten limits |
| Install of MCP config | Overwriting user config | `argus mcp install` writes a timestamped backup before modifying an existing file |

Candidate artifacts written before the signing change have no signature and are treated as untrusted: re-run recovery to create a new candidate.

---

## 4. MCP server

The MCP caller is an LLM agent, so its arguments are untrusted:

- `workspace` must be inside the server root (the directory it was started in, or `ARGUS_MCP_WORKSPACE_ROOT`).
- `budget` must be finite and positive, and is capped at `$0.25` unless `ARGUS_MCP_MAX_BUDGET_USD` raises the ceiling.
- There is no tool to approve a candidate. Approval is a human CLI/TUI action, recorded in a signed file.
- Status output masks keys and never returns secret values.

---

## 5. Gateway (experimental)

- Requires `ARGUS_GATEWAY_API_KEY`; comparison is constant-time. The server refuses to start without a key. There are no environment-variable test bypasses in production code.
- Binds to loopback by default; non-loopback needs `--allow-remote` and prints a warning.
- Loopback CORS regex, `Host` header validation, upstream URL validation (blocks private ranges and cloud metadata addresses, and Groq).
- Rate limiting keys on a token hash and a client identifier; the run ID header is validated and never taken from an API key header. Budget reservations expire after a TTL.
- Unknown models are priced fail-closed.
- Known limitation: rotating the client identifier within a valid token can fragment rate-limit buckets.

---

## 6. Candidate integrity and approval

- Artifacts live in `.argus/runs/<run_id>/` (`metadata.json`, `metadata.sig`, `candidate.patched`, `patch.diff`, `prompt.txt`, `raw_response.txt`, `attempts.json`, `context_window.json`). `.argus/.gitignore` ignores them.
- `baseline_hash` is the SHA-256 of the target before modification and `candidate_hash` of the verified content. Apply verifies both, the approval flag, the signature, path containment and syntax, then replaces the file atomically (temp sibling + `os.replace`), preserving mode.
- Argus never writes your files without explicit approval. `--yes` and `--yes-reviewed` record the approval as **automation**, not human review. `--force` bypasses only the approval gate.
- `prompt.txt` and `raw_response.txt` hold scrubbed source context. They persist, including for failed runs. Delete old runs; do not commit them.

---

## 7. Known limitations

1. Python-only syntax gate; and it is syntactic, not semantic: a valid patch can still delete a security check or special-case a test.
2. Single-file scope; multi-file patches are refused.
3. Host execution with user privileges (section 2).
4. Test passage is test-relative. In independent grading, about 6.6-12.0% of target-test passes broke other tests, and about 18% of resolved patches were judged wrong or fragile by one reviewer (see [docs/RESEARCH_STATUS.md](docs/RESEARCH_STATUS.md)). Read the diff.
5. The copy workspace can include secret files that are in your working tree (for example an untracked `.env`). Use ignore rules or keep secrets outside the project.
6. Gateway rate-limit client-ID rotation (section 5).
7. `argus doctor` and `argus provider test` make live provider requests.

---

## 8. Reporting a vulnerability

- **Do not open a public issue.**
- Use GitHub Security Advisories on `hemang1404/Argus` ("Report a vulnerability" under the Security tab). A dedicated security mailbox is not currently published.
- Include a description, reproduction steps or proof of concept, and affected components, platforms and versions.
- We aim to acknowledge reports within 48 hours and to coordinate a fix before disclosure.
