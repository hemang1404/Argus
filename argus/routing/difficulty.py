"""Adaptive difficulty classification for LLM reasoning effort routing.

Analyzes task characteristics, code context, error feedback, and scope to select
an appropriate effort tier ('low', 'medium', 'high') without making paid API calls.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class DifficultyDecision:
    """Structured decision returned by the difficulty classifier."""
    effort: str  # "low", "medium", or "high"
    confidence: float  # Normalized score 0.0 to 1.0
    reasons: List[str] = field(default_factory=list)
    signals: Dict[str, Any] = field(default_factory=dict)


class DifficultyClassifier:
    """Inexpensive, deterministic heuristic difficulty classifier.
    
    Evaluates:
    - Task question vs code modification
    - Size and complexity of target code
    - Debugging traceback depth and failure taxonomy
    - Multi-file scope vs single-function locality
    - Architectural / refactoring keywords
    """

    ARCHITECTURAL_PATTERN = re.compile(
        r"\b(?:refactor|architecture|redesign|migration|concurrency|asyncio|multithread|"
        r"deadlock|race\s+condition|protocol|schema\s+migration|rewrite|distributed)\b",
        re.IGNORECASE,
    )

    MULTI_FILE_PATTERN = re.compile(
        r"\b(?:multi-file|across\s+files|multiple\s+files|whole\s+(?:project|repo|codebase)|cross-module|repository-wide)\b",
        re.IGNORECASE,
    )

    SIMPLE_QUESTION_PATTERN = re.compile(
        r"\b(?:what\s+is|how\s+do\s+i|explain|meaning\s+of|fix\s+typo|spelling|add\s+docstring|"
        r"add\s+comment|rename\s+variable|quick\s+question|single\s+line)\b",
        re.IGNORECASE,
    )

    COMPLEX_EXCEPTION_PATTERN = re.compile(
        r"\b(?:RecursionError|MemoryError|SystemError|TimeoutError|DeadlockError|SegmentationFault)\b"
    )

    TRACEBACK_FRAME_PATTERN = re.compile(r'File ".*?", line \d+')

    def __init__(self, default_effort: str = "medium") -> None:
        if default_effort not in ("low", "medium", "high"):
            default_effort = "medium"
        self.default_effort = default_effort

    def classify(
        self,
        prompt: str,
        code: Optional[str] = None,
        error_feedback: Optional[str] = None,
        files: Optional[List[str]] = None,
        explicit_override: Optional[str] = None,
        disable_adaptive: bool = False,
    ) -> DifficultyDecision:
        """Classify task difficulty and determine appropriate reasoning effort."""
        # Check explicit user override
        if explicit_override:
            effort_norm = explicit_override.strip().lower()
            if effort_norm in ("low", "medium", "high"):
                return DifficultyDecision(
                    effort=effort_norm,
                    confidence=1.0,
                    reasons=[f"Explicit user override: effort={effort_norm}"],
                    signals={"override": effort_norm},
                )

        # Check configuration / env disable switch
        env_disable = os.getenv("ARGUS_DISABLE_ADAPTIVE_ROUTING", "").strip().lower() in ("1", "true", "yes")
        if disable_adaptive or env_disable:
            return DifficultyDecision(
                effort=self.default_effort,
                confidence=1.0,
                reasons=[f"Adaptive routing disabled; conservative default '{self.default_effort}' applied"],
                signals={"adaptive_disabled": True},
            )

        # Extract Task Signals
        signals: Dict[str, Any] = {}
        prompt_text = prompt or ""
        code_text = code or ""
        error_text = error_feedback or ""
        file_list = files or []

        # Signal 1: Architectural & Multi-file presence
        arch_matches = self.ARCHITECTURAL_PATTERN.findall(prompt_text)
        multi_file_prompt_matches = self.MULTI_FILE_PATTERN.findall(prompt_text)
        file_count = len(file_list)
        is_multi_file = (file_count > 1) or bool(multi_file_prompt_matches)

        signals["architectural_keywords"] = arch_matches
        signals["is_multi_file"] = is_multi_file
        signals["file_count"] = file_count

        # Signal 2: Diagnostic & Traceback Complexity
        traceback_frames = len(self.TRACEBACK_FRAME_PATTERN.findall(error_text))
        complex_err_matches = self.COMPLEX_EXCEPTION_PATTERN.findall(error_text)
        has_error = bool(error_text.strip())
        error_lines = len(error_text.strip().splitlines()) if has_error else 0

        signals["traceback_frames"] = traceback_frames
        signals["complex_exceptions"] = complex_err_matches
        signals["has_error_feedback"] = has_error
        signals["error_lines"] = error_lines

        # Signal 3: Prompt & Code Volumetrics
        prompt_words = len(prompt_text.split())
        code_lines = len(code_text.strip().splitlines()) if code_text else 0

        signals["prompt_words"] = prompt_words
        signals["code_lines"] = code_lines

        # Signal 4: Simple Question / Informational Queries
        simple_matches = self.SIMPLE_QUESTION_PATTERN.findall(prompt_text)
        signals["simple_question_keywords"] = simple_matches

        # Decision Evaluation
        reasons: List[str] = []

        # High Effort Conditions
        high_score = 0
        if is_multi_file:
            high_score += 3
            reasons.append(f"Multi-file scope detected ({file_count} files, '{', '.join(multi_file_prompt_matches[:2])}')")
        if arch_matches:
            if len(arch_matches) >= 2 or code_lines >= 50:
                high_score += 3
            else:
                high_score += 2
            reasons.append(f"Architectural keywords found: {', '.join(set(arch_matches[:3]))}")
        if complex_err_matches:
            high_score += 3
            reasons.append(f"Severe exception type in traceback: {', '.join(set(complex_err_matches))}")
        elif traceback_frames >= 4:
            high_score += 3
            reasons.append(f"Deep traceback with {traceback_frames} frames")
        elif error_lines > 25:
            high_score += 1
            reasons.append(f"Extensive failure diagnostic log ({error_lines} lines)")

        if high_score >= 3:
            confidence = min(0.95, 0.75 + (high_score * 0.05))
            return DifficultyDecision(
                effort="high",
                confidence=round(confidence, 2),
                reasons=reasons,
                signals=signals,
            )

        # Low Effort Conditions
        # Simple questions or typos with no failing tests and no architectural flags
        if simple_matches and not has_error and not arch_matches and code_lines < 40 and not is_multi_file:
            low_reasons = [f"Informational / simple inquiry detected: '{simple_matches[0]}'"]
            if code_lines == 0:
                low_reasons.append("No source code editing required")
            else:
                low_reasons.append(f"Small local code context ({code_lines} lines)")
            return DifficultyDecision(
                effort="low",
                confidence=0.90,
                reasons=low_reasons,
                signals=signals,
            )

        # Medium Effort Conditions (Standard coding tasks, single-file bug fix, normal tests)
        med_reasons: List[str] = []
        if has_error:
            med_reasons.append(f"Standard single-module test failure ({traceback_frames} traceback frames)")
        elif code_lines > 0:
            med_reasons.append(f"Standard feature implementation / code modification ({code_lines} lines)")
        else:
            med_reasons.append("General coding task within single-file scope")

        if arch_matches:
            med_reasons.append("Architectural cues present but isolated to single file")

        return DifficultyDecision(
            effort="medium",
            confidence=0.85 if has_error else 0.75,
            reasons=med_reasons,
            signals=signals,
        )
