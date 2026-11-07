"""Budget guard tracking per-task and per-session inference expenditure."""

from __future__ import annotations

import threading
import uuid
from typing import Dict, Optional, Tuple
from argus.config import MODEL_REGISTRY, ModelCost
from argus.gateway.config import BudgetConfig


def _to_micro(usd: float) -> int:
    """Convert USD float to integer micro-dollars ($1.00 = 1,000,000 micro-dollars)."""
    return int(round(usd * 1_000_000))


def _to_usd(micro: int) -> float:
    """Convert integer micro-dollars to float USD."""
    return micro / 1_000_000.0


class BudgetGuard:
    """Thread-safe budget enforcer with exact integer micro-dollar accounting and two-phase reservations."""

    def __init__(self, config: BudgetConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._session_spend_micro = 0
        self._task_spend_micro: Dict[str, int] = {}
        self._reserved_session_micro = 0
        self._task_reserved_micro: Dict[str, int] = {}
        self._uncertain_spend_micro = 0
        self._task_uncertain_micro: Dict[str, int] = {}
        self._active_reservations: Dict[str, Tuple[str, int]] = {}
        self._reconciled_reservations: set[str] = set()

    @property
    def _session_spend_usd(self) -> float:
        return _to_usd(self._session_spend_micro)

    @property
    def _reserved_session_usd(self) -> float:
        return _to_usd(self._reserved_session_micro)

    @property
    def _uncertain_spend_usd(self) -> float:
        return _to_usd(self._uncertain_spend_micro)

    @property
    def _task_spend_usd(self) -> Dict[str, float]:
        return {k: _to_usd(v) for k, v in self._task_spend_micro.items()}

    def get_session_spend(self) -> float:
        """Return total spend in current gateway session."""
        with self._lock:
            return round(_to_usd(self._session_spend_micro), 6)

    def get_uncertain_spend(self) -> float:
        """Return total potential liability from uncertain execution outcomes."""
        with self._lock:
            return round(_to_usd(self._uncertain_spend_micro), 6)

    def get_task_spend(self, run_id: str) -> float:
        """Return spend recorded for a specific task run_id."""
        with self._lock:
            return round(_to_usd(self._task_spend_micro.get(run_id, 0)), 6)

    def check_budget(self, run_id: str, estimated_cost: float = 0.0) -> Tuple[bool, str]:
        """Check if request would violate task or session budget limits using exact integer arithmetic."""
        cost_micro = _to_micro(estimated_cost)
        with self._lock:
            # 1. Session check: confirmed spend + active reservations + uncertain liabilities
            total_session_micro = (
                self._session_spend_micro
                + self._reserved_session_micro
                + self._uncertain_spend_micro
                + cost_micro
            )
            session_limit_micro = _to_micro(self.config.per_session_usd)
            if total_session_micro > session_limit_micro:
                msg = (
                    f"Session budget limit of ${self.config.per_session_usd:.4f} exceeded. "
                    f"Current spend: ${_to_usd(self._session_spend_micro):.4f} "
                    f"(reserved: ${_to_usd(self._reserved_session_micro):.4f}, "
                    f"uncertain: ${_to_usd(self._uncertain_spend_micro):.4f})."
                )
                return False, msg

            # 2. Task check
            task_spend = self._task_spend_micro.get(run_id, 0)
            task_reserved = self._task_reserved_micro.get(run_id, 0)
            task_uncertain = self._task_uncertain_micro.get(run_id, 0)
            total_task_micro = task_spend + task_reserved + task_uncertain + cost_micro
            task_limit_micro = _to_micro(self.config.per_task_usd)
            if total_task_micro > task_limit_micro:
                msg = (
                    f"Task budget limit of ${self.config.per_task_usd:.4f} exceeded for run_id '{run_id}'. "
                    f"Current spend: ${_to_usd(task_spend):.4f} "
                    f"(reserved: ${_to_usd(task_reserved):.4f}, "
                    f"uncertain: ${_to_usd(task_uncertain):.4f})."
                )
                return False, msg

            return True, ""

    def reserve(
        self,
        run_id: str,
        estimated_amount: float = 0.001,
        estimated_cost: Optional[float] = None,
    ) -> Tuple[bool, str, Optional[str]]:
        """Phase 1: Reserve budget before issuing upstream inference request."""
        amount = estimated_cost if estimated_cost is not None else estimated_amount
        amount_micro = _to_micro(amount)
        with self._lock:
            ok, msg = self.check_budget(run_id, estimated_cost=amount)
            if not ok:
                return False, msg, None

            res_id = f"res_{uuid.uuid4().hex[:8]}"
            self._reserved_session_micro += amount_micro
            self._task_reserved_micro[run_id] = self._task_reserved_micro.get(run_id, 0) + amount_micro
            self._active_reservations[res_id] = (run_id, amount_micro)
            return True, "", res_id

    def _release_locked(self, reservation_id: Optional[str]) -> None:
        """Release reservation assuming lock is already held."""
        if not reservation_id:
            return
        if reservation_id in self._active_reservations:
            run_id, amount_micro = self._active_reservations.pop(reservation_id)
            self._reserved_session_micro = max(0, self._reserved_session_micro - amount_micro)
            self._task_reserved_micro[run_id] = max(0, self._task_reserved_micro.get(run_id, 0) - amount_micro)

    def release(self, reservation_id: Optional[str]) -> None:
        """Release reservation without charging."""
        if not reservation_id:
            return
        with self._lock:
            if reservation_id in self._reconciled_reservations:
                return
            self._reconciled_reservations.add(reservation_id)
            self._release_locked(reservation_id)

    def record_uncertain(
        self,
        reservation_id: Optional[str],
        run_id: str,
        model: str,
        estimated_amount: float,
    ) -> float:
        """Record an uncertain provider execution outcome with integer micro-dollar accuracy."""
        est_micro = _to_micro(estimated_amount)
        with self._lock:
            if reservation_id:
                if reservation_id in self._reconciled_reservations:
                    return 0.0
                self._reconciled_reservations.add(reservation_id)
            self._release_locked(reservation_id)
            self._uncertain_spend_micro += est_micro
            self._task_uncertain_micro[run_id] = self._task_uncertain_micro.get(run_id, 0) + est_micro
        return estimated_amount

    def commit(
        self,
        reservation_id: Optional[str],
        run_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        is_subscription: bool = False,
    ) -> float:
        """Phase 2: Reconcile reservation with actual observed tokens and record final spend."""
        if is_subscription or (model and "antigravity" in model.lower()):
            cost = 0.0
        else:
            cost_profile = MODEL_REGISTRY.get(model, ModelCost(input_cost_per_m=0.50, output_cost_per_m=0.50))
            cost = cost_profile.calculate_cost(prompt_tokens, completion_tokens)

        cost_micro = _to_micro(cost)
        with self._lock:
            if reservation_id:
                if reservation_id in self._reconciled_reservations:
                    return 0.0
                self._reconciled_reservations.add(reservation_id)
            self._release_locked(reservation_id)
            self._session_spend_micro += cost_micro
            self._task_spend_micro[run_id] = self._task_spend_micro.get(run_id, 0) + cost_micro

        return cost

    def record_usage(
        self,
        run_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        is_subscription: bool = False,
    ) -> float:
        """Calculate and record USD cost for an execution turn (direct or legacy path)."""
        return self.commit(
            reservation_id=None,
            run_id=run_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            is_subscription=is_subscription,
        )
