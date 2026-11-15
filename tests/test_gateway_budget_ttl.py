"""Gateway budget: leaked reservations expire; reconciled-id memory is bounded."""

from __future__ import annotations

from argus.gateway.budget import BudgetGuard
from argus.gateway.config import BudgetConfig


def _guard(ttl):
    return BudgetGuard(BudgetConfig(per_task_usd=0.10, per_session_usd=1.0), reservation_ttl_seconds=ttl)


def test_leaked_reservation_expires_and_frees_budget(monkeypatch):
    import argus.gateway.budget as b

    clock = [1000.0]
    monkeypatch.setattr(b.time, "monotonic", lambda: clock[0])
    g = _guard(60.0)
    ok, _, res = g.reserve("t", estimated_cost=0.09)
    assert ok and res
    ok2, _, _ = g.reserve("t", estimated_cost=0.05)
    assert not ok2  # held by the (leaked) reservation
    clock[0] += 61.0
    ok3, _, res3 = g.reserve("t", estimated_cost=0.05)
    assert ok3 and res3


def test_late_commit_after_expiry_still_records_spend(monkeypatch):
    import argus.gateway.budget as b

    clock = [0.0]
    monkeypatch.setattr(b.time, "monotonic", lambda: clock[0])
    g = _guard(10.0)
    _, _, res = g.reserve("t", estimated_cost=0.05)
    clock[0] += 11.0
    g.check_budget("t")  # triggers expiry
    g.commit(res, "t", "gpt-4o-mini", 1000, 1000)
    assert g.get_task_spend("t") > 0
    assert g.commit(res, "t", "gpt-4o-mini", 1000, 1000) == 0.0  # no double count


def test_reconciled_id_set_is_bounded():
    g = _guard(0)
    g._MAX_RECONCILED_IDS = 5
    for _ in range(20):
        _, _, res = g.reserve("t", estimated_cost=0.0001)
        g.release(res)
    assert len(g._reconciled_reservations) <= 5
