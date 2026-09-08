"""Argus Controlled Experiments Package.

Contains the 3 primary empirical experiment runners:
1. run_isocost.py -> Iso-Cost Strategy Comparison (Experiment 1)
2. run_marginal_returns.py -> Marginal Return Curves across Attempts (Experiment 2)
3. run_intervention_matrix.py -> Failure Mode to Intervention Matrix (Experiment 3)
"""

from src.experiments.run_isocost import run_isocost_experiment
from src.experiments.run_marginal_returns import run_marginal_returns_experiment
from src.experiments.run_intervention_matrix import run_intervention_matrix_experiment

__all__ = [
    "run_isocost_experiment",
    "run_marginal_returns_experiment",
    "run_intervention_matrix_experiment",
]
