# Contributing to This Project

## Branching Strategy

```
main          ← stable, always working code
├── dev       ← integration branch (merge PRs here first)
│   ├── feat/execution-harness      ← teammate feature branches
│   ├── feat/cot-strategies
│   ├── feat/heuristic-router
│   └── feat/analysis-plots
```

**Rules:**
1. Never commit directly to `main`
2. All work goes on a feature branch
3. Open a PR → at least one review → merge to `dev`
4. `dev` → `main` only when stable

## Branch Naming

```
feat/short-description       # new feature
fix/short-description        # bug fix
exp/experiment-name          # running an experiment
docs/section-name            # documentation only
```

## Commit Messages

```
feat: add per-test granularity to evaluator
fix: handle empty stderr in error classifier
exp: run 7-strategy sweep on pilot tasks
docs: add RQ definitions to README
```

## Pull Request Template

When opening a PR, fill in:
- **What** — what did you build/change?
- **Why** — which phase/task does this serve?
- **How to test** — command to verify it works
- **Results** (if an experiment) — key numbers

## Code Style

- Python 3.10+
- PEP 8 formatting (use `black` if in doubt)
- All new files must have a module-level docstring
- All new functions must have a docstring with Args + Returns
- No hardcoded API keys anywhere — always use `.env`

## Do Not Commit

- `.env` files
- `*.pkl`, `*.pt` model files > 10MB
- Raw API responses
- Your personal scratch scripts
