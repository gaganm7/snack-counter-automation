"""Failure-boundary tests: an external timeout must never pay someone twice."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest

from snack_counter.integrations import FakeChat, FakeHR, Integrations
from snack_counter.service import RuleViolation, SnackService


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "snacks.sqlite3")
        self.now = datetime(2026, 11, 16, 10, tzinfo=timezone.utc)
        self.service = SnackService(self.path, clock=lambda: self.now)
        self.addCleanup(self.service.close)
        self.service.configure_office("oslo", "NOK", 600_000, 120_000, "Europe/Oslo")
        self.service.add_employee("buyer", "oslo", "NOK")
        self.service.set_presence("buyer", "oslo", "2026-11-16")
        self.chat = FakeChat()
        self.hr = FakeHR()
        self.integrations = Integrations(self.service, self.chat, self.hr)

    def purchased_refill(self):
        refill = self.service.report_empty("oslo", "buyer")
        self.service.offer_next(refill["id"])
        self.service.commit(refill["id"], "buyer")
        return self.service.purchase(
            refill["id"], "buyer", 30_000, b"receipt-image-bytes",
            [{"name": "nuts", "quantity": 2}],
        )

    def test_submission_sends_frozen_exact_payload_once(self):
        refill = self.purchased_refill()
        self.integrations.submit_claim(refill["id"])
        claim = self.integrations.get_claim(refill["id"])
        self.assertEqual(claim["status"], "SUBMITTED")
        payload = self.hr.get_claim(claim["external_id"])
        self.assertEqual(payload["amount_minor"], 30_000)
        self.assertEqual(payload["currency"], "NOK")
        self.assertEqual(payload["employee_id"], "buyer")
        self.assertEqual(payload["receipt_image"], b"receipt-image-bytes")
        self.assertIn(refill["id"], payload["description"])
        self.integrations.submit_claim(refill["id"])
        self.assertEqual(self.hr.create_calls, 1)

    def test_hr_accepts_then_timeout_stays_unknown_until_reconciled(self):
        refill = self.purchased_refill()
        self.hr.timeout_after_create = True
        self.integrations.submit_claim(refill["id"])
        self.assertEqual(self.integrations.get_claim(refill["id"])["status"], "UNKNOWN")
        self.integrations.submit_claim(refill["id"])
        self.assertEqual(self.hr.create_calls, 1)
        self.assertEqual(len(self.hr.claims), 1)
        external_id = next(iter(self.hr.claims))
        self.integrations.reconcile_claim(refill["id"], external_id)
        self.assertEqual(self.integrations.get_claim(refill["id"])["status"], "SUBMITTED")
        self.assertEqual(self.hr.create_calls, 1)

    def test_restarted_worker_does_not_retry_a_persisted_sending_attempt(self):
        refill = self.purchased_refill()
        description = self.integrations.get_claim(refill["id"])["description"]
        # Simulate the persisted intent followed by HR acceptance and a process
        # crash before either its successful response or UNKNOWN could be saved.
        with self.service.transaction():
            self.service.conn.execute(
                "UPDATE claims SET status='SENDING' WHERE refill_id=?", (refill["id"],)
            )
        external_id = self.hr.create_claim({
            "employee_id": "buyer", "amount_minor": 30_000, "currency": "NOK",
            "description": description, "receipt_image": b"receipt-image-bytes",
        })
        self.service.close()
        self.service = SnackService(self.path, clock=lambda: self.now)
        self.addCleanup(self.service.close)
        self.integrations = Integrations(self.service, self.chat, self.hr)
        self.integrations.submit_claim(refill["id"])
        self.assertEqual(self.integrations.get_claim(refill["id"])["status"], "SENDING")
        self.assertEqual(self.hr.create_calls, 1)
        self.integrations.reconcile_claim(refill["id"], external_id)
        self.assertEqual(self.integrations.get_claim(refill["id"])["status"], "SUBMITTED")
        self.integrations.confirm_approved(refill["id"], "approval after crash reconciliation")
        self.integrations.confirm_paid(refill["id"], "finance payment after reconciliation")
        self.assertEqual(self.service.get_refill(refill["id"])["state"], "REIMBURSED")

    def test_failure_before_acceptance_requires_explicit_absence_evidence(self):
        refill = self.purchased_refill()
        self.hr.fail_before_create = True
        self.integrations.submit_claim(refill["id"])
        self.assertEqual(self.integrations.get_claim(refill["id"])["status"], "UNKNOWN")
        self.integrations.submit_claim(refill["id"])
        self.assertEqual(self.hr.create_calls, 1)
        with self.assertRaises(RuleViolation):
            self.integrations.confirm_not_submitted(refill["id"], "")
        self.integrations.confirm_not_submitted(
            refill["id"], "Finance checked HR's employee claims and found no matching claim."
        )
        self.integrations.submit_claim(refill["id"])
        self.assertEqual(self.hr.create_calls, 2)
        self.assertEqual(len(self.hr.claims), 1)

    def test_reconcile_rejects_claim_for_different_receipt_or_employee(self):
        refill = self.purchased_refill()
        self.hr.timeout_after_create = True
        self.integrations.submit_claim(refill["id"])
        wrong = self.hr.create_claim({
            "amount_minor": 30_000, "currency": "NOK", "employee_id": "someone-else",
            "description": f"Snack refill {refill['id']}", "receipt_image": b"different",
        })
        with self.assertRaises(RuleViolation):
            self.integrations.reconcile_claim(refill["id"], wrong)
        self.assertEqual(self.integrations.get_claim(refill["id"])["status"], "UNKNOWN")

    def test_approval_is_not_reimbursement_and_payment_needs_evidence(self):
        refill = self.purchased_refill()
        self.integrations.submit_claim(refill["id"])
        with self.assertRaises(RuleViolation):
            self.integrations.confirm_paid(refill["id"], "bank transfer")
        self.integrations.confirm_approved(refill["id"], "HR approval reference 42")
        self.assertEqual(self.service.get_refill(refill["id"])["state"], "PURCHASED")
        self.assertEqual(self.integrations.get_claim(refill["id"])["status"], "APPROVED")
        with self.assertRaises(RuleViolation):
            self.integrations.confirm_paid(refill["id"], " ")
        self.integrations.confirm_paid(refill["id"], "Finance payment reference 99")
        self.assertEqual(self.service.get_refill(refill["id"])["state"], "REIMBURSED")
        self.assertEqual(self.integrations.get_claim(refill["id"])["status"], "PAID")
        self.assertEqual(self.service.budget("oslo", "2026-11")["spent_minor"], 30_000)

    def test_unpaid_buyer_not_selected_again_until_payment_confirmed(self):
        refill = self.purchased_refill()
        next_refill = self.service.report_empty("oslo", "buyer")
        self.assertEqual(self.service.offer_next(next_refill["id"])["state"], "OPEN")
        self.integrations.submit_claim(refill["id"])
        self.integrations.confirm_approved(refill["id"], "approved")
        self.assertEqual(self.service.offer_next(next_refill["id"])["state"], "OPEN")
        self.integrations.confirm_paid(refill["id"], "paid")
        self.assertEqual(self.service.offer_next(next_refill["id"])["assignee_id"], "buyer")

    def test_concurrent_workers_make_only_one_nonidempotent_hr_call(self):
        refill = self.purchased_refill()
        barrier = Barrier(2)

        def submit():
            service = SnackService(self.path, clock=lambda: self.now)
            try:
                integrations = Integrations(service, self.chat, self.hr)
                barrier.wait(timeout=10)
                integrations.submit_claim(refill["id"])
            finally:
                service.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            for future in futures:
                future.result(timeout=20)
        self.assertEqual(self.hr.create_calls, 1)
        self.assertEqual(len(self.hr.claims), 1)
        self.assertEqual(self.integrations.get_claim(refill["id"])["status"], "SUBMITTED")

    def test_chat_delivery_failure_preserves_business_state_and_can_retry(self):
        refill = self.purchased_refill()
        pending_count = self.service.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE sent=0"
        ).fetchone()[0]
        self.assertGreater(pending_count, 0)
        self.chat.fail_next = True
        self.integrations.deliver_chat()
        self.assertEqual(self.service.get_refill(refill["id"])["state"], "PURCHASED")
        self.assertEqual(self.chat.messages, [])
        self.assertEqual(self.service.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE sent=0"
        ).fetchone()[0], pending_count)
        self.integrations.deliver_chat()
        self.assertEqual(len(self.chat.messages), pending_count)
        self.assertEqual(self.service.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE sent=0"
        ).fetchone()[0], 0)
        message_count = len(self.chat.messages)
        self.integrations.deliver_chat()
        self.assertEqual(len(self.chat.messages), message_count)

    def test_chat_redelivery_after_lost_local_ack_uses_same_stable_key(self):
        self.purchased_refill()
        row = self.service.conn.execute(
            "SELECT * FROM outbox WHERE sent=0 ORDER BY id LIMIT 1"
        ).fetchone()
        self.chat.send(row["event_key"], row["recipient"], row["body"])
        # The process died before setting sent=1, so the next worker redelivers.
        self.integrations.deliver_chat()
        keys = [message["event_id"] for message in self.chat.messages]
        self.assertEqual(keys.count(row["event_key"]), 1)
        self.assertEqual(self.service.conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE sent=0"
        ).fetchone()[0], 0)

    def test_restarted_offer_round_delivers_new_offer_and_expiry_without_duplicates(self):
        refill = self.service.report_empty("oslo", "buyer")
        self.service.offer_next(refill["id"])
        self.integrations.deliver_chat()

        def messages_starting(prefix):
            return [message for message in self.chat.messages if message["body"].startswith(prefix)]

        offer_prefix = f"Offer {refill['id']}: commit before"
        expired_prefix = f"Offer {refill['id']} expired."
        unavailable_prefix = f"No eligible buyer for {refill['id']}."
        self.assertEqual(len(messages_starting(offer_prefix)), 1)
        self.now += timedelta(hours=5)
        self.service.tick()
        self.integrations.deliver_chat()
        self.assertEqual(self.service.get_refill(refill["id"])["state"], "OPEN")
        self.assertEqual(len(messages_starting(expired_prefix)), 1)
        self.assertEqual(len(messages_starting(unavailable_prefix)), 1)
        round_zero_count = len(self.chat.messages)
        self.service.tick()
        self.integrations.deliver_chat()
        self.assertEqual(len(self.chat.messages), round_zero_count)

        self.service.restart_offers(refill["id"])
        new_offer = self.service.offer_next(refill["id"])
        self.assertEqual(new_offer["assignee_id"], "buyer")
        self.integrations.deliver_chat()
        offers = messages_starting(offer_prefix)
        self.assertEqual(len(offers), 2)
        self.assertNotEqual(offers[0]["event_id"], offers[1]["event_id"])
        round_one_offer_count = len(self.chat.messages)
        self.service.tick()
        self.integrations.deliver_chat()
        self.assertEqual(len(self.chat.messages), round_one_offer_count)

        self.now += timedelta(hours=5)
        self.service.tick()
        self.integrations.deliver_chat()
        self.assertEqual(len(messages_starting(expired_prefix)), 2)
        self.assertEqual(len(messages_starting(unavailable_prefix)), 2)
        exhausted_count = len(self.chat.messages)
        self.service.tick()
        self.integrations.deliver_chat()
        self.assertEqual(len(self.chat.messages), exhausted_count)

    def test_restarted_committed_round_delivers_new_commit_and_overdue_escalation(self):
        refill = self.service.report_empty("oslo", "buyer")
        self.service.offer_next(refill["id"])
        self.service.commit(refill["id"], "buyer")
        self.integrations.deliver_chat()
        self.now += timedelta(hours=25)
        self.service.tick()
        self.integrations.deliver_chat()
        self.service.release(refill["id"], "buyer", no_purchase_confirmed=True)
        self.service.restart_offers(refill["id"])
        self.service.set_presence("buyer", "oslo", self.now.date().isoformat())
        self.service.offer_next(refill["id"])
        self.service.commit(refill["id"], "buyer")
        self.integrations.deliver_chat()
        commits = [message for message in self.chat.messages
                   if message["body"].startswith(f"buyer committed to refill {refill['id']}.")]
        self.assertEqual(len(commits), 2)
        self.assertNotEqual(commits[0]["event_id"], commits[1]["event_id"])

        self.now += timedelta(hours=25)
        self.service.tick()
        self.integrations.deliver_chat()
        overdue_prefix = f"Refill {refill['id']} is overdue;"
        for recipient in ("buyer", "coordinator:oslo"):
            alerts = [message for message in self.chat.messages
                      if message["recipient"] == recipient and message["body"].startswith(overdue_prefix)]
            self.assertEqual(len(alerts), 2)
            self.assertNotEqual(alerts[0]["event_id"], alerts[1]["event_id"])
        message_count = len(self.chat.messages)
        self.service.tick()
        self.integrations.deliver_chat()
        self.assertEqual(len(self.chat.messages), message_count)

    def test_hr_fake_lookup_returns_snapshot_not_mutable_storage(self):
        payload = {"amount_minor": 1, "receipt_image": b"one"}
        external_id = self.hr.create_claim(payload)
        payload["amount_minor"] = 2
        fetched = self.hr.get_claim(external_id)
        fetched["amount_minor"] = 3
        self.assertEqual(self.hr.get_claim(external_id)["amount_minor"], 1)


if __name__ == "__main__":
    unittest.main()
