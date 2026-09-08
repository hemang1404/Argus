"""REPAIR Action: Iterative Code Refinement with Execution Diagnostics.

Sends the previously failing code, error taxonomy, and specific failed assertions
back to the same model tier to patch identified bugs.
"""

from typing import Dict, Any, Optional
from src.actions.base import BaseAction, ActionResult
from src.state_builder import ExecutionState
from src.prompt_builder import format_task_prompt, SYSTEM_PROMPT


class RepairAction(BaseAction):
    """Refines previously broken code by conditioning on runtime diagnostics."""

    def __init__(self, model_name: str = "openai/gpt-oss-20b", temperature: float = 0.2):
        super().__init__(name="REPAIR")
        self.model_name = model_name
        self.temperature = temperature

    def build_prompt(self, task: Dict[str, Any], state: ExecutionState) -> str:
        """Constructs error-conditioned repair prompt."""
        base_prompt = format_task_prompt(task)
        error_summary = state.error_type or "assertion_error"

        # Extract specific failing assertions (limit to top 3 to prevent prompt bloat)
        details_str = ""
        if state.latest_eval and state.latest_eval.get("failed_test_details"):
            details = state.latest_eval["failed_test_details"]
            details_str = "\n".join(f"- Failed Assertion: {d['test_line']}\n  Error: {d['error']}" for d in details[:3])
        elif state.latest_eval and state.latest_eval.get("error_msg"):
            details_str = state.latest_eval["error_msg"][:300]
        else:
            details_str = "Tests failed without detailed exception output."

        return (
            f"### Problem Description:\n{base_prompt}\n\n"
            f"### Previous Implementation (Failed with {error_summary}):\n"
            f"```python\n{state.latest_code}\n```\n\n"
            f"### Test Execution Diagnostics:\n"
            f"{details_str}\n\n"
            f"Analyze why the implementation failed and return ONLY the complete, corrected Python function code."
        )

    def execute(
        self,
        task: Dict[str, Any],
        state: ExecutionState,
        groq_client: Any,
        **kwargs
    ) -> ActionResult:
        """Executes the REPAIR intervention via the LLM client."""
        prompt = self.build_prompt(task, state)
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
