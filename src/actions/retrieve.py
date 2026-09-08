"""RETRIEVE Action: Dynamic Error-Conditioned Context Injection.

Supports three distinct retrieval modalities:
1. mode="docs"     -> Retrieves API documentation & syntax patterns (RAG)
2. mode="few_shot" -> Retrieves solved problem-solution code exemplars
3. mode="hybrid"   -> Injects both documentation and code exemplars
"""

from typing import Dict, Any, Optional, List
from src.actions.base import BaseAction, ActionResult
from src.state_builder import ExecutionState
from src.prompt_builder import format_task_prompt, SYSTEM_PROMPT


class RetrieveAction(BaseAction):
    """Augments prompt with retrieved technical documentation and/or solved code exemplars."""

    def __init__(
        self,
        model_name: str = "openai/gpt-oss-20b",
        temperature: float = 0.2,
        mode: str = "docs",
        top_k_docs: int = 2,
        top_k_examples: int = 2
    ):
        name = f"RETRIEVE_{mode.upper()}" if mode in ["few_shot", "hybrid"] else "RETRIEVE"
        super().__init__(name=name)
        self.model_name = model_name
        self.temperature = temperature
        self.mode = mode.lower()
        self.top_k_docs = top_k_docs
        self.top_k_examples = top_k_examples

    def build_prompt(
        self,
        task: Dict[str, Any],
        state: ExecutionState,
        retriever: Optional[Any],
        mode: Optional[str] = None
    ) -> str:
        """Constructs prompt augmented with docs, few-shot examples, or both."""
        task_block = format_task_prompt(task)
        active_mode = (mode or self.mode).lower()

        if not retriever:
            return task_block

        # Query formed from task prompt and runtime error diagnostics
        err_snippet = state.error_msg[:100] if state.error_msg else ""
        query = f"{task['prompt']} {state.error_type or ''} {err_snippet}".strip()

        sections: List[str] = []

        # 1. Retrieve RAG documentation chunks
        if active_mode in ["docs", "hybrid"]:
            docs, _ = retriever.retrieve_rag_docs(query, k=self.top_k_docs)
            if docs:
                doc_lines = []
                for i, doc in enumerate(docs, 1):
                    doc_lines.append(
                        f"--- Documentation {i}: {doc.get('topic', 'Reference')} ---\n"
                        f"{doc.get('content', '')}\n"
                    )
                sections.append(
                    "### Relevant Technical Documentation & Patterns:\n" + "\n".join(doc_lines)
                )

        # 2. Retrieve Few-Shot Exemplars
        if active_mode in ["few_shot", "hybrid"]:
            examples, _ = retriever.retrieve_few_shot(query, k=self.top_k_examples)
            if examples:
                ex_lines = []
                for i, ex in enumerate(examples, 1):
                    ex_lines.append(
                        f"--- Example {i} ---\n"
                        f"Problem: {ex.get('problem', '')}\n"
                        f"Solution:\n{ex.get('solution', '')}\n"
                    )
                sections.append(
                    "### Reference Code Examples:\n" + "\n".join(ex_lines)
                )

        if not sections:
            return task_block

        context_block = "\n\n".join(sections)
        return f"{context_block}\n\n### Your Task:\n{task_block}"

    def execute(
        self,
        task: Dict[str, Any],
        state: ExecutionState,
        groq_client: Any,
        **kwargs
    ) -> ActionResult:
        """Executes context-augmented recovery generation."""
        retriever = kwargs.get("retriever")
        mode = kwargs.get("mode", self.mode)
        prompt = self.build_prompt(task, state, retriever, mode=mode)
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
