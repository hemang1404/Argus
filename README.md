# Argus — Adaptive Execution-Aware Inference Router

> **Research → Production** | AI Systems · Inference Optimization · Code Generation
> Cut LLM inference costs by 50–70%, verified by execution.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Status: Research Phase](https://img.shields.io/badge/status-research%20phase-yellow.svg)]()

---

## What is Argus?

Argus is an **execution-aware inference router** for LLM code generation. Instead of blindly sending every problem to an expensive frontier model, Argus:

1. Tries the cheapest plausible approach first
2. Executes generated code against tests in a sandbox
3. Observes the failure (error type, tests passed, error message)
4. Decides what to do next — repair, retrieve docs, resample, or escalate
5. Repeats until solved or budget is exhausted

The result: **93.3% accuracy at 66.6% lower cost** vs. direct frontier model routing (from N=1,224 empirical runs).

---

## Project Status

| Phase | Description | Status |
|---|---|---|
| **Preliminary Study** | 7-strategy baseline, 1,224 API runs, cascade economics | ✅ Complete |
| **Phase 2** | Execution harness + per-test evaluator | 🔄 In Progress |
| **Phase 3** | Heuristic adaptive router | ⬜ Planned |
| **Phase 4** | Learned router (supervised) | ⬜ Planned |
| **Phase 5** | RL policy | ⬜ Planned |
| **Phase 6** | Ablation studies + paper | ⬜ Planned |
| **Phase 7** | Production API + SDK | ⬜ Planned |

---

## Key Findings (Preliminary Study, N=1,224 runs)

| System | Pass Rate | Cost / 100K Solved |
|---|---|---|
| Large model (120B), Zero-shot | 80.0% | $51.85 |
| Small model (20B), Zero-shot | 77.0% | $8.29 |
| **Argus (S2→Large Cascade)** | **93.3%** | **$17.31** |

---

## System Architecture

```
CODING PROBLEM
      │
      ▼
┌──────────────────┐
│  Query Analyzer  │  ← problem features, difficulty estimate
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  Initial Router  │  ← selects cheapest plausible strategy + model
└────────┬─────────┘
         │ (strategy, model)
         ▼
    CODE GENERATION
         │
         ▼
┌──────────────────┐
│   Test Harness   │  ← sandboxed subprocess, per-test granularity
└────────┬─────────┘
         │ execution result
         ▼
┌──────────────────┐
│  State Builder   │  ← s_t = {problem, code, tests, errors, history, cost}
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  Adaptive Policy │  ← heuristic / learned / RL
└────────┬─────────┘
    ┌────┴────┬──────────┬────────┐
    ▼         ▼          ▼        ▼
 REPAIR   RETRIEVE   RESAMPLE  ESCALATE
    └────────────────────────────┘
                   │
              loop back
```

---

## Repository Structure

```
├── configs/
│   └── experiment.yaml          # Hyperparameters (single source of truth)
├── data/
│   ├── candidate_tasks.jsonl    # 200 normalized tasks (MBPP + HumanEval)
│   ├── pilot_tasks.json         # 50 frozen pilot benchmark
│   ├── few_shot_examples.jsonl  # 60 curated code exemplars
│   └── rag_documents.jsonl      # 25 algorithmic reference docs
├── src/
│   ├── groq_client.py           # Multi-key Groq API pool with failover
│   ├── evaluator.py             # Subprocess sandboxed code execution
│   ├── task_loader.py           # MBPP/HumanEval dataset normalizer
│   ├── retrieval.py             # Sentence-transformer semantic retrieval
│   ├── prompt_builder.py        # Strategy prompt assembly (S0–S6)
│   ├── metrics.py               # Cascade economics & gap closure metrics
│   ├── run_screening.py         # Baseline screening runner
│   ├── run_context_experiment.py# Context strategy runner
│   ├── analyze_results.py       # Academic tables & statistical analysis
│   └── generate_plots.py        # Publication-quality chart generation
├── results/                     # All experiment CSV outputs
├── figures/                     # Publication-quality plots
├── tests/                       # Unit tests
├── CONTRIBUTING.md
└── README.md
```

---

## Setup

### 1. Clone & Install

```bash
git clone https://github.com/YOUR_USERNAME/Argus.git
cd Argus
pip install -r requirements.txt
```

### 2. Configure API Keys

```bash
cp .env.example .env
# Edit .env and add your GROQ_API_KEY
```

### 3. Verify Setup

```bash
python -c "from src.groq_client import GroqClient; c = GroqClient(); print('Argus OK')"
```

---

## Running Experiments

```bash
# Baseline screening (Phase A)
python -m src.run_screening

# Context strategy experiment (Phase B/C)
python -m src.run_context_experiment

# Generate plots
python -m src.generate_plots
python -m src.analyze_results
```

---

## Research Questions

| RQ | Question |
|---|---|
| **RQ1** | Can adaptive selection of inference strategies improve code-generation performance compared with fixed prompting strategies? |
| **RQ2** | Can execution feedback enable a router to select the most cost-effective next intervention after a failed attempt? |
| **RQ3** | Can execution-aware intervention routing achieve a better correctness–cost–latency trade-off than model-only routing? |
| **RQ4** | Does reinforcement learning improve sequential inference allocation compared with heuristic and supervised routing? |

---

## Competitors

| Tool | What They Do | Argus Advantage |
|---|---|---|
| LiteLLM | Unified API across providers | No execution loop, no repair |
| RouteLLM (Salesforce) | Strong/weak model routing | Single-shot, no code execution |
| Martian | Commercial routing | Black box, no verification |
| Not Diamond | Task-type routing | Single-shot only |

**Argus is the only router that executes code, verifies correctness, and adapts its next action based on what failed.**

---

## Team

| Role | Responsibility |
|---|---|
| Lead | Infrastructure, execution harness, routers, RL, experiments |
| Teammate A | CoT strategies, evaluator extension, Pareto plot |
| Teammate B | Literature review, formal problem definition, baselines, paper |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch strategy and PR guidelines.

---

## License

MIT — see [LICENSE](LICENSE) for details.
