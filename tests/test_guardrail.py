"""
Tests for agent_guardrail. Written fresh against the public package API
(Guardrail / GuardrailConfig / Decision / SpendResult) -- no code here is
copied from any internal project.

Run with:
    pip install pytest
    pytest
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from agent_guardrail import Decision, Guardrail, GuardrailConfig


def make_guardrail(tmp_path, **overrides) -> Guardrail:
    config_kwargs = dict(
        db_path=str(tmp_path / "guardrail.db"),
        starting_capital=Decimal("40.00"),
        total_budget_cap=Decimal("40.00"),
        auto_approve_max=Decimal("7.00"),
        forbidden_action_types=frozenset({"gamble"}),
    )
    config_kwargs.update(overrides)
    return Guardrail(GuardrailConfig(**config_kwargs))


# -- basic decision behavior --------------------------------------------

def test_expense_within_limits_is_approved(tmp_path):
    g = make_guardrail(tmp_path)
    result = g.request_expense(Decimal("3.00"), "api_call", "test")
    assert result.decision == Decision.APPROVED
    assert g.get_total_spent() == Decimal("3.00")


def test_expense_over_auto_approve_max_is_pending(tmp_path):
    g = make_guardrail(tmp_path)
    result = g.request_expense(Decimal("20.00"), "big_purchase", "test")
    assert result.decision == Decision.PENDING_APPROVAL
    # A pending spend is not (yet) counted as spent.
    assert g.get_total_spent() == Decimal("0.00")


def test_expense_over_budget_cap_is_denied(tmp_path):
    g = make_guardrail(tmp_path, auto_approve_max=Decimal("1000.00"))
    result = g.request_expense(Decimal("150.00"), "api_call", "test")
    assert result.decision == Decision.DENIED_BUDGET
    assert g.get_total_spent() == Decimal("0.00")


def test_forbidden_action_type_is_denied_without_touching_budget(tmp_path):
    g = make_guardrail(tmp_path)
    result = g.request_expense(Decimal("1.00"), "gamble", "test", action_type="gamble")
    assert result.decision == Decision.DENIED_FORBIDDEN
    assert g.get_total_spent() == Decimal("0.00")


def test_negative_or_zero_amounts_are_rejected(tmp_path):
    g = make_guardrail(tmp_path)
    with pytest.raises(ValueError):
        g.request_expense(Decimal("0.00"), "api_call", "test")
    with pytest.raises(ValueError):
        g.request_expense(Decimal("-5.00"), "api_call", "test")


# -- real vs. test revenue separation -------------------------------------

def test_real_and_test_revenue_are_tracked_separately(tmp_path):
    g = make_guardrail(tmp_path)
    g.record_income(Decimal("20.00"), "subscription", "customer #1", revenue_type="real")
    g.record_income(Decimal("999.00"), "stripe_payment", "sandbox checkout", revenue_type="test")

    assert g.get_real_revenue() == Decimal("20.00")
    assert g.get_test_revenue() == Decimal("999.00")
    # Test revenue must never leak into real-money calculations.
    assert g.get_net_profit() == Decimal("20.00")
    assert g.get_available_capital() == Decimal("60.00")  # 40.00 starting capital + 20.00 net profit


def test_invalid_revenue_type_is_rejected(tmp_path):
    g = make_guardrail(tmp_path)
    with pytest.raises(ValueError):
        g.record_income(Decimal("10.00"), "misc", "test", revenue_type="fake")


# -- concurrency regression test -----------------------------------------

def test_concurrent_requests_never_exceed_budget_cap(tmp_path):
    """Regression test for a check-then-act race in request_expense():
    get_total_spent() used to be read on one connection/transaction, then
    the approved expense written on a second, separate one. Two concurrent
    callers could both read the pre-spend total, both pass the
    total_budget_cap check, and both commit -- collectively spending past
    the cap. request_expense() now performs the check and the write it
    gates inside a single SQLite BEGIN IMMEDIATE transaction, which
    serializes concurrent callers around that check.

    This test fires many concurrent requests -- more than the budget can
    possibly cover -- and asserts that total spend never exceeds the cap,
    regardless of how the requests interleave.
    """
    total_budget_cap = Decimal("120.00")
    spend_amount = Decimal("10.00")
    n_requests = 25  # 25 * $10 = $250, well past a $120 cap if the race exists

    g = make_guardrail(
        tmp_path,
        total_budget_cap=total_budget_cap,
        auto_approve_max=Decimal("1000.00"),  # keep every request on the immediate-write path
    )

    barrier = threading.Barrier(n_requests)
    results: list = [None] * n_requests

    def worker(i: int) -> None:
        barrier.wait()  # release all threads together to maximize overlap
        results[i] = g.request_expense(spend_amount, "load_test", f"concurrent spend {i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_requests)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r is not None for r in results)
    approved = [r for r in results if r.decision == Decision.APPROVED]
    denied = [r for r in results if r.decision == Decision.DENIED_BUDGET]
    assert len(approved) + len(denied) == n_requests

    # The invariant that actually matters: the ledger's recorded total
    # spend must never exceed the configured cap, no matter how the
    # concurrent requests interleaved.
    assert g.get_total_spent() <= total_budget_cap
    # And it should reflect exactly the approved spends -- no lost or
    # duplicated writes from concurrent access.
    assert g.get_total_spent() == spend_amount * len(approved)
    # With a $120 cap and $10 spends, at most 12 can ever be approved.
    assert len(approved) <= 12
