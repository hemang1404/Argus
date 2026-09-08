"""Base Action Interface and Result Container for Argus Interventions.

Defines the contract for all test-time compute allocation actions:
REPAIR, RESAMPLE, ESCALATE, and RETRIEVE.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Any, Optional
from src.state_builder import ExecutionState


@dataclass
class ActionResult:
    """Standardized output container returned by any recovery intervention."""
    action_name: str
    generated_code: str
    model_used: str
    prompt: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    success: bool
    error: Optional[str] = None


class BaseAction(ABC):
    """Abstract Base Class for all recovery actions (Strategy Pattern)."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def execute(
        self,
        task: Dict[str, Any],
        state: ExecutionState,
        groq_client: Any,
        **kwargs
    ) -> ActionResult:
        """Executes the recovery action and returns an ActionResult."""
        pass
