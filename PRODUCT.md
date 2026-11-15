# Argus Product Specification & Capability Inventory

<!-- impeccable:product-schema 1 -->

## 1. Product Identity

- **Product Name:** Argus (local recovery runtime for model-generated code edits).
- **Category:** Developer tooling / reliability infrastructure for AI-assisted code changes.
- **Core Purpose:** Turn a failing test command into a reviewable, test-gated candidate patch using a model the user has configured, with bounded attempts and spend, tolerant-but-safe patch reconciliation, and approval-gated application.
- **Product principle:** The recovery runtime is the product. Argus is not a model router and does not pick providers for you.
- **Target audience:** Developers and agent builders who want model-proposed edits to fail safely: bounded, reviewable, never applied silently.

---

## 2. Positioning & Boundaries

- **What Argus is:** A local CLI runtime, a Textual TUI, an MCP stdio server for agents, an experimental loopback gateway, and a Python API, all over one controller.
- **What Argus is not:** an AI model, an autonomous end-to-end coding agent, a chat interface, a cloud service, an adaptive/learned inference router, or a security sandbox.
- **Authorized inference only:** Argus calls only a provider the user configured (keyring or environment). It has no Argus-owned keys, no hidden fallback and no silent provider or model switch. With no provider configured, commands stop with an actionable error. A model the user names is the model used.
- **Success levels are separate:** *applied* (edit located), *syntactic* (parses), *tests pass* (the one supplied command passed in the copy; shown as "verified"), *semantic* (a human judgement Argus does not make).

---

## 3. Capability Inventory

### A. Implemented

1. **Tiered patch reconciliation (Tiers 1-3, refusal):**
   - **Tier 1 exact:** verbatim match at a line or token boundary; never inside a longer identifier, comment or string.
   - **Tier 2 whitespace/indentation:** whole-line match after normalizing indentation and trailing whitespace.
   - **Tier 3 token-equal (default on, can be disabled):** a unique region whose identifier/operator/number/string tokens are identical to the search block; only whitespace and comments may differ. Near misses that change a token are refused.
   - **Refusal:** ambiguous or absent matches change nothing.
   - **Syntactic gate:** `ast.parse` + `compile` on the final state for `.py`/`.pyi`/`.pyw` targets. This is not a correctness check.
   - Line endings (LF/CRLF/CR/mixed), BOM and PEP 263 encodings are preserved; undecodable files are refused.
   - SEARCH/REPLACE parsing reports truncated or malformed blocks instead of applying a partial edit.
2. **Temporary workspace (copy):**
   - Each run works in a temporary copy of the project (`argus_ws_*` under the system temp directory). The default snapshot is the Git-tracked plus untracked-not-ignored files (working-tree content), or a full walk for non-Git directories; `.git`, `.argus`, caches and root-level virtualenv/`node_modules` are excluded. Size is bounded (default 2 GiB, fails fast).
   - Git worktree mode was removed; the copy is the only isolation mode.
   - Session-aware cleanup of leaked workspaces (PID plus process start-time).
3. **Bounded subprocess execution:** direct invocation (`shell=False`), strict command tokenizer, bounded output (4 MB), process-tree kill on timeout. **Runs on the host with the user's privileges; not sandboxed.**
4. **Signed candidates and authoritative apply:**
   - Run artifacts in `.argus/runs/<run_id>/` (`metadata.json`, `metadata.sig`, `candidate.patched`, `patch.diff`, `prompt.txt`, `raw_response.txt`, `attempts.json`, `context_window.json`).
   - Metadata is HMAC-signed with a key in the user config directory; baseline and candidate SHA-256 hashes are re-checked at apply; approval is recorded separately; apply is atomic and takes a per-run lock.
5. **Model dispatch and cost control:**
   - Pinned model = single model. Cheap-first cascade only when no model is named and the provider has a documented ladder (openai, deepseek, gemini, antigravity), attempts 1-2 small and attempt 3 large. Other providers: default model, no escalation.
   - Unknown model prices are refused unless the provider is local; budgets must be finite and positive; usage counts are validated.
   - Project policy file may only tighten limits.
6. **Interfaces:** CLI (`run`/`recover`, `candidates`, `inspect`, `diff`, `approve`, `reject`, `apply`, `provider`, `doctor`, `mcp`, `tui`, `setup`, `init`, `update`, `exec`), Textual TUI, FastMCP stdio server (no approval tool), Python API.
7. **Provider adapters:** OpenAI-compatible HTTP adapter (OpenAI, DeepSeek, Gemini, OpenRouter, Ollama, LM Studio, custom endpoints) and the Antigravity host CLI adapter. Anthropic models are reachable through OpenRouter or a custom endpoint; there is no native Anthropic preset.

### B. Experimental

1. **Tier 4 anchor matching:** off by default; `--enable-tier4`. No benefit has been measured (0 extra in a 26-task pilot and 0 discordant on 261 offline candidate pools; never measured on the 100-task design set).
2. **Local gateway (`argus gateway` / `argus serve`):** OpenAI/Anthropic-compatible loopback proxy. Needs the `gateway` extra and `ARGUS_GATEWAY_API_KEY`; budget reservation in integer micro-dollars; unknown models priced fail-closed. Outside the recovery loop.
3. **Container evaluator (`ContainerEvaluator`):** library-level only (not a CLI flag): Docker/Podman with `--network=none` and resource limits. Fails closed when requested but unavailable. Not independently audited.

### C. Deferred

1. Multi-language syntax validation (Tree-sitter): the gate is Python-only.
2. Transactional multi-file patches: single-file scope today.
3. MicroVM isolation for untrusted code.
4. A candidate clean/revert command (old runs must be deleted by hand).

### D. Explicitly out of scope

1. **Groq:** refused at configuration, CLI and gateway.
2. **Unauthenticated network exposure** of the gateway.
3. **Silent application:** no code path applies a candidate without verification and approval (`--yes`/`--yes-reviewed` record the approval as automation).
4. **Learned or "adaptive" routing:** no learned router is shipped or supported; the evidence did not justify one (see `docs/RESEARCH_STATUS.md`).
5. **Correctness claims:** passing tests proves the supplied command passed, nothing more.

---

## 4. Operational Invariants

1. **Working-tree invariance:** attempts run in the temporary copy; the live tree changes only through an approved, hash-checked apply.
2. **Fail closed:** missing provider, unpriced model, unavailable requested isolation, invalid signature or run ID, ambiguous match: stop with an explicit code.
3. **Bounded computation:** attempts (default 3), per-test timeout (30 s), task deadline (300 s), output buffer (4 MB), and a monetary ceiling (default `$0.03`) per run.
4. **Honest reporting:** distinguish applied / syntactic / tests pass / semantic; never describe a test pass as a fix.
