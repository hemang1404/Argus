"""Fixed Baseline Strategies for Controlled Empirical Evaluation.

Implements pure benchmark baselines:
- AlwaysRepair: Always attempts code repair with error feedback
- AlwaysResample: Always discards broken code and samples fresh
- AlwaysEscalate: Immediately escalates to large frontier model on failure
- RandomAction: Selects an intervention uniformly at random
"""

import random
from typing import Callable, List, Optional
from src.state_builder import ExecutionState


class AlwaysRepairStrategy:
    """Baseline policy that always attempts iterative repair."""
    def __init__(self):
        self.name = "always_repair"

    def __call__(self, state: ExecutionState) -> str:
        if state.is_terminal:
            return "STOP"
        return "REPAIR"


class AlwaysResampleStrategy:
    """Baseline policy that always resamples from scratch."""
    def __init__(self):
        self.name = "always_resample"

    def __call__(self, state: ExecutionState) -> str:
        if state.is_terminal:
            return "STOP"
        return "RESAMPLE"


class AlwaysEscalateStrategy:
    """Baseline policy that immediately escalates to frontier model tier."""
    def __init__(self):
        self.name = "always_escalate"

    def __call__(self, state: ExecutionState) -> str:
        if state.is_terminal:
            return "STOP"
        return "ESCALATE"


class RandomActionStrategy:
    """Stochastic baseline policy that picks an intervention uniformly at random."""
    def __init__(self, action_pool: Optional[List[str]] = None, seed: Optional[int] = None):
        self.name = "random_action"
        self.action_pool = action_pool or ["REPAIR", "RESAMPLE", "ESCALATE"]
        self.rng = random.Random(seed)

    def __call__(self, state: ExecutionState) -> str:
        if state.is_terminal:
            return "STOP"
        return self.rng.choice(self.action_pool)
