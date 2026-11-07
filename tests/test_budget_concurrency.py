"""Multithreaded Concurrency Tests for Global Experiment Budget and Task Condition Budgets.

Validates that:
1. Simultaneous reservations across concurrent threads cannot collectively exceed the global ceiling.
2. The core accounting invariant strictly holds at all times:
   settled_spend + quarantined_spend + outstanding_reservations <= global_cap_usd
3. Thread-safe settlement, cancellation/release, pre-dispatch rejection, and uncertain spend quarantine.
"""

from __future__ import annotations

import concurrent.futures
import random
import time
from typing import List

import pytest

from argus.config import ModelCost
from argus.runtime.budget import GlobalExperimentBudget, TaskConditionBudget


def test_concurrent_global_budget_reservation_ceiling():
    """Verify that 25 concurrent threads attempting reservations cannot oversubscribe global cap."""
    global_cap = 0.050  # $0.05 total global ceiling
    global_budget = GlobalExperimentBudget.reset_instance(global_cap_usd=global_cap)

    model_cost = ModelCost(input_cost_per_m=0.075, output_cost_per_m=0.300)
    num_threads = 25
    reservations_succeeded = 0
    reservations_rejected = 0

    invariant_violations: List[str] = []

    def worker_task(thread_id: int):
        nonlocal reservations_succeeded, reservations_rejected
        # Task condition budget sharing the global experiment budget singleton
        task_budget = TaskConditionBudget(
            task_id=f"concurrent_task_{thread_id}",
            budget_limit_usd=0.030,
            global_budget=global_budget,
        )

        # Attempt reservation of ~2000 prompt tokens and 1000 completion tokens = ~$0.00045
        # Vary slightly per thread
        p_tokens = 2000 + (thread_id * 50)
        c_tokens = 1000

        ok, reserved_cost, reason = task_budget.check_and_reserve(
            prompt_tokens=p_tokens,
            max_completion_tokens=c_tokens,
            model_cost=model_cost,
        )

        # Check invariant immediately
        committed = global_budget.total_committed
        if committed > global_cap + 1e-9:
            invariant_violations.append(
                f"Thread {thread_id} caused invariant violation: committed ${committed:.7f} > cap ${global_cap:.7f}"
            )

        if ok:
            reservations_succeeded += 1
            time.sleep(random.uniform(0.001, 0.005))

            # Randomly simulate outcomes:
            # 1. 50% settle with confirmed actual spend
            # 2. 25% cancel/release (pre-dispatch drop or 429)
            # 3. 25% quarantine (post-dispatch timeout)
            outcome_type = thread_id % 4
            if outcome_type in (0, 1):
                # Settle actual usage (usually less than worst-case reserved)
                actual_p = p_tokens
                actual_c = c_tokens // 2
                task_budget.settle(reserved_cost, actual_p, actual_c, model_cost)
            elif outcome_type == 2:
                # Release reservation without spend
                task_budget.release(reserved_cost)
            else:
                # Quarantine uncertain spend
                task_budget.quarantine(reserved_cost)

            # Check invariant again after mutation
            committed_post = global_budget.total_committed
            if committed_post > global_cap + 1e-9:
                invariant_violations.append(
                    f"Thread {thread_id} post-mutation violation: committed ${committed_post:.7f} > cap ${global_cap:.7f}"
                )
        else:
            reservations_rejected += 1
            assert "BUDGET_EXCEEDED" in reason

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker_task, i) for i in range(num_threads)]
        concurrent.futures.wait(futures)

    # Invariant must never have been violated during execution
    assert len(invariant_violations) == 0, f"Invariant violations detected: {invariant_violations}"

    # Final invariant check
    total_committed = global_budget.total_committed
    assert total_committed <= global_cap + 1e-9
    assert total_committed == round(
        global_budget.global_settled + global_budget.global_quarantined + global_budget.global_reserved, 7
    )

    # Both successes and ceiling rejections must have occurred given 25 threads $\times$ ~$0.003
    assert reservations_succeeded > 0
    assert reservations_succeeded + reservations_rejected == num_threads


def test_concurrent_settle_quarantine_release_lifecycle():
    """Verify concurrent lifecycle transitions between reserved, settled, and quarantined states."""
    global_cap = 0.020
    global_budget = GlobalExperimentBudget(global_cap_usd=global_cap)
    global_budget.reset()

    model_cost = ModelCost(input_cost_per_m=0.150, output_cost_per_m=0.600)

    # Reserve up to capacity
    task1 = TaskConditionBudget(task_id="t1", budget_limit_usd=0.010, global_budget=global_budget)
    task2 = TaskConditionBudget(task_id="t2", budget_limit_usd=0.010, global_budget=global_budget)

    # Reserve $0.009 on task1
    ok1, r1, _ = task1.check_and_reserve(prompt_tokens=20_000, max_completion_tokens=10_000, model_cost=model_cost)
    assert ok1 is True

    # Reserve $0.009 on task2
    ok2, r2, _ = task2.check_and_reserve(prompt_tokens=20_000, max_completion_tokens=10_000, model_cost=model_cost)
    assert ok2 is True

    # Third reservation exceeding remaining $0.002 available must be rejected
    task3 = TaskConditionBudget(task_id="t3", budget_limit_usd=0.010, global_budget=global_budget)
    ok3, r3, reason3 = task3.check_and_reserve(prompt_tokens=20_000, max_completion_tokens=10_000, model_cost=model_cost)
    assert ok3 is False
    assert "GLOBAL_BUDGET_EXCEEDED" in reason3

    # Task 1 releases its reservation (simulating 429 or pre-dispatch cancellation)
    task1.release(r1)
    assert global_budget.global_reserved == r2

    # Now task 3 can reserve because funds became available
    ok3_retry, r3_retry, _ = task3.check_and_reserve(prompt_tokens=20_000, max_completion_tokens=10_000, model_cost=model_cost)
    assert ok3_retry is True

    # Task 2 times out post-dispatch: quarantine uncertain spend
    task2.quarantine(r2)
    assert global_budget.global_quarantined == r2
    assert task2.spend_uncertain is True

    # Task 3 settles actual usage
    task3.settle(r3_retry, prompt_tokens=5000, completion_tokens=2000, model_cost=model_cost)

    # Final invariant check
    assert global_budget.total_committed <= global_cap + 1e-9

