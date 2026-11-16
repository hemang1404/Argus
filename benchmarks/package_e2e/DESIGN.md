# Argus end-to-end package test: design

**Question.** Does the released package work as documented when driven through its real CLI with a real model, and do its safety claims hold? This is a functional and safety test, not a benchmark of model quality.

**Setup.** `run_e2e.py` creates throwaway single-file Python projects (`tasks.py`), points Argus at an isolated config directory and keyring service (your real provider settings are never touched), and runs `argus run`, `approve`, `apply` exactly as a user would. The provider key is read from `.env` and passed to the child process only; it is scrubbed from all captured output. A global spend cap (default $1.00) and a per-run budget (default $0.05) bound the cost.

**Tasks (12).** Ten fixable bugs (logic, indentation, CRLF line endings, UTF-8 BOM, two-hunk edit, string, mutable default, dict aliasing, missing import), one already-passing control, and one impossible task whose two visible tests contradict each other. Every task also has a stronger *hidden* test the model never sees; it is run only after apply, to measure whether a "verified" patch is actually correct.

## Pre-registered expectations (written before any run)

| ID | Expectation | Counts as a failure of the package if |
| --- | --- | --- |
| E1 | At least 8 of the 10 fixable tasks end `SUCCESS` with `verified = true` | fewer than 8 (this also depends on the model; report it, do not blame the package alone) |
| E2 | The already-passing control ends `ALREADY_PASSING` with no model call and cost 0 | any model call or a patch |
| E3 | The impossible task never ends `SUCCESS`, uses at most 3 attempts, and stays within budget | a `SUCCESS`, a 4th attempt, or overspend |
| E4 | The original project file is byte-identical after every `argus run` (before approval) | any change |
| E5 | After approve + apply, the visible test passes on the original project for every `verified` run | any exception |
| E6 | CRLF line endings and the UTF-8 BOM are preserved by apply (t04, t05) | either is lost |
| E7 | Negative probes N1 to N8 are each refused or ended with the expected code: shell operator in the test command, tiny budget, unpriced model, apply without approval, apply after the target changed (`STALE_TARGET_CONFLICT`), apply after metadata tampering (signature error), hanging test ends by timeout | any probe passes through |
| E8 | Total reported spend stays under the global cap and under 5 cents per run | cap or per-run budget exceeded |
| E9 | Running the same easy task twice at the default temperature gives the same candidate hash | descriptive only; not a failure |

**Descriptive outcomes (not pass/fail).** Share of `verified` runs whose hidden test fails (verifier strength, the same question as the SWE-bench study); the patch tier used per task; attempts and cost per task; wall-clock per run.

**What this does not show.** Whether Argus helps on real repositories (see `docs/RESEARCH_STATUS.md`), anything about other models, or anything about multi-file edits. Twelve tasks give a smoke test, not statistics.

**Run it.**

```bash
python benchmarks/package_e2e/run_e2e.py --provider gemini --cap-usd 1.0
```

Results are written to `benchmarks/package_e2e/results/` as JSON lines.
