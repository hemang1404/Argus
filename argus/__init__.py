"""Argus: Adaptive Inference Routing & Resilient Code Recovery Runtime.

Argus is an execution-feedback-driven code recovery runtime and inference optimizer.
It reconciles LLM-generated code edits using a 5-tier resilient fuzzy patch engine,
validates syntax with full-file AST checks, and iteratively tests fixes in an isolated
workspace.
"""

__version__ = "0.2.0b1"
__all__ = ["__version__"]
