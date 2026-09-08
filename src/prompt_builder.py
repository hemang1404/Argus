"""Prompt Builder for Context Injection Strategies (S0, S1, S2, S3).

Formats problem prompts with dynamically retrieved few-shot examples,
RAG technical documentation chunks, or both.
"""

from typing import Dict, List, Any, Tuple, Optional
try:
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from src.retrieval import ContextRetriever
except ImportError:
    pass


SYSTEM_PROMPT = (
    "You are an expert Python programming assistant. Write clean, complete, correct, "
    "and self-contained Python code. Do not write long docstrings or conversational explanations. "
    "Return ONLY executable Python function code."
)


def format_task_prompt(task: Dict[str, Any]) -> str:
    """Formats the base task prompt with entry_point specification."""
    prompt_text = task["prompt"].strip()
    entry_point = task.get("entry_point")
    
    if task["source"] == "MBPP":
        return (
            f"Problem Description:\n{prompt_text}\n\n"
            f"Your function must be named `{entry_point}`.\n"
            f"Return ONLY the executable Python function code."
        )
    else:
        return (
            f"{prompt_text}\n\n"
            f"Return ONLY the completed Python function code."
        )


def build_s0_prompt(task: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Strategy S0: Zero-shot baseline (No context)."""
    prompt = format_task_prompt(task)
    metadata = {
        "strategy": "S0",
        "few_shot_ids": [],
        "rag_doc_ids": [],
        "retrieval_latency_ms": 0.0
    }
    return prompt, metadata


def build_s1_prompt(
    task: Dict[str, Any],
    retriever: ContextRetriever,
    k: int = 2
) -> Tuple[str, Dict[str, Any]]:
    """Strategy S1: Few-shot exemplar context only."""
    query = task["prompt"]
    examples, latency_ms = retriever.retrieve_few_shot(query, k=k)
    
    context_sections = []
    few_shot_ids = []
    
    for i, ex in enumerate(examples, 1):
        few_shot_ids.append(ex["id"])
        context_sections.append(
            f"--- Example {i} ---\n"
            f"Problem: {ex['problem']}\n"
            f"Solution:\n{ex['solution']}\n"
        )
        
    few_shot_block = "\n".join(context_sections)
    task_block = format_task_prompt(task)
    
    full_prompt = (
        f"### Reference Code Examples:\n"
        f"{few_shot_block}\n"
        f"### Your Task:\n"
        f"{task_block}"
    )
    
    metadata = {
        "strategy": "S1",
        "few_shot_ids": few_shot_ids,
        "rag_doc_ids": [],
        "retrieval_latency_ms": latency_ms
    }
    return full_prompt, metadata


def build_s2_prompt(
    task: Dict[str, Any],
    retriever: ContextRetriever,
    k: int = 3
) -> Tuple[str, Dict[str, Any]]:
    """Strategy S2: RAG technical documentation context only."""
    query = task["prompt"]
    docs, latency_ms = retriever.retrieve_rag_docs(query, k=k)
    
    doc_sections = []
    rag_doc_ids = []
    
    for i, doc in enumerate(docs, 1):
        rag_doc_ids.append(doc["doc_id"])
        doc_sections.append(
            f"--- Documentation {i}: {doc['topic']} ---\n"
            f"{doc['content']}\n"
        )
        
    rag_block = "\n".join(doc_sections)
    task_block = format_task_prompt(task)
    
    full_prompt = (
        f"### Relevant Technical Documentation & Patterns:\n"
        f"{rag_block}\n"
        f"### Your Task:\n"
        f"{task_block}"
    )
    
    metadata = {
        "strategy": "S2",
        "few_shot_ids": [],
        "rag_doc_ids": rag_doc_ids,
        "retrieval_latency_ms": latency_ms
    }
    return full_prompt, metadata


def build_s3_prompt(
    task: Dict[str, Any],
    retriever: ContextRetriever,
    few_shot_k: int = 2,
    rag_k: int = 3
) -> Tuple[str, Dict[str, Any]]:
    """Strategy S3: Hybrid Context (Few-shot exemplars + RAG documentation)."""
    query = task["prompt"]
    docs, rag_lat = retriever.retrieve_rag_docs(query, k=rag_k)
    examples, fs_lat = retriever.retrieve_few_shot(query, k=few_shot_k)
    
    # 1. Build RAG documentation block
    doc_sections = []
    rag_doc_ids = []
    for i, doc in enumerate(docs, 1):
        rag_doc_ids.append(doc["doc_id"])
        doc_sections.append(
            f"--- Documentation {i}: {doc['topic']} ---\n"
            f"{doc['content']}\n"
        )
    rag_block = "\n".join(doc_sections)
    
    # 2. Build Few-shot block
    context_sections = []
    few_shot_ids = []
    for i, ex in enumerate(examples, 1):
        few_shot_ids.append(ex["id"])
        context_sections.append(
            f"--- Example {i} ---\n"
            f"Problem: {ex['problem']}\n"
            f"Solution:\n{ex['solution']}\n"
        )
    few_shot_block = "\n".join(context_sections)
    
    task_block = format_task_prompt(task)
    
    full_prompt = (
        f"### Relevant Technical Documentation & Patterns:\n"
        f"{rag_block}\n"
        f"### Reference Code Examples:\n"
        f"{few_shot_block}\n"
        f"### Your Task:\n"
        f"{task_block}"
    )
    
    metadata = {
        "strategy": "S3",
        "few_shot_ids": few_shot_ids,
        "rag_doc_ids": rag_doc_ids,
        "retrieval_latency_ms": round(rag_lat + fs_lat, 2)
    }
    return full_prompt, metadata


def build_prompt(
    strategy: str,
    task: Dict[str, Any],
    retriever: Optional[ContextRetriever] = None,
    few_shot_k: int = 2,
    rag_k: int = 3
) -> Tuple[str, Dict[str, Any]]:
    """Unified dispatcher for building any strategy prompt (S0, S1, S2, S3)."""
    if strategy == "S0":
        return build_s0_prompt(task)
    elif strategy == "S1":
        if not retriever:
            raise ValueError("ContextRetriever required for S1 prompt.")
        return build_s1_prompt(task, retriever, k=few_shot_k)
    elif strategy == "S2":
        if not retriever:
            raise ValueError("ContextRetriever required for S2 prompt.")
        return build_s2_prompt(task, retriever, k=rag_k)
    elif strategy == "S3":
        if not retriever:
            raise ValueError("ContextRetriever required for S3 prompt.")
        return build_s3_prompt(task, retriever, few_shot_k=few_shot_k, rag_k=rag_k)
    else:
        raise ValueError(f"Unknown strategy: {strategy}")
