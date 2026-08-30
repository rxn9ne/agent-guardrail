"""
agent-guardrail — a drop-in, zero-dependency spending & forbidden-action
guardrail for solo/indie AI agent builders.

Extracted and generalized from a working, tested internal project (27
passing tests on the original). This version is parameterized by a
GuardrailConfig instead of a fixed config module, so any project can drop
this file in and configure its own limits without forking the code.

Design goals:
  1. Deterministic. No LLM call can change what this computes or decides.
  2. A hard per-action auto-approve ceiling, a hard total budget cap, and a
     categorical forbidden-action list -- all human-configured, never
     agent-editable at runtime.
  3. Zero third-party dependencies -- stdlib only (sqlite3, decimal,
     dataclasses, enum).
  4. Safe under concurrent callers: request_expense()'s budget-cap check
     and the write it gates run inside one SQLite IMMEDIATE transaction,
     so two concurrent calls cannot both see "there's room" and then both
     commit an expense that, together, exceeds total_budget_cap. See
     "Concurrency" below.

Usage sketch:

    from agent_guardrail import Guardrail, GuardrailConfig
    from decimal import Decimal

    g = Guardrail(GuardrailConfig(
        db_path="my_agent_ledger.db",
        starting_capital=Decimal("250.00"),
        total_budget_cap=Decimal("250.00"),
        auto_approve_max=Decimal("10.00"),
        forbidden_action_types=frozenset({"gamble", "borrow", ...}),
    ))

    result = g.request_expense(Decimal("3.50"), "api_call", "embeddings run")
    if result.decision == Decision.APPROVED:
        ...  # do the thing
    elif result.decision == Decision.PENDING_APPROVAL:
        ...  # stop, wait for a human -- see "Pending approvals" below
    else:
        ...  # denied -- stop, do not look for a workaround

Concurrency:
    request_expense() performs its budget-cap check and the database write
    it gates (either an approved expense or a queued pending-spend row)
    inside a single SQLite `BEGIN IMMEDIATE` transaction. That serializes
    concurrent callers -- multiple threads, or multiple separate processes
    sharing the same db_path -- around the check, so two callers can never
    both observe "there's room under the cap" and then both commit an
    expense that, combined, pushes total spend past total_budget_cap. A
    caller that has to wait for another's transaction to finish will block
    (up to the connection `timeout`, currently 30s) rather than raise.
    This guarantee covers get_total_spent() vs. immediately-approved
    expenses only -- see "Pending approvals" for what it does NOT cover.

Pending approvals:
    PENDING_APPROVAL is a queue entry, nothing more. request_expense()
    inserts a row into `pending_spends` and returns; it does not notify
    anyone, does not poll for a decision, and does not itself execute the
    spend once a human approves it. Building the actual
    human-approval-and-execution step (an admin UI, a CLI, a Slack button,
    whatever fits your project) is the calling application's job. Also
    note that a pending amount is not reserved against total_budget_cap:
    it is not counted by get_total_spent() until (and unless) your
    application separately records it as a real expense after a human
    approves it, so concurrently-queued pending amounts can, in total,
    exceed what would fit under the cap if all were later approved --
    your approval step is responsible for re-checking room at approval
    time, not just at request time.

Trust boundary on income:
    record_income(..., revenue_type="real") only classifies a transaction
    for reporting/budget purposes -- it does not independently verify
    that a real payment occurred. It trusts the caller. Only pass
    revenue_type="real" after your own code has independently verified
    the payment (e.g. checked a payment provider's webhook signature in
    live mode); never in response to unauthenticated input or an
    unverified callback. See Guardrail.record_income for details.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Optional


class Decision(str, Enum):
    APPROVED = "APPROVED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    DENIED_BUDGET = "DENIED_BUDGET"
    DENIED_FORBIDDEN = "DENIED_FORBIDDEN"


@dataclass(frozen=True)
class SpendResult:
    decision: Decision
    amount: Decimal
    reason: str
    transaction_id: Optional[int] = None


@dataclass(frozen=True)
class GuardrailConfig:
    db_path: str = "guardrail.db"
    starting_capital: Decimal = Decimal("0.00")
    total_budget_cap: Decimal = Decimal("0.00")
    auto_approve_max: Decimal = Decimal("0.00")
    currency: str = "USD"
    forbidden_action_types: frozenset[str] = field(default_factory=frozenset)
    forbidden_action_reason: str = (
        "This action type is categorically forbidden by this agent's "
        "GuardrailConfig and cannot be approved programmatically."
    )


def _cents(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value())


def _dollars(cents: int) -> Decimal:
    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))


class Guardrail:
    """One instance per agent/project. Thin wrapper around a SQLite ledger
    with hard spending limits and real/test revenue separation built in.

    Real-vs-test revenue separation (record_income(..., revenue_type=)) is
    included because payment-sandbox testing (e.g. Stripe test mode) is
    common enough for agent-built products that a sandbox payment should
    never be able to silently count as real income or move real numbers.
    """

    def __init__(self, config: GuardrailConfig):
        self.config = config
        self._db_path = Path(config.db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None puts the connection in autocommit mode, so we
        # control transactions ourselves with explicit BEGIN/COMMIT/ROLLBACK
        # (needed by request_expense() to run its check-then-write as one
        # atomic SQLite IMMEDIATE transaction). `timeout` is how long a
        # caller will wait for another connection's write lock to clear
        # before raising sqlite3.OperationalError, rather than failing fast.
        return sqlite3.connect(self._db_path, timeout=30.0, isolation_level=None)

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
                    amount_cents INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    revenue_type TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_spends (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING'
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    # -- reads --------------------------------------------------------

    def get_real_revenue(self) -> Decimal:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_cents),0) FROM transactions WHERE kind='income' AND revenue_type='real'"
            ).fetchone()
            return _dollars(row[0])
        finally:
            conn.close()

    def get_test_revenue(self) -> Decimal:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_cents),0) FROM transactions WHERE kind='income' AND revenue_type='test'"
            ).fetchone()
            return _dollars(row[0])
        finally:
            conn.close()

    def get_total_spent(self) -> Decimal:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_cents),0) FROM transactions WHERE kind='expense'"
            ).fetchone()
            return _dollars(row[0])
        finally:
            conn.close()

    def get_net_profit(self) -> Decimal:
        return self.get_real_revenue() - self.get_total_spent()

    def get_available_capital(self) -> Decimal:
        return self.config.starting_capital + self.get_net_profit()

    def get_remaining_budget(self) -> Decimal:
        return self.config.total_budget_cap - self.get_total_spent()

    # -- writes ---------------------------------------------------------

    def record_income(self, amount: Decimal, category: str, description: str,
                       revenue_type: str = "real") -> int:
        """Record an income transaction as "real" or "test" revenue.

        This call only classifies the transaction for reporting and budget
        purposes (get_real_revenue(), get_net_profit(), etc.) -- it does
        NOT independently verify that a payment actually happened, that it
        was for this amount, or that it came from a verified source.
        revenue_type="real" is trusted input: it is the caller's
        responsibility to call this with revenue_type="real" only after
        independently verifying the payment (e.g. after checking a payment
        provider's webhook signature in live mode), and never from
        unauthenticated user input or an unverified callback. Anything
        that hasn't been independently verified should be recorded with
        revenue_type="test" (or not recorded until it is verified).
        """
        if amount <= 0:
            raise ValueError("income amount must be positive")
        if revenue_type not in ("real", "test"):
            raise ValueError("revenue_type must be 'real' or 'test'")
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO transactions (ts, kind, amount_cents, category, description, revenue_type) "
                "VALUES (?, 'income', ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), _cents(amount), category, description, revenue_type),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def request_expense(self, amount: Decimal, category: str, description: str,
                         action_type: str = "generic_expense") -> SpendResult:
        """Ask permission to spend `amount`.

        The budget-cap check and the write it gates (an approved expense,
        or a queued pending-spend row) run inside one SQLite `BEGIN
        IMMEDIATE` transaction, so concurrent callers cannot each read the
        pre-spend total, both pass the check, and then both commit --
        collectively exceeding total_budget_cap. See the module docstring
        ("Concurrency") for the exact guarantee and its limits, and
        ("Pending approvals") for what PENDING_APPROVAL does and does not
        do -- in particular, this library never executes anything itself
        once a human approves a pending spend.
        """
        if amount <= 0:
            raise ValueError("expense amount must be positive")

        if action_type in self.config.forbidden_action_types:
            return SpendResult(Decision.DENIED_FORBIDDEN, amount, self.config.forbidden_action_reason)

        conn = self._connect()
        try:
            # BEGIN IMMEDIATE grabs SQLite's write lock right away (rather
            # than only when the first write statement runs), so a second
            # concurrent caller's BEGIN IMMEDIATE blocks here until this
            # transaction commits or rolls back. That is what makes the
            # read-total -> compare-to-cap -> write sequence below atomic
            # across concurrent callers instead of a check-then-act race.
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) FROM transactions WHERE kind='expense'"
                ).fetchone()
                total_spent = _dollars(row[0])

                if total_spent + amount > self.config.total_budget_cap:
                    conn.execute("ROLLBACK")
                    return SpendResult(
                        Decision.DENIED_BUDGET, amount,
                        f"Would exceed total budget cap of {self.config.currency} {self.config.total_budget_cap}.",
                    )

                if amount > self.config.auto_approve_max:
                    cur = conn.execute(
                        "INSERT INTO pending_spends (ts, amount_cents, category, description) VALUES (?, ?, ?, ?)",
                        (datetime.now(timezone.utc).isoformat(), _cents(amount), category, description),
                    )
                    conn.execute("COMMIT")
                    return SpendResult(
                        Decision.PENDING_APPROVAL, amount,
                        f"Exceeds auto-approve threshold of {self.config.currency} {self.config.auto_approve_max}; "
                        f"queued for human approval (id={cur.lastrowid}). This only queues the request -- a human "
                        f"still has to approve it, and something outside this library still has to execute it.",
                        transaction_id=cur.lastrowid,
                    )

                cur = conn.execute(
                    "INSERT INTO transactions (ts, kind, amount_cents, category, description, revenue_type) "
                    "VALUES (?, 'expense', ?, ?, ?, NULL)",
                    (datetime.now(timezone.utc).isoformat(), _cents(amount), category, description),
                )
                conn.execute("COMMIT")
                return SpendResult(Decision.APPROVED, amount, "Within auto-approve threshold and budget cap.",
                                    transaction_id=cur.lastrowid)
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
