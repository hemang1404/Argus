# Contributing to Argus

Thank you for contributing to Argus. As an open-source code recovery runtime, we emphasize engineering correctness, deterministic reproducibility, security boundaries, and reliable test verification.

---

## 1. Development Environment Setup

### Prerequisites
- Python 3.11, 3.12, 3.13, or 3.14.
- Git.
- Optional: Docker / Podman (for container evaluation harness).

### Local Workspace Setup

```bash
# 1. Clone the repository
git clone https://github.com/argus-org/argus.git
cd argus

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

Every contribution must pass the offline regression test suite before submission:

```bash
# 1. Run core safety, isolation, and lifecycle gates
pytest tests/test_runtime_and_workspace_safety.py tests/test_gateway_security.py tests/test_wp6_deep_verification.py tests/test_wp7_deep_verification.py

# 2. Run the 500-case synthetic adversarial patch corpus
pytest -v tests/test_patch_adversarial_corpus.py

# 3. Run the full offline test suite
pytest -q --ignore=tests/test_real_host_integration.py

# 4. Verify Python syntax and bytecode compilation across the codebase
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
   - Never write to or pollute `.git/` without isolated worktrees or cleanup handlers.
4. **Security & Secrets:**
   - Never commit API keys, tokens, or private credentials.
   - Scrub sensitive environment variables and tokens before executing child subprocesses.
   - Groq provider configurations and keys are strictly prohibited across all components.

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
7. **Does it alter matching tier definitions or baseline experimental conditions?**

---

## 6. Code of Conduct

All contributors and maintainers are expected to adhere to our [Code of Conduct](CODE_OF_CONDUCT.md).
