# Snack Counter Automation

A take-home implementation for Framsikt's Oslo and Bengaluru snack counters. It includes working refill, assignment and budget logic; simulated chat and HR integrations; automated tests; and a short design document: [PDF](DESIGN.pdf) / [Markdown](DESIGN.md).

## Run locally

Use Python 3.11 or newer. The application uses Python's standard library plus `tzdata`, which supplies the IANA time zones on Windows and other hosts without a system time-zone database. No API keys, cloud account or database server is required.

From this directory:

```sh
python -m venv .venv
```

Activate the environment:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```sh
# macOS / Linux
. .venv/bin/activate
```

Then install the dependency, run the tests and run the demonstration:

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m snack_counter.demo
```

The demonstration uses simulated employees, receipts, notifications, HR claims and reporting rates. It contacts no external service. Its exchange rates are examples, not live rates or reimbursement policy.

To rebuild the design PDF, optionally install the document dependency and run the renderer:

```sh
python -m pip install -r requirements-docs.txt
python scripts/build_design.py
```

## Rules to look for

- Repeated empty reports converge on one refill. A transaction protects assignment and budget decisions when requests overlap.
- A dated presence record determines eligibility. Buyer selection favors volunteers who have completed fewer refills, with a deterministic tie-break. Someone with an active assignment or an unpaid purchase is ineligible.
- An offer reserves money. An unanswered offer expires and can go to someone else. After all volunteers decline or expire, the coordinator can explicitly start another offer round after checking availability. A committed buyer must confirm no purchase before releasing ownership.
- Local monthly budgets count actual spending plus reservations. Actual cost replaces the reservation, and the unused balance becomes available again.
- Receipt entry records a completed purchase/refill and queues reimbursement. The claim continues independently, so an unpaid claim does not prevent another empty report for that office.
- Claim creation is not assumed to be idempotent. An ambiguous attempt is visible and requires reconciliation. Before resetting a `SENDING` submission, stop/reconcile its worker and obtain HR evidence that no claim exists. Neither submission nor approval counts as reimbursement.
- Source currency amounts remain intact. A consolidated total requires an explicit target currency and conversion rates.

The public workflow operations are `report_empty`, `offer_next`, `commit`, `release`, `tick` and `purchase`; `restart_offers` is the coordinator action for an unassigned refill. Read the service methods and tests together: tests illustrate the preconditions and expected state transitions. A clock is supplied to the service so deadline and month-boundary tests do not sleep or depend on today's date.

## Ten-minute walkthrough

1. **Problem and choices, 1 minute:** one accountable buyer, protected volunteer cash flow and two local budgets; explain the assumptions in the design.
2. **Successful refill, 2 minutes:** run the demo and trace an empty report through offer, commitment, receipt, HR submission and confirmed payment.
3. **Ownership and handover, 2 minutes:** show the concurrent-request test, then contrast an expired offer with an overdue committed buyer. Explain why the latter needs a no-purchase confirmation.
4. **Money, 2 minutes:** show budget reservation/release, office-local month boundaries and the report's separate source totals plus disclosed reporting conversion.
5. **External failure and limitations, 3 minutes:** show the ambiguous HR submission test and persistent claim state; discuss payment evidence, visitor restrictions, manual presence and what production would require.

## Clarifications to discuss with the team

These are questions for the evaluator, recorded here rather than sent automatically. The implementation proceeds with the explicit assumptions in the design.

1. Does HR support idempotent creation, lookup by a client reference, status exports/webhooks, and a signal that money was actually paid?
2. Can HR accept an expense in a different currency from an employee's payroll? Which rate/date/fees determine the reimbursement?
3. What is the preferred reporting currency and monthly FX policy? Are budgets hard limits, and who can approve an exception?
4. Is there an existing source for office attendance and holidays, and who should receive unanswered-task or overdue-claim escalations?

## Scope

This submission implements a core service and simulations. Authentication, a deployed chat bot, a scheduler process, real HR credentials, payroll conversion, production receipt handling and a web UI are outside the prototype. Snack purchase quantities and refill frequency are demand proxies; they do not measure what individuals eat. See [DESIGN.md](DESIGN.md) for the full trade-offs and calendar rules.
