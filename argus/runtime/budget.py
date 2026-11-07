"""Atomic Budget Reservation and Multi-Tier Accounting for Argus.

Provides thread-safe pre-dispatch budget reservation, post-dispatch settlement,
uncertain spend quarantine, and global experiment spending controls.
Token counts are strictly divided by 1,000,000 for exact dollar accounting.
"""

from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from argus.config import ModelCost

ARGUS_SYSTEM_PROMPT = (
    "You are Argus, an expert software debugging and repair assistant. "
    "Your job is to fix failing tests by modifying the source code. "
    "Always output your code changes in exact SEARCH/REPLACE block format:\n"
    "<<<<<<< SEARCH\n"
    "[exact lines from file to replace]\n"
    "=======\n"
    "[new replacement lines]\n"
    ">>>>>>> REPLACE"
)


def get_gpt_oss_tokenizer(model: str = "openai/gpt-oss-20b"):
    """Retrieve the native tokenizer for GPT-OSS models.
    
    GPT-OSS (20B and 120B) uses the o200k_harmony BPE vocabulary.
    Falls back gracefully to o200k_base or cl100k_base if harmony is unavailable.
    """
    try:
        import tiktoken
        for name in ("o200k_harmony", "o200k_base", "cl100k_base"):
            try:
                return tiktoken.get_encoding(name)
            except Exception:
                continue
    except Exception:
        pass
    return None


def count_prompt_tokens(
    prompt_or_messages: Union[str, List[Dict[str, str]]],
    model: str = "openai/gpt-oss-20b",
    system_prompt: Optional[str] = None,
) -> int:
    """Conservatively estimate prompt tokens for pre-dispatch budget reservation.
    
    Tokenizer Selection:
      Uses GPT-OSS's native 'o200k_harmony' BPE vocabulary (or o200k_base/cl100k_base fallback).
    
    Wire Framing Overhead Accounted:
      - 4 tokens per message for ChatML role delimiters (<|im_start|>{role}\n{content}<|im_end|>\n)
      - 3 tokens for assistant generation priming (<|im_start|>assistant\n)
      - System message content (defaults to ARGUS_SYSTEM_PROMPT if a single user prompt string is passed)
      - User prompt content
      - A conservative safety buffer (+15% or minimum 32 tokens) for provider-side metadata or header wrappers.
    
    Policy Note — Conservative Estimated Cap vs Guaranteed Billing Cap:
      Pre-dispatch estimation is an intentionally conservative heuristic to refuse
      requests that cannot reasonably fit within the task or global budget limit.
      Because upstream cloud providers (e.g. Groq) may prepend internal headers,
      guardrail prompts, or proprietary routing metadata, this calculation provides
      a defensible conservative estimated spending cap rather than a mathematically
      guaranteed billing cap. Actual financial liabilities are settled post-dispatch
      against provider-reported wire telemetry ('usage' field), and in-flight dispatches
      that abort or time out are quarantined against the cap.
    """
    tokenizer = get_gpt_oss_tokenizer(model)

    # Normalize messages structure
    if isinstance(prompt_or_messages, list):
        messages = prompt_or_messages
    else:
        sys_content = system_prompt if system_prompt is not None else ARGUS_SYSTEM_PROMPT
        user_content = str(prompt_or_messages)
        messages = [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": user_content},
        ]

    if tokenizer is not None:
        raw_tokens = 0
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "") or ""
            # 4 tokens per message for <|im_start|>{role}\n...<|im_end|>\n framing
            raw_tokens += 4
            raw_tokens += len(tokenizer.encode(role, allowed_special="all"))
            raw_tokens += len(tokenizer.encode(content, allowed_special="all"))
        # 3 tokens for assistant priming
        raw_tokens += 3

        # Add 20% safety buffer (minimum 48 tokens) for provider runtime headers,
        # ChatML serialization variations, and upstream system preambles
        safety_buffer = max(48, math.ceil(raw_tokens * 0.20))
        return raw_tokens + safety_buffer

    # Conservative empirical fallback when tiktoken is unavailable:
    # Most code/text is ~3-4 characters per token in UTF-8.
    # To be strictly conservative without local BPE, estimate 1 token per 2 bytes plus framing.
    # Note: Clearly documented as an empirical conservative estimate, not a proven bound.
    total_bytes = 0
    for msg in messages:
        total_bytes += len(msg.get("role", "").encode("utf-8"))
        total_bytes += len(msg.get("content", "").encode("utf-8"))
        total_bytes += 16  # framing overhead

    estimated = math.ceil(total_bytes / 2.0) + 20
    return estimated


def is_conclusive_provider_refusal(err: Exception) -> bool:
    """Determine if an error conclusively establishes that the provider did not accept or execute the request.

    Conclusive refusals (safe to release reserved budget):
      - HTTP 429: Provider rejected the request due to rate limiting without processing tokens.
      - HTTP 400, 401, 403, 404: Client-side syntax error, invalid authorization, or route failure
        rejected at gateway before model inference.
      - Pre-connection DNS or TCP connection refused (e.g. socket.gaierror, ConnectionRefusedError)
        where no bytes were transmitted to the provider.

    Uncertain failures (MUST be quarantined against task-condition and global budget):
      - HTTP 504 Gateway Timeout or client-side read/connection timeouts.
      - HTTP 500, 502, 503 Server Errors where upstream model compute may have started or completed.
      - Connection resets, broken pipes, or mid-stream network drops.
      - Task deadlines, cancellations, or aborts that occur after dispatch.
    """
    status_code = getattr(err, "status_code", None)
    if status_code in (400, 401, 403, 404, 405, 413, 422, 429):
        return True

    msg = str(err).lower()
    # Check for DNS or connection refused indicators prior to socket connection
    if (
        "connection refused" in msg
        or "getaddrinfo failed" in msg
        or "name or service not known" in msg
        or "nodename nor servname provided" in msg
    ):
        return True

    return False


class GlobalExperimentBudget:
    """Thread-safe global experiment budget ceiling.

    EXECUTION MODEL CONSTRAINT:
    Argus experiments are strictly constrained to a single-process execution model.
    Concurrency across task-condition runs must be executed via in-process worker threads
    (e.g., concurrent.futures.ThreadPoolExecutor), which share this singleton instance
    and coordinate atomically via reentrant/mutex locks.
    Multi-process execution without an external IPC coordinator is prohibited.
    """
    _instance: Optional[GlobalExperimentBudget] = None
    _lock = threading.Lock()

    def __init__(self, global_cap_usd: float = 5.0, initial_settled_usd: float = 0.0) -> None:
        self.global_cap_usd = float(global_cap_usd)
        self.global_settled: float = float(initial_settled_usd)
        self.global_quarantined: float = 0.0
        self.global_reserved: float = 0.0
        self._mutex = threading.Lock()
        self._creator_pid = os.getpid()

    def check_process_affinity(self) -> None:
        """Verify that callers operate within the designated coordinator process."""
        current_pid = os.getpid()
        if current_pid != self._creator_pid:
            raise RuntimeError(
                f"GlobalExperimentBudget process violation: Budget initialized in coordinator PID "
                f"{self._creator_pid} but accessed from worker PID {current_pid}. Argus requires a "
                f"single-process execution model with thread-based concurrency for atomic accounting."
            )

    @classmethod
    def get_instance(cls, global_cap_usd: float = 5.0) -> GlobalExperimentBudget:
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(global_cap_usd=global_cap_usd)
            return cls._instance

    @classmethod
    def reset_instance(cls, global_cap_usd: float = 5.0, initial_settled_usd: float = 0.0) -> GlobalExperimentBudget:
        """Reset or recreate the global singleton instance (for test isolation)."""
        with cls._lock:
            cls._instance = cls(global_cap_usd=global_cap_usd, initial_settled_usd=initial_settled_usd)
            return cls._instance

    @classmethod
    def set_instance(cls, instance: Optional[GlobalExperimentBudget]) -> None:
        """Explicitly inject a global budget instance."""
        with cls._lock:
            cls._instance = instance

    @property
    def total_committed(self) -> float:
        self.check_process_affinity()
        with self._mutex:
            return round(self.global_settled + self.global_quarantined + self.global_reserved, 7)

    @property
    def available(self) -> float:
        self.check_process_affinity()
        with self._mutex:
            return max(0.0, round(self.global_cap_usd - (self.global_settled + self.global_quarantined + self.global_reserved), 7))

    def reserve(self, amount: float) -> bool:
        """Atomically reserve funds against the global ceiling."""
        self.check_process_affinity()
        if amount <= 0.0:
            return True
        with self._mutex:
            projected = self.global_settled + self.global_quarantined + self.global_reserved + amount
            if projected > self.global_cap_usd + 1e-9:
                return False
            self.global_reserved = round(self.global_reserved + amount, 7)
            return True

    def settle(self, reserved_amount: float, actual_cost: float) -> None:
        """Settle an outstanding reservation with actual confirmed spend."""
        self.check_process_affinity()
        with self._mutex:
            self.global_reserved = max(0.0, round(self.global_reserved - reserved_amount, 7))
            self.global_settled = round(self.global_settled + actual_cost, 7)

    def quarantine(self, reserved_amount: float) -> None:
        """Quarantine uncertain spend (e.g. timeout after dispatch) against global cap."""
        self.check_process_affinity()
        with self._mutex:
            self.global_reserved = max(0.0, round(self.global_reserved - reserved_amount, 7))
            self.global_quarantined = round(self.global_quarantined + reserved_amount, 7)

    def release(self, reserved_amount: float) -> None:
        """Release reservation without spend (e.g. pre-dispatch refusal or 429)."""
        self.check_process_affinity()
        with self._mutex:
            self.global_reserved = max(0.0, round(self.global_reserved - reserved_amount, 7))

    def reset(self) -> None:
        """Reset global budget state (for test isolation)."""
        self.check_process_affinity()
        with self._mutex:
            self.global_settled = 0.0
            self.global_quarantined = 0.0
            self.global_reserved = 0.0


@dataclass
class TaskConditionBudget:
    """Manages atomic budget reservation and settlement for a single task-condition run."""
    task_id: str = "task_default"
    condition: str = "condition_default"
    budget_limit_usd: float = 0.030
    settled_spend: float = 0.0
    quarantined_spend: float = 0.0
    outstanding_reservations: float = 0.0
    spend_uncertain: bool = False
    global_budget: Optional[GlobalExperimentBudget] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def total_committed(self) -> float:
        with self._lock:
            return round(self.settled_spend + self.quarantined_spend + self.outstanding_reservations, 7)

    @property
    def available_usd(self) -> float:
        with self._lock:
            return max(0.0, round(self.budget_limit_usd - (self.settled_spend + self.quarantined_spend + self.outstanding_reservations), 7))

    def check_and_reserve(
        self,
        prompt_tokens: int,
        max_completion_tokens: int,
        model_cost: ModelCost,
    ) -> Tuple[bool, float, str]:
        """Atomically calculate and reserve worst-case cost prior to API dispatch.

        Always uses uncached input rate for worst-case conservative reservation.
        Strictly divides token counts by 1,000,000.0.
        """
        # Worst-case cost calculation
        input_usd = (prompt_tokens / 1_000_000.0) * model_cost.input_cost_per_m
        output_usd = (max_completion_tokens / 1_000_000.0) * model_cost.output_cost_per_m
        reserved_cost = round(input_usd + output_usd, 7)

        with self._lock:
            # 1. Task-Condition Ceiling Check
            current_committed = self.settled_spend + self.quarantined_spend + self.outstanding_reservations
            projected = current_committed + reserved_cost
            if projected > self.budget_limit_usd + 1e-9:
                avail = max(0.0, round(self.budget_limit_usd - current_committed, 7))
                return (
                    False,
                    reserved_cost,
                    f"TASK_BUDGET_EXCEEDED (need ${reserved_cost:.6f}, available ${avail:.6f})",
                )

            # 2. Global Experiment Ceiling Check
            if self.global_budget is not None:
                if not self.global_budget.reserve(reserved_cost):
                    return (
                        False,
                        reserved_cost,
                        f"GLOBAL_BUDGET_EXCEEDED (need ${reserved_cost:.6f}, global available ${self.global_budget.available:.6f})",
                    )

            # 3. Commit reservation
            self.outstanding_reservations = round(self.outstanding_reservations + reserved_cost, 7)
            return (True, reserved_cost, "OK")

    def settle(
        self,
        reserved_amount: float,
        prompt_tokens: int,
        completion_tokens: int,
        model_cost: ModelCost,
        cached_prompt_tokens: int = 0,
    ) -> float:
        """Settle confirmed provider usage against the reservation."""
        actual_cost = model_cost.calculate_cost(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
        )
        with self._lock:
            self.outstanding_reservations = max(0.0, round(self.outstanding_reservations - reserved_amount, 7))
            self.settled_spend = round(self.settled_spend + actual_cost, 7)

        if self.global_budget is not None:
            self.global_budget.settle(reserved_amount, actual_cost)

        return actual_cost

    def quarantine(self, reserved_amount: float) -> None:
        """Quarantine reservation when response is uncertain (timeout post-dispatch)."""
        with self._lock:
            self.outstanding_reservations = max(0.0, round(self.outstanding_reservations - reserved_amount, 7))
            self.quarantined_spend = round(self.quarantined_spend + reserved_amount, 7)
            self.spend_uncertain = True

        if self.global_budget is not None:
            self.global_budget.quarantine(reserved_amount)

    def release(self, reserved_amount: float) -> None:
        """Release reservation when call is conclusively rejected before inference (DNS, 429)."""
        with self._lock:
            self.outstanding_reservations = max(0.0, round(self.outstanding_reservations - reserved_amount, 7))

        if self.global_budget is not None:
            self.global_budget.release(reserved_amount)

    def handle_uncertain_abort(
        self,
        reserved_amount: float,
        dispatched: bool = False,
        err: Optional[Exception] = None,
    ) -> None:
        """Handle request abortion, timeout, or cancellation.

        Edge Case Accounting Policy:
        If a request has already been dispatched over the network and task deadline,
        connection timeout, or cancellation occurs, the worst-case reservation is QUARANTINED
        unless an error conclusively establishes that the provider did not accept or execute
        the request (such as HTTP 429 or pre-connection DNS refusal).
        Ensures the reservation remains accounted for in BOTH task-condition and global budgets.
        """
        if not dispatched:
            # Pre-dispatch cancellation or refusal: safe to release
            self.release(reserved_amount)
            return

        if err is not None and is_conclusive_provider_refusal(err):
            # Conclusive provider refusal (e.g. 429 or pre-connect error): safe to release
            self.release(reserved_amount)
            return

        # Uncertain spend: provider may have received or billed request
        self.quarantine(reserved_amount)

    def quarantine_all_outstanding(self) -> float:
        """Quarantine all active outstanding reservations upon task deadline or cancellation."""
        with self._lock:
            amount = self.outstanding_reservations
            if amount > 0:
                self.outstanding_reservations = 0.0
                self.quarantined_spend = round(self.quarantined_spend + amount, 7)
                self.spend_uncertain = True
                if self.global_budget is not None:
                    self.global_budget.quarantine(amount)
                return amount
        return 0.0

