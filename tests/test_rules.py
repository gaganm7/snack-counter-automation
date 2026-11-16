"""Behavioral rules, including races against a real SQLite database.

Amounts are integer minor units: 100 ore = 1 NOK; 100 paise = 1 INR.
The fixed dates and reporting rates are test data, not current market rates.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import threading
import unittest

from snack_counter.service import BudgetExceeded, RuleViolation, SnackService


UTC = timezone.utc


class SnackRulesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = str(Path(self.directory.name) / "snacks.sqlite3")
        self.now = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)
        self.service = SnackService(self.db_path, clock=lambda: self.now)
        self.addCleanup(self.service.close)
        self.service.configure_office("oslo", "NOK", 600_000, 120_000, "Europe/Oslo")
        self.service.configure_office(
            "bengaluru", "INR", 1_500_000, 300_000, "Asia/Kolkata"
        )
        for employee_id, office, currency in [
            ("o_alex", "oslo", "NOK"),
            ("o_bea", "oslo", "NOK"),
            ("i_asha", "bengaluru", "INR"),
            ("i_bala", "bengaluru", "INR"),
        ]:
            self.service.add_employee(employee_id, office, currency)
            self.present(employee_id, office)

    def present(self, employee_id, office, available=True):
        # All test dates are in Oslo's winter time; no machine-local timezone.
        offset = timedelta(hours=1) if office == "oslo" else timedelta(hours=5, minutes=30)
        on_date = self.now.astimezone(timezone(offset)).date().isoformat()
        self.service.set_presence(employee_id, office, on_date, available=available)

    def offer(self, office="oslo", cap_minor=None):
        reporter = "o_alex" if office == "oslo" else "i_asha"
        refill = self.service.report_empty(office, reporter, cap_minor=cap_minor)
        return self.service.offer_next(refill["id"])

    def committed(self, office="oslo", cap_minor=None):
        refill = self.offer(office, cap_minor)
        return self.service.commit(refill["id"], refill["assignee_id"])

    def purchased(self, office="oslo", amount_minor=30_000, cap_minor=None):
        refill = self.committed(office, cap_minor)
        return self.service.purchase(
            refill["id"],
            refill["assignee_id"],
            amount_minor,
            receipt=b"simulated receipt image",
            items=[{"name": "nuts", "quantity": 2}],
        )

    def test_core_lifecycle_records_spend_and_releases_unused_reservation(self):
        refill = self.committed()
        self.assertEqual("COMMITTED", refill["state"])
        before = self.service.budget("oslo", "2026-11")
        self.assertEqual(120_000, before["reserved_minor"])
        self.assertEqual(0, before["spent_minor"])

        result = self.service.purchase(
            refill["id"], refill["assignee_id"], 35_000,
            receipt=b"receipt", items=[{"name": "fruit", "quantity": 4}],
        )

        self.assertEqual("PURCHASED", result["state"])
        after = self.service.budget("oslo", "2026-11")
        self.assertEqual(35_000, after["spent_minor"])
        self.assertEqual(0, after["reserved_minor"])
        self.assertEqual(565_000, after["available_minor"])

    def test_repeated_empty_reports_share_one_active_refill_and_reservation(self):
        first = self.offer()
        second = self.service.report_empty("oslo", "o_bea")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["assignee_id"], second["assignee_id"])
        self.assertEqual(120_000, self.service.budget("oslo", "2026-11")["reserved_minor"])

    def test_only_offered_buyer_can_commit(self):
        offered = self.offer()
        stranger = "o_bea" if offered["assignee_id"] == "o_alex" else "o_alex"
        with self.assertRaises(RuleViolation):
            self.service.commit(offered["id"], stranger)
        current = self.service.get_refill(offered["id"])
        self.assertEqual("OFFERED", current["state"])
        self.assertEqual(offered["assignee_id"], current["assignee_id"])

    def test_concurrent_reports_from_separate_connections_deduplicate(self):
        barrier = threading.Barrier(2)

        def report(employee_id):
            service = SnackService(self.db_path, clock=lambda: self.now)
            try:
                barrier.wait(timeout=5)
                return service.report_empty("oslo", employee_id)["id"]
            finally:
                service.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(report, "o_alex")
            second = pool.submit(report, "o_bea")
            self.assertEqual(first.result(timeout=10), second.result(timeout=10))

    def test_concurrent_buyers_cannot_both_commit(self):
        offered = self.offer()
        other = "o_bea" if offered["assignee_id"] == "o_alex" else "o_alex"
        barrier = threading.Barrier(2)

        def commit(employee_id):
            service = SnackService(self.db_path, clock=lambda: self.now)
            try:
                barrier.wait(timeout=5)
                try:
                    return service.commit(offered["id"], employee_id)["assignee_id"]
                except RuleViolation:
                    return None
            finally:
                service.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(commit, [offered["assignee_id"], other]))
        self.assertEqual([offered["assignee_id"]], [x for x in results if x is not None])
        self.assertEqual("COMMITTED", self.service.get_refill(offered["id"])["state"])
        self.assertEqual(120_000, self.service.budget("oslo", "2026-11")["reserved_minor"])

    def test_concurrent_offer_workers_preserve_one_owner_and_reservation(self):
        refill = self.service.report_empty("oslo", "o_alex")
        barrier = threading.Barrier(2)

        def offer():
            service = SnackService(self.db_path, clock=lambda: self.now)
            try:
                barrier.wait(timeout=5)
                return service.offer_next(refill["id"])
            finally:
                service.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(offer) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual("OFFERED", results[0]["state"])
        self.assertEqual(results[0]["assignee_id"], results[1]["assignee_id"])
        self.assertEqual(120_000, self.service.budget("oslo", "2026-11")["reserved_minor"])

    def test_purchase_budget_and_claim_intent_survive_database_reopen(self):
        purchased = self.purchased()
        budget = self.service.budget("oslo", "2026-11")
        self.service.close()
        self.service = SnackService(self.db_path, clock=lambda: self.now)
        self.addCleanup(self.service.close)
        restored = self.service.get_refill(purchased["id"])
        self.assertEqual("PURCHASED", restored["state"])
        self.assertEqual("READY", restored["claim_status"])
        self.assertEqual(purchased["assignee_id"], restored["assignee_id"])
        self.assertEqual([{"name": "nuts", "quantity": 2}], restored["items"])
        self.assertNotIn("receipt_image", restored)
        self.assertEqual(budget, self.service.budget("oslo", "2026-11"))

    def test_expired_offer_is_handed_to_someone_else_and_stale_acceptance_fails(self):
        offered = self.offer()
        self.now += timedelta(hours=4, seconds=1)
        self.service.tick()
        reassigned = self.service.get_refill(offered["id"])
        self.assertEqual("OFFERED", reassigned["state"])
        self.assertNotEqual(offered["assignee_id"], reassigned["assignee_id"])
        with self.assertRaises(RuleViolation):
            self.service.commit(offered["id"], offered["assignee_id"])
        self.service.commit(reassigned["id"], reassigned["assignee_id"])

    def test_overdue_committed_buyer_is_not_silently_replaced(self):
        committed = self.committed()
        self.now += timedelta(hours=25)
        self.service.tick()
        current = self.service.get_refill(committed["id"])
        self.assertEqual("COMMITTED", current["state"])
        self.assertEqual(committed["assignee_id"], current["assignee_id"])
        with self.assertRaises(RuleViolation):
            self.service.release(committed["id"], committed["assignee_id"])

    def test_confirmed_no_purchase_releases_committed_refill_for_reassignment(self):
        committed = self.committed()
        result = self.service.release(
            committed["id"], committed["assignee_id"], no_purchase_confirmed=True
        )
        self.assertEqual("OPEN", result["state"])
        replacement = self.service.offer_next(committed["id"])
        self.assertEqual("OFFERED", replacement["state"])
        self.assertNotEqual(committed["assignee_id"], replacement["assignee_id"])

    def test_coordinator_can_restart_exhausted_offers_without_erasing_history(self):
        first = self.offer()
        self.service.release(first["id"], first["assignee_id"])
        second = self.service.offer_next(first["id"])
        self.assertNotEqual(first["assignee_id"], second["assignee_id"])
        self.service.release(second["id"], second["assignee_id"])
        exhausted = self.service.offer_next(first["id"])
        self.assertEqual("OPEN", exhausted["state"])
        history = [tuple(row) for row in self.service.conn.execute(
            "SELECT employee_id,offer_round FROM offers WHERE refill_id=? ORDER BY id",
            (first["id"],),
        )]
        self.assertEqual(2, len(history))

        restarted = self.service.restart_offers(first["id"])
        self.assertEqual(exhausted["offer_round"] + 1, restarted["offer_round"])
        new_offer = self.service.offer_next(first["id"])
        self.assertEqual("OFFERED", new_offer["state"])
        self.assertIn(new_offer["assignee_id"], {first["assignee_id"], second["assignee_id"]})
        preserved = [tuple(row) for row in self.service.conn.execute(
            "SELECT employee_id,offer_round FROM offers WHERE refill_id=? ORDER BY id",
            (first["id"],),
        )]
        self.assertEqual(history, preserved[:2])
        self.assertEqual((new_offer["assignee_id"], restarted["offer_round"]), preserved[2])
        self.assertEqual(120_000, self.service.budget("oslo", "2026-11")["reserved_minor"])

    def test_restart_offers_cannot_interrupt_offered_or_committed_buyer(self):
        offered = self.offer()
        for state in ["OFFERED", "COMMITTED"]:
            with self.subTest(state=state):
                if state == "COMMITTED":
                    self.service.commit(offered["id"], offered["assignee_id"])
                with self.assertRaises(RuleViolation):
                    self.service.restart_offers(offered["id"])
                current = self.service.get_refill(offered["id"])
                self.assertEqual(state, current["state"])
                self.assertEqual(offered["assignee_id"], current["assignee_id"])
                self.assertEqual(offered["offer_round"], current["offer_round"])

    def test_no_present_volunteer_leaves_refill_open(self):
        self.present("o_alex", "oslo", available=False)
        self.present("o_bea", "oslo", available=False)
        refill = self.offer()
        self.assertEqual("OPEN", refill["state"])
        self.assertIsNone(refill["assignee_id"])

    def test_holidaying_volunteer_is_not_selected(self):
        self.present("o_alex", "oslo", available=False)
        refill = self.offer()
        self.assertEqual("o_bea", refill["assignee_id"])

    def test_visitor_can_report_but_foreign_payroll_is_not_selected(self):
        self.present("o_alex", "bengaluru")
        refill = self.service.report_empty("bengaluru", "o_alex")
        offered = self.service.offer_next(refill["id"])
        self.assertIn(offered["assignee_id"], {"i_asha", "i_bala"})

    def test_foreign_payroll_visitor_does_not_hide_no_local_buyer(self):
        self.present("i_asha", "bengaluru", available=False)
        self.present("i_bala", "bengaluru", available=False)
        self.present("o_alex", "bengaluru")
        refill = self.offer("bengaluru")
        self.assertEqual("OPEN", refill["state"])
        self.assertIsNone(refill["assignee_id"])

    def test_buyer_waiting_for_reimbursement_is_skipped_for_next_refill(self):
        first = self.purchased()
        second = self.offer()
        self.assertEqual("OFFERED", second["state"])
        self.assertNotEqual(first["assignee_id"], second["assignee_id"])

    def test_fully_repaid_buyer_with_more_completed_purchases_yields_to_other_volunteer(self):
        from snack_counter.integrations import FakeChat, FakeHR, Integrations

        first = self.purchased()
        integrations = Integrations(self.service, FakeChat(), FakeHR())
        integrations.submit_claim(first["id"])
        integrations.confirm_approved(first["id"], "HR approval test reference")
        integrations.confirm_paid(first["id"], "Finance payment test reference")
        self.assertEqual("REIMBURSED", self.service.get_refill(first["id"])["state"])

        next_offer = self.offer()
        self.assertEqual("OFFERED", next_offer["state"])
        self.assertNotEqual(first["assignee_id"], next_offer["assignee_id"])

    def test_expense_remains_spent_while_reimbursement_is_pending(self):
        self.purchased(amount_minor=80_000)
        before = self.service.budget("oslo", "2026-11")
        self.now += timedelta(days=3)
        self.service.tick()
        after = self.service.budget("oslo", "2026-11")
        self.assertEqual(80_000, before["spent_minor"])
        self.assertEqual(before, after)

    def test_exact_budget_purchase_is_allowed(self):
        result = self.purchased(amount_minor=600_000, cap_minor=600_000)
        self.assertEqual("PURCHASED", result["state"])
        budget = self.service.budget("oslo", "2026-11")
        self.assertEqual(600_000, budget["spent_minor"])
        self.assertEqual(0, budget["available_minor"])

    def test_prior_spend_plus_new_reservation_cannot_exceed_budget(self):
        self.purchased(amount_minor=480_000, cap_minor=480_000)
        blocked = self.offer(cap_minor=120_001)
        self.assertEqual("OPEN", blocked["state"])
        self.assertIsNone(blocked["assignee_id"])
        budget = self.service.budget("oslo", "2026-11")
        self.assertEqual(480_000, budget["spent_minor"])
        self.assertEqual(0, budget["reserved_minor"])
        self.assertEqual(120_000, budget["available_minor"])

    def test_over_cap_purchase_fails_atomically_and_can_be_corrected(self):
        committed = self.committed(cap_minor=40_000)
        before = self.service.budget("oslo", "2026-11")
        with self.assertRaises((RuleViolation, BudgetExceeded)):
            self.service.purchase(
                committed["id"], committed["assignee_id"], 40_001,
                receipt=b"receipt", items=[{"name": "nuts", "quantity": 1}],
            )
        self.assertEqual(before, self.service.budget("oslo", "2026-11"))
        self.assertEqual("COMMITTED", self.service.get_refill(committed["id"])["state"])
        self.service.purchase(
            committed["id"], committed["assignee_id"], 40_000,
            receipt=b"receipt", items=[{"name": "nuts", "quantity": 1}],
        )

    def test_money_rejects_zero_negative_float_and_boolean(self):
        committed = self.committed()
        for invalid in [0, -1, 12.5, True]:
            with self.subTest(amount=invalid), self.assertRaises(RuleViolation):
                self.service.purchase(
                    committed["id"], committed["assignee_id"], invalid,
                    receipt=b"receipt", items=[{"name": "nuts", "quantity": 1}],
                )
        self.assertEqual(0, self.service.budget("oslo", "2026-11")["spent_minor"])

    def test_purchase_requires_committed_buyer_and_receipt(self):
        offered = self.offer()
        with self.assertRaises(RuleViolation):
            self.service.purchase(
                offered["id"], offered["assignee_id"], 30_000,
                receipt=b"receipt", items=[{"name": "nuts", "quantity": 1}],
            )
        self.service.commit(offered["id"], offered["assignee_id"])
        other = "o_bea" if offered["assignee_id"] == "o_alex" else "o_alex"
        with self.assertRaises(RuleViolation):
            self.service.purchase(offered["id"], other, 30_000, receipt=b"receipt", items=[{"name": "nuts", "quantity": 1}])
        with self.assertRaises(RuleViolation):
            self.service.purchase(
                offered["id"], offered["assignee_id"], 30_000, receipt=b"", items=[{"name": "nuts", "quantity": 1}]
            )

    def test_budgets_remain_independent_when_report_converts_currencies(self):
        self.purchased("oslo", amount_minor=30_000)
        self.purchased("bengaluru", amount_minor=50_000)
        report = self.service.report(
            "2026-11", "INR", {"NOK": Decimal("8"), "INR": Decimal("1")}
        )
        self.assertEqual({"NOK": 30_000, "INR": 50_000}, report["by_currency"])
        self.assertEqual(290_000, report["total_minor"])
        self.assertEqual("INR", report["reporting_currency"])
        self.assertEqual(570_000, self.service.budget("oslo", "2026-11")["available_minor"])
        self.assertEqual(
            1_450_000, self.service.budget("bengaluru", "2026-11")["available_minor"]
        )

    def test_consolidated_report_requires_rate_for_every_spending_currency(self):
        self.purchased("oslo")
        self.purchased("bengaluru", amount_minor=50_000)
        with self.assertRaises(RuleViolation):
            self.service.report("2026-11", "INR", {"INR": Decimal("1")})

    def test_empty_month_report_is_zero(self):
        report = self.service.report("2026-10", "INR", {})
        self.assertEqual({}, report["by_currency"])
        self.assertEqual(0, report["total_minor"])

    def test_commit_migrates_reservation_when_offer_crosses_month_boundary(self):
        self.now = datetime(2026, 11, 30, 22, 0, tzinfo=UTC)
        self.present("o_alex", "oslo")
        self.present("o_bea", "oslo")
        offered = self.offer()
        self.assertEqual(120_000, self.service.budget("oslo", "2026-11")["reserved_minor"])
        self.now = datetime(2026, 12, 1, 0, 5, tzinfo=UTC)
        self.present(offered["assignee_id"], "oslo")
        self.service.commit(offered["id"], offered["assignee_id"])
        self.assertEqual(0, self.service.budget("oslo", "2026-11")["reserved_minor"])
        self.assertEqual(120_000, self.service.budget("oslo", "2026-12")["reserved_minor"])

    def test_delayed_receipt_charges_original_local_purchase_month(self):
        self.now = datetime(2026, 11, 30, 22, 0, tzinfo=UTC)
        self.present("o_alex", "oslo")
        self.present("o_bea", "oslo")
        committed = self.committed()
        purchased_at = self.now + timedelta(minutes=15)
        self.now = datetime(2026, 12, 2, 10, 0, tzinfo=UTC)
        self.service.purchase(
            committed["id"], committed["assignee_id"], 30_000,
            receipt=b"receipt uploaded later", items=[{"name": "nuts", "quantity": 1}], purchased_at=purchased_at,
        )
        self.assertEqual(30_000, self.service.budget("oslo", "2026-11")["spent_minor"])
        self.assertEqual(0, self.service.budget("oslo", "2026-12")["spent_minor"])
        self.assertEqual(
            {"NOK": 30_000},
            self.service.report("2026-11", "NOK", {"NOK": Decimal("1")})["by_currency"],
        )

    def test_purchase_timestamp_must_be_between_commit_and_now(self):
        committed = self.committed()
        for invalid in [self.now - timedelta(seconds=1), self.now + timedelta(seconds=1)]:
            with self.subTest(purchased_at=invalid), self.assertRaises(RuleViolation):
                self.service.purchase(
                    committed["id"], committed["assignee_id"], 30_000,
                    receipt=b"receipt", items=[{"name": "nuts", "quantity": 1}], purchased_at=invalid,
                )

    def test_purchase_after_authorized_month_requires_new_authorization(self):
        self.now = datetime(2026, 11, 30, 22, 0, tzinfo=UTC)
        self.present("o_alex", "oslo")
        self.present("o_bea", "oslo")
        committed = self.committed()
        self.now = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)
        with self.assertRaises(RuleViolation):
            self.service.purchase(
                committed["id"], committed["assignee_id"], 30_000,
                receipt=b"receipt", items=[{"name": "nuts", "quantity": 1}],
            )
        self.assertEqual(120_000, self.service.budget("oslo", "2026-11")["reserved_minor"])
        self.assertEqual(0, self.service.budget("oslo", "2026-12")["spent_minor"])

    def test_reporting_month_uses_each_office_local_purchase_date(self):
        # At this instant Bengaluru is in December and Oslo is in November.
        self.now = datetime(2026, 11, 30, 20, 0, tzinfo=UTC)
        for employee_id, office in [
            ("o_alex", "oslo"), ("o_bea", "oslo"),
            ("i_asha", "bengaluru"), ("i_bala", "bengaluru"),
        ]:
            self.present(employee_id, office)
        self.purchased("oslo", amount_minor=30_000)
        self.purchased("bengaluru", amount_minor=50_000)
        rates = {"NOK": Decimal("8"), "INR": Decimal("1")}
        self.assertEqual({"NOK": 30_000}, self.service.report("2026-11", "INR", rates)["by_currency"])
        self.assertEqual({"INR": 50_000}, self.service.report("2026-12", "INR", rates)["by_currency"])

    def test_hr_claim_description_uses_office_local_purchase_date(self):
        self.now = datetime(2026, 11, 30, 20, 0, tzinfo=UTC)
        self.present("i_asha", "bengaluru")
        self.present("i_bala", "bengaluru")
        purchased = self.purchased("bengaluru", amount_minor=50_000)
        claim = self.service.conn.execute(
            "SELECT description FROM claims WHERE refill_id=?", (purchased["id"],)
        ).fetchone()
        self.assertIn("on 2026-12-01", claim["description"])
        self.assertNotIn("2026-11-30", claim["description"])


if __name__ == "__main__":
    unittest.main()
