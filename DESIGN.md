# Snack Counter Automation

Framsikt | Design brief

**One accountable buyer. Protected volunteer cash flow. Visible spending.**

Python owns the workflow; SQLite persists decisions. Chat and HR are simulated adapters. HR owns approval and payment.

## Offices at a glance

| Office | Volunteers | Monthly budget | Typical purchase |
| --- | ---: | ---: | ---: |
| Oslo | ~15 | 6,000 NOK | 300–1,200 NOK |
| Bengaluru | ~20 | 15,000 INR | 500–3,000 INR |

## Refill lifecycle

```mermaid
flowchart LR
    A["OPEN<br/>Empty reported"] --> B["OFFERED<br/>Reserve cap · 4h"]
    B -->|Buyer accepts| C["COMMITTED<br/>24h completion target"]
    C -->|Buy + stock + receipt| D["PURCHASED<br/>Queue HR claim"]
    D --> E["SUBMITTED<br/>HR claim ID"]
    E --> F["APPROVED<br/>Still unpaid"]
    F -->|Payment evidence| G["REIMBURSED"]
```

- **Choose fairly:** present that office/day, matching payroll currency, no active assignment or unpaid purchase. Rank by fewest completed refills, oldest offer, then employee ID.
- **Record once:** buyer confirms stocking and supplies cost, receipt image, items/quantities and purchase time. Atomically replace reservation with spend, release the remainder and queue HR. Another empty report may proceed independently.
- **Confirm payment:** HR receives amount/currency, description, receipt and employee ID. Claim states: **READY → SENDING → SUBMITTED → APPROVED → PAID**. Approval alone is not reimbursement.

## Responsibilities and data

| Part | Responsibility and retained data |
| --- | --- |
| Workflow + SQLite | Offices/budgets/time zones; employees/payroll; daily presence; refill owner/cap/deadlines; purchase amount/date/items/receipt; audit events. |
| Chat + outbox | Queue notifications with decisions; deliver separately. Stable event IDs deduplicate simulated retries. |
| HR adapter | Frozen request, submission state, external claim ID and reconciliation/payment evidence. |
| Monthly report | Office/currency totals, disclosed reporting FX and snack quantities. |

## Money and reporting

- **Atomic budget gate:** purchased + reserved + requested cap ≤ office monthly budget. Actual purchase must fit its cap. Store integer minor units; never add NOK and INR directly.
- **Office-local months:** offers reserve the current month. Acceptance across month-end must move the reservation successfully. Purchases must follow acceptance and fall within their authorized month; late receipt entry preserves the purchase month.
- **Consolidation:** retain source totals; convert using explicit positive decimal rates and a declared reporting currency. Demo rates are illustrative. FX never changes local budgets or claims.

## Problems → guardrails

| Problem | Rule |
| --- | --- |
| Empty for days | Named buyer, timed offers, coordinator escalation. |
| Duplicate buying | One unfinished refill per office; database uniqueness/transactions; only assignee commits. |
| Uneven effort; slow repayment | Fair rotation; skip unpaid volunteers; immediate HR queue; escalate unresolved claims after ten days. |
| Unknown spend/demand | Consolidated report; quantities/refill frequency are demand proxies, not measured consumption. |

## Exceptions and recovery

- **Unaccepted after 4h:** release reservation; try another volunteer. One offer per person/round; after exhaustion, coordinator checks availability and explicitly restarts offers.
- **Committed beyond 24h:** remind/escalate, retain ownership. Reassign only after buyer confirms no purchase; someone may already be shopping.
- **No buyer/budget:** leave open; notify coordinator. Above-cap or unauthorized-month purchases require finance handling outside this prototype.
- **HR timeout/crash:** persist SENDING before contact; hold uncertainty for reconciliation without blind retries. Match the external claim or obtain absence evidence. Stop/reconcile its worker before resetting SENDING.

## Assumptions and trade-offs

- Coordinator-maintained daily presence covers hybrid work/holidays. Visitors may report; matching-payroll-currency volunteers buy until HR's visitor FX policy is confirmed.
- HR guarantees creation only. Simulated approval/payment confirmations represent finance evidence or future supported integrations. Submission cannot guarantee faster payment.
- Timers need scheduler invocations. SQLite fits ~35 volunteers. Protecting unpaid volunteers can leave tasks open and escalated.
- **Production additions:** company-login authorization, coordinator permissions, scheduler, authenticated adapters, receipt access/retention controls and monitoring.
- **Deliberately omit:** microservices, sensors, OCR, individual consumption tracking, automatic purchasing, payroll FX and a custom reimbursement engine.
