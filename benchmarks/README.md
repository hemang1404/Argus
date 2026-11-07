# Argus Benchmark Reproducibility

This directory outlines the evaluation methodologies and reproducibility guidelines for the Argus adaptive routing and recovery runtime.

## Core Evaluation Pillars

1. **Adversarial Patch Corpus**: Evaluates deterministic patch reconciliation across 500 edge cases (whitespace shifts, line modifications, refactored context, collision avoidance).
2. **Offline Anchor Matching**: Measures deterministic fallback behavior when exact line-number anchors drift.
3. **Budget Concurrency & Enforcing**: Confirms hard USD spend ceilings under concurrent request streams.

For full empirical evaluation protocols, see [BENCHMARK_METHODOLOGY.md](../docs/BENCHMARK_METHODOLOGY.md).
