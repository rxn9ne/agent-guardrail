# agent-guardrail

A drop-in, zero-dependency Python library that hard-caps how much money an
autonomous AI agent can spend, and refuses categorically forbidden action
types — without adopting an enterprise "AI agent governance platform."

One file. Standard library only (`sqlite3`, `decimal`, `dataclasses`,
`enum`). No SaaS, no dashboard, no server, no signup.

## Why this exists

A 2026 survey of existing open-source "AI agent governance" tooling turns
up options like Microsoft's Agent Governance Toolkit, asqav, Guardrails AI,
NeMo Guardrails, and AgentMint — but they cluster into two groups:
enterprise/regulated-industry platforms with real setup cost (policy DSLs,
SDKs across five languages, dashboards), or tools focused on output
validation and signed audit receipts rather than hard spending caps. None
of them are a single-file, no-SaaS, sqlite-backed library whose entire job
is "never let this agent spend more than $X on one action, or more than $Y
in total, ever."

That's the gap this fills: a hard financial backstop for a solo developer
running a small autonomous agent, without adopting a platform.

## Install

```
pip install git+https://github.com/rxn9ne/agent-guardrail.git
```

or clone it and install locally:

```
git clone https://github.com/rxn9ne/agent-guardrail.git
cd agent-guardrail
pip install .
```

(Not yet on PyPI — that's a reasonable next step once there's real usage.)

## Quickstart

```python
from decimal import Decimal
from agent_guardrail import Guardrail, GuardrailConfig, Decision

guardrail = Guardrail(GuardrailConfig(
    db_path="my_agent_ledger.db",
    starting_capital=Decimal("250.00"),
    total_budget_cap=Decimal("250.00"),
    auto_approve_max=Decimal("10.00"),
    forbidden_action_types=frozenset({"gamble", "borrow", "trade_crypto"}),
))

result = guardrail.request_expense(Decimal("3.50"), "api_call", "embeddings run")

if result.decision == Decision.APPROVED:
    call_the_paid_api()
elif result.decision == Decision.PENDING_APPROVAL:
    notify_human_owner(result.reason)   # stop -- wait for a person
else:
    log_and_skip(result.reason)         # DENIED_BUDGET / DENIED_FORBIDDEN -- stop, no workaround
```

Every call is deterministic Python against a local SQLite file — nothing
here calls out to an LLM to decide whether a spend is allowed, and nothing
in your agent's own reasoning can raise the limits at runtime.

### Recording income (with real-vs-test separation built in)

If your agent's product takes payments, and you test it with something like
Stripe's test mode before going live, you don't want a sandbox payment
silently inflating real numbers:

```python
# A confirmed real payment (e.g. from a verified Stripe webhook in live mode)
guardrail.record_income(Decimal("29.00"), "subscription", "customer #14", revenue_type="real")

# A sandbox/test-mode payment -- tracked, but excluded from every real calculation
guardrail.record_income(Decimal("1.00"), "stripe_payment", "sandbox checkout", revenue_type="test")

guardrail.get_real_revenue()        # only counts revenue_type="real"
guardrail.get_test_revenue()        # only counts revenue_type="test" -- never mixed in
guardrail.get_net_profit()          # real revenue minus expenses -- test revenue has zero effect
guardrail.get_available_capital()   # starting capital + net profit -- test revenue has zero effect
```

### Checking your position at any time

```python
guardrail.get_real_revenue()
guardrail.get_test_revenue()
guardrail.get_total_spent()
guardrail.get_net_profit()
guardrail.get_available_capital()
guardrail.get_remaining_budget()
```

## What's in the box

- `GuardrailConfig` — your limits, injected, not hard-coded: starting
  capital, total budget cap, per-action auto-approve ceiling, forbidden
  action types, db path.
- `Guardrail` — the enforcement object: `request_expense(...)` returns
  `APPROVED` / `PENDING_APPROVAL` / `DENIED_BUDGET` / `DENIED_FORBIDDEN`.
  The budget-cap check and the write it gates run inside one SQLite
  transaction, so concurrent callers can't collectively spend past the
  cap (see "Concurrency and trust boundaries" below).
- `record_income(..., revenue_type="real"|"test")` — real and test revenue
  are tracked separately from the start, so sandbox/payment-testing traffic
  can never be mistaken for real income in your agent's own decisions.

## Concurrency and trust boundaries

Three things worth understanding before you rely on this in production:

- **Budget-cap checks are atomic.** `request_expense()` reads the current
  total spent and writes its decision (an approved expense, or a queued
  pending-spend) inside a single SQLite `BEGIN IMMEDIATE` transaction.
  That serializes concurrent callers — multiple threads, or multiple
  processes sharing the same `db_path` — around the check, so two callers
  can never both see "there's room" and then both commit, collectively
  exceeding `total_budget_cap`. This does not extend to amounts sitting in
  `PENDING_APPROVAL`: those aren't counted in `get_total_spent()` until
  your application separately records them as an actual expense after a
  human approves them, so your approval step should re-check remaining
  budget at approval time, not just trust the original request.
- **`PENDING_APPROVAL` is a queue entry, not a workflow.** `request_expense()`
  inserts a row and returns — it does not notify anyone, does not poll for
  a decision, and does not execute the spend once a human approves it.
  The actual approval-and-execution step (an admin UI, a CLI, a Slack
  button, whatever fits your project) is on you to build.
- **`record_income(revenue_type="real")` trusts its caller.** It classifies
  a transaction for reporting and budget purposes only — it does not
  itself verify that a real payment happened, for that amount, from a
  verified source. Only call it with `revenue_type="real"` after your own
  code has independently verified the payment (e.g. checked a payment
  provider's webhook signature in live mode); never from unauthenticated
  input.

## What's deliberately not in here (yet)

- No PyPI package yet, no CI.
- No non-financial approval queue (for things like "publish this page live"
  that aren't a dollar amount) — out of scope for a pure spending guardrail;
  a reasonable v2 if there's real interest.
- Single-currency, single-process (SQLite, not built for a distributed
  agent fleet sharing one budget).

## Running tests

```
pip install -e ".[dev]"
pytest
```

Includes a concurrency regression test that fires many simultaneous
`request_expense()` calls and asserts total spend never exceeds the
configured cap.

## Status

v0.1.0 — extracted and generalized from a working internal project (27
passing tests on the original), then generalized and smoke-tested
standalone. Early. Feedback, issues, and PRs welcome.

## License

MIT — see [LICENSE](LICENSE).
