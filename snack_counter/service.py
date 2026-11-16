"""Transaction boundaries and business rules. Money is always integer minor units."""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from .schema import SCHEMA


class RuleViolation(ValueError):
    pass


class BudgetExceeded(RuleViolation):
    pass


def utc_now():
    return datetime.now(timezone.utc)


class SnackService:
    def __init__(self, db_path, clock=utc_now, offer_ttl=timedelta(hours=4),
                 commit_ttl=timedelta(hours=24)):
        self.conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.clock = clock
        if offer_ttl <= timedelta(0) or commit_ttl <= timedelta(0):
            raise RuleViolation("Deadlines must be positive")
        self.offer_ttl, self.commit_ttl = offer_ttl, commit_ttl

    def close(self):
        self.conn.close()

    @contextmanager
    def transaction(self):
        # Separate connections compete on the database, not on a Python mutex.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def now(self):
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuleViolation("Clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _money(value):
        if type(value) is not int or value <= 0:
            raise RuleViolation("Money must be a positive integer in minor units")
        return value

    def _office(self, office_id):
        row = self.conn.execute("SELECT * FROM offices WHERE id=?", (office_id,)).fetchone()
        if row is None:
            raise RuleViolation("Unknown office")
        return row

    def _refill(self, refill_id):
        row = self.conn.execute("SELECT * FROM refills WHERE id=?", (refill_id,)).fetchone()
        if row is None:
            raise RuleViolation("Unknown refill")
        return row

    def _employee(self, employee_id):
        row = self.conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
        if row is None:
            raise RuleViolation("Unknown employee")
        return row

    def _local(self, office, at=None):
        return (at or self.now()).astimezone(ZoneInfo(office["timezone_name"]))

    def _event(self, refill_id, kind, **details):
        self.conn.execute("INSERT INTO events(refill_id,kind,at,details) VALUES (?,?,?,?)",
                          (refill_id, kind, self.now().isoformat(), json.dumps(details)))

    def _notify(self, event_key, recipient, body):
        self.conn.execute("INSERT OR IGNORE INTO outbox(event_key,recipient,body) VALUES (?,?,?)",
                          (event_key, recipient, body))

    def configure_office(self, office_id, currency, monthly_budget_minor, default_cap_minor,
                         timezone_name):
        self._money(monthly_budget_minor)
        self._money(default_cap_minor)
        if currency not in ("NOK", "INR"):
            raise RuleViolation("This MVP supports NOK and INR")
        ZoneInfo(timezone_name)  # Fail configuration early if timezone data is missing.
        with self.transaction():
            self.conn.execute("INSERT INTO offices VALUES (?,?,?,?,?)",
                              (office_id, currency, monthly_budget_minor, default_cap_minor,
                               timezone_name))

    def add_employee(self, employee_id, home_office, payroll_currency):
        self._office(home_office)
        if not employee_id or payroll_currency not in ("NOK", "INR"):
            raise RuleViolation("Employee ID and supported payroll currency required")
        with self.transaction():
            self.conn.execute("INSERT INTO employees VALUES (?,?,?)",
                              (employee_id, home_office, payroll_currency))

    def set_presence(self, employee_id, office_id, on_date, available=True):
        self._employee(employee_id)
        self._office(office_id)
        date.fromisoformat(on_date)
        with self.transaction():
            self.conn.execute("INSERT INTO presence VALUES (?,?,?,?) ON CONFLICT DO UPDATE "
                              "SET available=excluded.available",
                              (employee_id, office_id, on_date, int(bool(available))))

    def get_refill(self, refill_id):
        if not self.conn.in_transaction:
            with self.transaction():
                return self.get_refill(refill_id)
        row = dict(self._refill(refill_id))
        # Receipt bytes belong in the claim, not in chat messages or reports.
        row.pop("receipt_image", None)
        row["items"] = json.loads(row["items"]) if row["items"] else []
        claim = self.conn.execute("SELECT status,external_id FROM claims WHERE refill_id=?",
                                  (refill_id,)).fetchone()
        row["claim_status"] = claim["status"] if claim else None
        row["external_claim_id"] = claim["external_id"] if claim else None
        return row

    def report_empty(self, office_id, reporter_id, cap_minor=None):
        with self.transaction():
            office = self._office(office_id)
            self._employee(reporter_id)  # Visitors may report either office.
            existing = self.conn.execute("SELECT id FROM refills WHERE office_id=? AND "
                                         "state IN ('OPEN','OFFERED','COMMITTED')",
                                         (office_id,)).fetchone()
            if existing:
                return self.get_refill(existing["id"])
            cap = self._money(office["default_cap_minor"] if cap_minor is None else cap_minor)
            refill_id = uuid4().hex
            self.conn.execute("INSERT INTO refills(id,office_id,reporter_id,state,cap_minor,"
                              "created_at) VALUES (?,?,?,'OPEN',?,?)",
                              (refill_id, office_id, reporter_id, cap, self.now().isoformat()))
            self._event(refill_id, "EMPTY_REPORTED")
            self._notify(f"{refill_id}:empty", f"office:{office_id}",
                         f"Empty snack counter recorded: {refill_id}. Wait for a buyer to commit.")
            return self.get_refill(refill_id)

    def budget(self, office_id, period):
        # Use a read transaction so spend and reserve are one consistent snapshot.
        if not self.conn.in_transaction:
            with self.transaction():
                return self.budget(office_id, period)
        office = self._office(office_id)
        row = self.conn.execute("SELECT COALESCE(SUM(actual_minor),0) spent, "
                                "COALESCE(SUM(reserved_minor),0) reserved FROM refills "
                                "WHERE office_id=? AND period=?", (office_id, period)).fetchone()
        limit = office["monthly_budget_minor"]
        return {"limit_minor": limit, "spent_minor": row["spent"],
                "reserved_minor": row["reserved"],
                "available_minor": limit - row["spent"] - row["reserved"],
                "currency": office["currency"]}

    def _eligible(self, employee_id, office):
        return self.conn.execute("SELECT e.id FROM employees e JOIN presence p "
                                 "ON p.employee_id=e.id WHERE e.id=? AND p.office_id=? "
                                 "AND p.on_date=? AND p.available=1 AND e.payroll_currency=?",
                                 (employee_id, office["id"], self._local(office).date().isoformat(),
                                  office["currency"])).fetchone() is not None

    def offer_next(self, refill_id):
        with self.transaction():
            refill = self._refill(refill_id)
            if refill["state"] != "OPEN":
                return self.get_refill(refill_id)
            office = self._office(refill["office_id"])
            period = self._local(office).strftime("%Y-%m")
            if self.budget(office["id"], period)["available_minor"] < refill["cap_minor"]:
                self._notify(f"{refill_id}:round:{refill['offer_round']}:budget:{period}",
                             f"coordinator:{office['id']}",
                             f"Refill {refill_id} blocked by monthly budget. No purchase authorized.")
                return self.get_refill(refill_id)
            # Fewest completed purchases, then least recently offered, then stable ID.
            candidate = self.conn.execute("""
                SELECT e.id FROM employees e JOIN presence p ON p.employee_id=e.id
                WHERE p.office_id=? AND p.on_date=? AND p.available=1 AND e.payroll_currency=?
                AND NOT EXISTS (SELECT 1 FROM offers a WHERE a.refill_id=? AND a.employee_id=e.id
                                AND a.offer_round=?)
                AND NOT EXISTS (SELECT 1 FROM refills r WHERE r.assignee_id=e.id
                                AND r.state IN ('OFFERED','COMMITTED','PURCHASED'))
                ORDER BY (SELECT COUNT(*) FROM refills r WHERE r.assignee_id=e.id
                          AND r.state IN ('PURCHASED','REIMBURSED')),
                         COALESCE((SELECT MAX(a.id) FROM offers a WHERE a.employee_id=e.id),0), e.id
                LIMIT 1
            """, (office["id"], self._local(office).date().isoformat(), office["currency"],
                  refill_id, refill["offer_round"])).fetchone()
            if candidate is None:
                self._notify(f"{refill_id}:round:{refill['offer_round']}:no-buyer",
                             f"coordinator:{office['id']}",
                             f"No eligible buyer for {refill_id}. Check presence, unpaid claims or declined offers.")
                return self.get_refill(refill_id)
            buyer = candidate["id"]
            now = self.now()
            self.conn.execute("UPDATE refills SET state='OFFERED',reserved_minor=cap_minor,"
                              "period=?,assignee_id=?,offered_at=?,offer_until=? WHERE id=?",
                              (period, buyer, now.isoformat(), (now+self.offer_ttl).isoformat(),
                               refill_id))
            self.conn.execute("INSERT INTO offers(refill_id,employee_id,offer_round) VALUES (?,?,?)",
                              (refill_id, buyer, refill["offer_round"]))
            self._event(refill_id, "OFFERED", employee_id=buyer, period=period)
            self._notify(f"{refill_id}:round:{refill['offer_round']}:offer:{buyer}", buyer,
                         f"Offer {refill_id}: commit before {(now+self.offer_ttl).isoformat()}. "
                         f"Cap {refill['cap_minor']/100:.2f} {office['currency']}; "
                         f"purchase authorization ends with local month {period}.")
            return self.get_refill(refill_id)

    def restart_offers(self, refill_id):
        """Coordinator action after checking availability; preserve offer history.

        Each person gets at most one offer per round. Exhausted rounds stay open
        and escalated until a human explicitly starts another round.
        """
        with self.transaction():
            refill = self._refill(refill_id)
            if refill["state"] != "OPEN":
                raise RuleViolation("Only an unassigned refill can restart offers")
            self.conn.execute("UPDATE refills SET offer_round=offer_round+1 WHERE id=?", (refill_id,))
            self._event(refill_id, "OFFER_ROUND_RESTARTED")
            return self.get_refill(refill_id)

    def commit(self, refill_id, employee_id):
        with self.transaction():
            refill = self._refill(refill_id)
            if refill["state"] == "COMMITTED" and refill["assignee_id"] == employee_id:
                return self.get_refill(refill_id)  # Duplicate click.
            if refill["state"] != "OFFERED" or refill["assignee_id"] != employee_id:
                raise RuleViolation("Only the current offered buyer may commit")
            if self.now() >= datetime.fromisoformat(refill["offer_until"]):
                raise RuleViolation("Offer expired")
            office = self._office(refill["office_id"])
            if not self._eligible(employee_id, office):
                raise RuleViolation("Buyer must still be present and have local payroll currency")
            period = self._local(office).strftime("%Y-%m")
            if period != refill["period"]:
                if self.budget(office["id"], period)["available_minor"] < refill["cap_minor"]:
                    raise BudgetExceeded("New month has insufficient budget")
            now = self.now()
            self.conn.execute("UPDATE refills SET state='COMMITTED',period=?,committed_at=?,"
                              "commit_due=? WHERE id=?",
                              (period, now.isoformat(), (now+self.commit_ttl).isoformat(), refill_id))
            self._event(refill_id, "COMMITTED", employee_id=employee_id)
            self._notify(f"{refill_id}:round:{refill['offer_round']}:committed:{employee_id}",
                         f"office:{office['id']}",
                         f"{employee_id} committed to refill {refill_id}. Do not buy a second refill.")
            return self.get_refill(refill_id)

    def _release(self, refill, reason):
        self.conn.execute("UPDATE refills SET state='OPEN',reserved_minor=0,period=NULL,"
                          "assignee_id=NULL,offered_at=NULL,offer_until=NULL,committed_at=NULL,"
                          "commit_due=NULL WHERE id=?", (refill["id"],))
        self._event(refill["id"], reason, employee_id=refill["assignee_id"])

    def release(self, refill_id, employee_id, no_purchase_confirmed=False):
        with self.transaction():
            refill = self._refill(refill_id)
            if refill["state"] not in ("OFFERED", "COMMITTED") or refill["assignee_id"] != employee_id:
                raise RuleViolation("Only the current buyer can release this refill")
            if refill["state"] == "COMMITTED" and no_purchase_confirmed is not True:
                raise RuleViolation("Confirm no purchase before releasing a committed refill")
            self._release(refill, "BUYER_RELEASED")
            return self.get_refill(refill_id)

    def tick(self):
        # A scheduler calls this periodically; no background thread is hidden here.
        with self.transaction():
            active = self.conn.execute("SELECT * FROM refills WHERE state IN ('OFFERED','COMMITTED')").fetchall()
            for refill in active:
                if refill["state"] == "OFFERED" and self.now() >= datetime.fromisoformat(refill["offer_until"]):
                    self._release(refill, "OFFER_EXPIRED")
                    self._notify(f"{refill['id']}:round:{refill['offer_round']}:expired:{refill['assignee_id']}",
                                 refill["assignee_id"],
                                 f"Offer {refill['id']} expired. Do not purchase for it.")
                elif refill["state"] == "COMMITTED" and self.now() >= datetime.fromisoformat(refill["commit_due"]):
                    message = (f"Refill {refill['id']} is overdue; confirm completion or release only "
                               "if no purchase was made. Ownership has not changed.")
                    self._notify(f"{refill['id']}:round:{refill['offer_round']}:overdue:{refill['assignee_id']}",
                                 refill["assignee_id"], message)
                    self._notify(f"{refill['id']}:round:{refill['offer_round']}:escalate:{refill['assignee_id']}",
                                 f"coordinator:{refill['office_id']}", message)
            open_ids = [r[0] for r in self.conn.execute("SELECT id FROM refills WHERE state='OPEN'")]
            # 10 days is an escalation threshold, not an invented payment SLA.
            for claim in self.conn.execute("SELECT c.*,r.purchased_at,r.office_id FROM claims c "
                                           "JOIN refills r ON r.id=c.refill_id WHERE c.status!='PAID'").fetchall():
                if self.now()-datetime.fromisoformat(claim["purchased_at"]) >= timedelta(days=10):
                    self._notify(f"{claim['refill_id']}:hr-overdue", f"coordinator:{claim['office_id']}",
                                 f"Unpaid snack claim {claim['refill_id']} is at least 10 days old; reconcile with HR.")
        for refill_id in open_ids:
            self.offer_next(refill_id)

    def purchase(self, refill_id, employee_id, amount_minor, receipt, items, purchased_at=None):
        """Buyer confirms both purchase and stocking, with the receipt attached."""
        self._money(amount_minor)
        if not isinstance(receipt, bytes) or not receipt:
            raise RuleViolation("A receipt image is required")
        if not isinstance(items, list) or not items:
            raise RuleViolation("Record at least one purchased snack")
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                    or not item["name"].strip() or type(item.get("quantity")) is not int
                    or item["quantity"] <= 0):
                raise RuleViolation("Each snack needs a name and positive integer quantity")
        purchased_at = purchased_at or self.now()
        if purchased_at.tzinfo is None or purchased_at.utcoffset() is None:
            raise RuleViolation("Purchase time must be timezone-aware")
        purchased_at = purchased_at.astimezone(timezone.utc)
        with self.transaction():
            refill = self._refill(refill_id)
            if refill["state"] != "COMMITTED" or refill["assignee_id"] != employee_id:
                raise RuleViolation("Only the committed buyer can record a purchase")
            if amount_minor > refill["reserved_minor"]:
                raise BudgetExceeded("Purchase exceeds authorized cap; contact coordinator before buying")
            office = self._office(refill["office_id"])
            if not datetime.fromisoformat(refill["committed_at"]) <= purchased_at <= self.now():
                raise RuleViolation("Purchase must be after commitment and not in the future")
            if self._local(office, purchased_at).strftime("%Y-%m") != refill["period"]:
                raise RuleViolation("Purchase is outside the authorized month; contact coordinator/finance. "
                                    "Release only if no purchase has been made")
            self.conn.execute("UPDATE refills SET state='PURCHASED',actual_minor=?,reserved_minor=0,"
                              "purchased_at=?,receipt_image=?,items=? WHERE id=?",
                              (amount_minor, purchased_at.isoformat(), receipt, json.dumps(items), refill_id))
            description = (f"Snack refill {refill_id} at {office['id']} on "
                           f"{self._local(office, purchased_at).date().isoformat()}")
            self.conn.execute("INSERT INTO claims(refill_id,status,employee_id,amount_minor,currency,"
                              "description,receipt_image,updated_at) VALUES (?,'READY',?,?,?,?,?,?)",
                              (refill_id, employee_id, amount_minor, office["currency"], description,
                               receipt, self.now().isoformat()))
            self._event(refill_id, "PURCHASE_RECORDED", amount_minor=amount_minor,
                        currency=office["currency"], items=items)
            self._notify(f"{refill_id}:purchased", f"office:{office['id']}",
                         f"Refill {refill_id} completed by {employee_id}; reimbursement queued.")
            return self.get_refill(refill_id)

    def report(self, period, reporting_currency, rates):
        # rates[source] = reporting major units / source major unit. No live FX assumptions.
        if reporting_currency not in ("NOK", "INR"):
            raise RuleViolation("Unsupported reporting currency")
        try:
            date.fromisoformat(period + "-01")
        except (ValueError, TypeError):
            raise RuleViolation("Expected YYYY-MM report period") from None
        with self.transaction():
            rows = self.conn.execute("SELECT r.*,o.currency FROM refills r JOIN offices o "
                                     "ON o.id=r.office_id WHERE r.period=? AND r.actual_minor IS NOT NULL",
                                     (period,)).fetchall()
            by_currency, by_office, snacks = {}, {}, {}
            for row in rows:
                currency, office = row["currency"], row["office_id"]
                by_currency[currency] = by_currency.get(currency, 0) + row["actual_minor"]
                by_office.setdefault(office, {"currency": currency, "spent_minor": 0, "refills": 0})
                by_office[office]["spent_minor"] += row["actual_minor"]
                by_office[office]["refills"] += 1
                for item in json.loads(row["items"]):
                    key = f"{office}:{item['name'].strip().casefold()}"
                    snacks[key] = snacks.get(key, 0) + item["quantity"]
            total = Decimal(0)
            used_rates = {}
            for currency, amount in by_currency.items():
                rate = rates.get(currency)
                if (not isinstance(rate, Decimal) or not rate.is_finite() or rate <= 0
                        or (currency == reporting_currency and rate != 1)):
                    raise RuleViolation(f"An explicit positive Decimal reporting rate is required for {currency}")
                used_rates[currency] = str(rate)
                total += amount * rate
            return {"period": period, "by_currency": by_currency, "by_office": by_office,
                    "reporting_currency": reporting_currency,
                    "total_minor": int(total.quantize(Decimal('1'), rounding=ROUND_HALF_UP)),
                    "rates": used_rates, "purchased_snack_quantities": snacks,
                    "consumption_note": "Purchased quantities and empty reports are demand proxies, not measured consumption."}
