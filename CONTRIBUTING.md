# Contributing to Argus

Thank you for contributing to Argus. As an open-source code recovery runtime, we emphasize engineering correctness, deterministic reproducibility, security boundaries, and reliable test verification.

---

## 1. Development Environment Setup

### Prerequisites
- Python 3.11, 3.12, 3.13, or 3.14.
- Git.
- Optional: Docker / Podman (only for the library-level container evaluator; the offline test suite does not need it).
- No network, API key or model access is needed to run the tests.

### Local Workspace Setup

```bash
# 1. Clone the repository
git clone https://github.com/hemang1404/Argus.git
cd Argus

# 2. Create and activate a dedicated virtual environment
python -m venv .venv
# On Linux / macOS:
source .venv/bin/activate
# On Windows (PowerShell):
.venv\Scripts\Activate.ps1

# 3. Install Argus in editable mode with development dependencies
pip install -e ".[dev,tui,gateway]"
```

---

## 2. Testing & Quality Gates

Every contribution must pass the offline regression test suite before submission. `tests/test_real_host_integration.py` is excluded on purpose: it calls a locally installed Antigravity CLI and makes real, possibly billable, inference calls. Tests must not touch the real OS keyring, your real `~/.argus` / `ARGUS_CONFIG_DIR`, or write into the repository root; use `tmp_path` and the fixtures in `tests/conftest.py`.

```bash
# 1. Run the full offline test suite (no network, no model calls)
ARGUS_OFFLINE=1 pytest -q -m "not live_provider and not live_antigravity and not real_host and not tokenizer_download"

# 2. Focused gates: runtime/workspace safety, gateway security, patch engine, signed candidates
pytest tests/test_runtime_and_workspace_safety.py tests/test_gateway_security.py        tests/test_patch_remediation.py tests/test_candidate_signing.py

# 3. The 500-case synthetic adversarial patch corpus
pytest -v tests/test_patch_adversarial_corpus.py

# 4. Lint and compile check
ruff check .
python -m compileall -q argus tests
```

---

## 3. Engineering Guidelines & Coding Standards

1. **Correctness over Complexity:**
   - Prefer small, focused functions with explicit error handling.
   - Avoid broad `except Exception:` blocks; always catch specific exceptions or propagate failure states truthfully.
   - Do not silently fall back to less secure execution paths (e.g., container requests must never silently execute on the host).
2. **Type Hints & Documentation:**
   - Use standard Python type annotations (`typing`, `from __future__ import annotations`).
   - Every module must have a clear module-level docstring explaining its architectural responsibility.
3. **No Unsolicited Side Effects:**
   - The developer's live working tree must remain untouched until a candidate is explicitly approved and applied via `AuthoritativePatchApplier`.
   - Never write to `.git/`. Recovery runs in a temporary copy, not a worktree.
   - Never choose a provider or model for the user: only a configured provider is called, a named model is never substituted, and an unpriced model is refused.
4. **Security & Secrets:**
   - Never commit API keys, tokens, or private credentials.
   - Scrub sensitive environment variables and tokens before executing child subprocesses.
   - Provider keys (including `GROQ_API_KEY`) are never committed, logged or passed to test subprocesses.

---

## 4. Branching & Commit Conventions

### Branch Strategy
- `main`: Stable releases.
- `dev`: Primary active integration branch. Feature branches branch from and merge into `dev`.
- Feature branches: `feat/<name>`, `fix/<name>`, `docs/<name>`, `test/<name>`.

### Commit Messages
We follow the Conventional Commits specification:
```
feat(patch): add whitespace normalization for Tier 2 matching
fix(workspace): resolve PID reuse edge case with start-time verification
test(gateway): add multi-client rate limiting isolation test
docs(readme): clarify host execution privilege boundaries
```

---

## 5. Pull Request Review Criteria

Every pull request is reviewed against the core Argus review priorities:
1. **Does the change do what it claims?**
2. **Can it fail in realistic edge cases (timeouts, malformed inputs, process crashes)?**
3. **Does it introduce any regression in existing recovery behaviors?**
4. **Is failure and recovery handled correctly (fail-closed, bounded outputs, tree killing)?**
5. **Are relevant automated tests present?**
6. **Does it affect host security or credential confidentiality?**
7. **Does it alter matching tier definitions or baseline experimental conditions?** Patch-engine changes need regression tests for the refusal cases (ambiguity, near-miss tokens, line endings), not only the success cases.
8. **Do the docs match?** Update README, website pages and CHANGELOG for any CLI flag, default or behaviour you change. Claims about research results need an evidence pointer in `docs/RESEARCH_STATUS.md`.

---

## 6. Code of Conduct

All contributors and maintainers are expected to adhere to our [Code of Conduct](CODE_OF_CONDUCT.md).
