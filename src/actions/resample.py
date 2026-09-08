"""RESAMPLE Action: Stochastic Fresh-Start Sampling.

Discards broken code to avoid anchoring bias and queries the same model tier
with higher temperature (T=0.7) to explore alternate solution paths.
"""

from typing import Dict, Any, Optional
from src.actions.base import BaseAction, ActionResult
from src.state_builder import ExecutionState
from src.prompt_builder import format_task_prompt, SYSTEM_PROMPT


class ResampleAction(BaseAction):
    """Generates a fresh candidate solution without anchoring on previous failures."""

    def __init__(self, model_name: str = "openai/gpt-oss-20b", temperature: float = 0.7):
        super().__init__(name="RESAMPLE")
        self.model_name = model_name
        self.temperature = temperature

    def build_prompt(self, task: Dict[str, Any]) -> str:
        """Constructs clean, unanchored prompt from original task specification."""
        return format_task_prompt(task)

    def execute(
        self,
        task: Dict[str, Any],
        state: ExecutionState,
        groq_client: Any,
        **kwargs
    ) -> ActionResult:
        """Executes the RESAMPLE intervention via stochastic sampling."""
        prompt = self.build_prompt(task)
        model = kwargs.get("model", self.model_name)
        temp = kwargs.get("temperature", self.temperature)

        query_res = groq_client.query(
            prompt=prompt,
            model=model,
            system_prompt=SYSTEM_PROMPT,
            temperature=temp
        )

        return ActionResult(
            action_name=self.name,
            generated_code=query_res.get("content", ""),
            model_used=model,
            prompt=prompt,
            input_tokens=query_res.get("input_tokens", 0),
            output_tokens=query_res.get("output_tokens", 0),
            cost_usd=query_res.get("estimated_cost_usd", 0.0),
            latency_ms=query_res.get("latency_ms", 0.0),
            success=query_res.get("success", False),
            error=query_res.get("error")
        )
