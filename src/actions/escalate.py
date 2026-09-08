"""ESCALATE Action: Model Tier Escalation to Frontier Model.

Reroutes the task to a larger, more capable model tier (openai/gpt-oss-120b)
when the problem complexity exceeds small-model capacity.
"""

from typing import Dict, Any, Optional
from src.actions.base import BaseAction, ActionResult
from src.state_builder import ExecutionState
from src.prompt_builder import format_task_prompt, SYSTEM_PROMPT


class EscalateAction(BaseAction):
    """Escalates execution to a higher-capacity frontier model tier."""

    def __init__(self, model_name: str = "openai/gpt-oss-120b", temperature: float = 0.2):
        super().__init__(name="ESCALATE")
        self.model_name = model_name
        self.temperature = temperature

    def build_prompt(self, task: Dict[str, Any]) -> str:
        """Constructs prompt for frontier model generation."""
        return format_task_prompt(task)

    def execute(
        self,
        task: Dict[str, Any],
        state: ExecutionState,
        groq_client: Any,
        **kwargs
    ) -> ActionResult:
        """Executes the ESCALATE intervention using the large model tier."""
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
