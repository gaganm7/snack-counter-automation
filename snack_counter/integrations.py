"""Simulated external systems and explicit, recoverable delivery boundaries.

The HR fake deliberately does not deduplicate create requests: the exercise only
promises a create API. A timed-out request therefore needs reconciliation before
any further submission, even if the failure happened before HR saved a claim.
"""

from copy import deepcopy
from threading import Lock

from .service import RuleViolation


class FakeChat:
    """A chat stand-in that accepts a stable message key for duplicate delivery."""

    def __init__(self):
        self.messages = []
        self.fail_next = False
        self._seen = set()
        self._lock = Lock()

    def send(self, event_id, recipient, body):
        with self._lock:
            if self.fail_next:
                self.fail_next = False
                raise ConnectionError("simulated chat outage")
            if event_id not in self._seen:
                self._seen.add(event_id)
                self.messages.append(
                    {"event_id": event_id, "recipient": recipient, "body": body}
                )


class FakeHR:
    """Non-idempotent HR create API; lookup represents a manual admin check.

    Values remain in the submitted purchase currency. Real HR owns any payroll
    conversion; the core currently selects buyers whose payroll currency matches
    the office, so that conversion is unnecessary in this MVP.
    """

    def __init__(self):
        self.claims = {}
        self.create_calls = 0
        self.fail_before_create = False
        self.timeout_after_create = False
        self._lock = Lock()

    def create_claim(self, payload):
        with self._lock:
            self.create_calls += 1
            if self.fail_before_create:
                self.fail_before_create = False
                raise ConnectionError("simulated failure before HR acceptance")
            external_id = f"hr-{len(self.claims) + 1}"
            self.claims[external_id] = deepcopy(payload)
            if self.timeout_after_create:
                self.timeout_after_create = False
                raise TimeoutError("HR accepted the claim, but its response was lost")
            return external_id

    def get_claim(self, external_id):
        """A stand-in for a human checking HR; not a promised production API."""
        with self._lock:
            return deepcopy(self.claims[external_id])


class Integrations:
    """One explicit scheduler/worker step at a time; no hidden background sends."""

    def __init__(self, service, chat, hr):
        self.service, self.chat, self.hr = service, chat, hr

    def _claim(self, refill_id):
        row = self.service.conn.execute(
            "SELECT * FROM claims WHERE refill_id=?", (refill_id,)
        ).fetchone()
        if row is None:
            raise RuleViolation("No reimbursement claim for this refill")
        return row

    def get_claim(self, refill_id):
        row = dict(self._claim(refill_id))
        row.pop("receipt_image")
        return row

    @staticmethod
    def _payload(claim):
        return {field: claim[field] for field in (
            "employee_id", "amount_minor", "currency", "description", "receipt_image"
        )}

    @staticmethod
    def _evidence(evidence):
        if not isinstance(evidence, str) or not evidence.strip():
            raise RuleViolation("A nonblank finance/HR evidence reference is required")
        return evidence.strip()

    def deliver_chat(self):
        """Retry a durable outbox. Chat's stable keys make redelivery harmless.

        Chat delivery happens outside the transaction. A real chat adapter must
        support deduplication by event key, or accept occasional duplicate messages.
        A notification never grants ownership: the current database state does.
        """
        rows = self.service.conn.execute(
            "SELECT * FROM outbox WHERE sent=0 ORDER BY id"
        ).fetchall()
        for row in rows:
            try:
                self.chat.send(row["event_key"], row["recipient"], row["body"])
            except Exception:
                # Preserve this and later messages for the next worker invocation.
                break
            with self.service.transaction():
                self.service.conn.execute("UPDATE outbox SET sent=1 WHERE id=?", (row["id"],))

    def submit_claim(self, refill_id):
        """Create once; an ambiguous outcome always waits for human reconciliation."""
        with self.service.transaction():
            claim = self._claim(refill_id)
            if claim["status"] != "READY":
                return self.get_claim(refill_id)
            payload = self._payload(claim)
            self.service.conn.execute(
                "UPDATE claims SET status='SENDING',detail=NULL,updated_at=? WHERE refill_id=?",
                (self.service.now().isoformat(), refill_id),
            )
            self.service._event(refill_id, "CLAIM_SUBMISSION_STARTED")

        # HR has no promised idempotency support. Persist the attempt before I/O.
        # A process crash here leaves SENDING, which also cannot be auto-retried.
        try:
            external_id = self.hr.create_claim(payload)
            if not isinstance(external_id, str) or not external_id.strip():
                raise ValueError("HR response lacks a claim ID")
        except Exception as error:
            with self.service.transaction():
                changed = self.service.conn.execute(
                    "UPDATE claims SET status='UNKNOWN',detail=?,updated_at=? "
                    "WHERE refill_id=? AND status='SENDING'",
                    (type(error).__name__, self.service.now().isoformat(), refill_id),
                ).rowcount
                if changed:
                    self.service._event(refill_id, "CLAIM_OUTCOME_UNKNOWN", error_type=type(error).__name__)
                    office = self.service._refill(refill_id)["office_id"]
                    self.service._notify(
                        f"{refill_id}:claim-unknown", f"coordinator:{office}",
                        f"Claim {refill_id} has an unknown HR outcome. Check HR before retrying.",
                    )
            return self.get_claim(refill_id)

        with self.service.transaction():
            if self._claim(refill_id)["status"] != "SENDING":
                raise RuleViolation("Claim changed during submission; reconcile the external claim")
            self.service.conn.execute(
                "UPDATE claims SET status='SUBMITTED',external_id=?,updated_at=? WHERE refill_id=?",
                (external_id, self.service.now().isoformat(), refill_id),
            )
            self.service._event(refill_id, "CLAIM_SUBMITTED", external_id=external_id)
            self.service._notify(
                f"{refill_id}:claim-submitted", payload["employee_id"],
                f"Reimbursement for refill {refill_id} submitted to HR as {external_id}.",
            )
            return self.get_claim(refill_id)

    def reconcile_claim(self, refill_id, external_id):
        """A coordinator links a verified HR claim after an interrupted submission.

        The lookup is a simulation of an admin checking HR, not an assumption
        about an available HR status API. Every frozen payload field must match.
        """
        external_payload = self.hr.get_claim(external_id)
        with self.service.transaction():
            claim = self._claim(refill_id)
            if claim["status"] not in ("UNKNOWN", "SENDING"):
                raise RuleViolation("Only an ambiguous submission can be reconciled")
            if external_payload != self._payload(claim):
                raise RuleViolation("External claim does not match the frozen reimbursement request")
            self.service.conn.execute(
                "UPDATE claims SET status='SUBMITTED',external_id=?,detail=?,updated_at=? "
                "WHERE refill_id=?",
                (external_id, "Manually matched exact HR payload", self.service.now().isoformat(), refill_id),
            )
            self.service._event(refill_id, "CLAIM_RECONCILED", external_id=external_id)
            return self.get_claim(refill_id)

    def confirm_not_submitted(self, refill_id, evidence):
        """Explicit recovery only after HR has confirmed that no claim exists.

        A SENDING claim can belong to a live worker. Stop/reconcile that worker
        before using this administrative operation on a crashed submission.
        """
        evidence = self._evidence(evidence)
        with self.service.transaction():
            if self._claim(refill_id)["status"] not in ("UNKNOWN", "SENDING"):
                raise RuleViolation("Only an ambiguous submission needs absence confirmation")
            self.service.conn.execute(
                "UPDATE claims SET status='READY',detail=?,updated_at=? WHERE refill_id=?",
                (evidence, self.service.now().isoformat(), refill_id),
            )
            self.service._event(refill_id, "CLAIM_ABSENCE_CONFIRMED", evidence=evidence)
            return self.get_claim(refill_id)

    def confirm_approved(self, refill_id, evidence):
        evidence = self._evidence(evidence)
        with self.service.transaction():
            if self._claim(refill_id)["status"] != "SUBMITTED":
                raise RuleViolation("Only a submitted claim can be approved")
            self.service.conn.execute(
                "UPDATE claims SET status='APPROVED',detail=?,updated_at=? WHERE refill_id=?",
                (evidence, self.service.now().isoformat(), refill_id),
            )
            self.service._event(refill_id, "CLAIM_APPROVED", evidence=evidence)
            return self.get_claim(refill_id)

    def confirm_paid(self, refill_id, evidence):
        evidence = self._evidence(evidence)
        with self.service.transaction():
            claim = self._claim(refill_id)
            if claim["status"] != "APPROVED":
                raise RuleViolation("Payment confirmation requires an approved claim")
            self.service.conn.execute(
                "UPDATE claims SET status='PAID',detail=?,updated_at=? WHERE refill_id=?",
                (evidence, self.service.now().isoformat(), refill_id),
            )
            self.service.conn.execute(
                "UPDATE refills SET state='REIMBURSED' WHERE id=?", (refill_id,)
            )
            self.service._event(refill_id, "REIMBURSED", evidence=evidence)
            self.service._notify(
                f"{refill_id}:paid", claim["employee_id"],
                f"Finance confirmed payment for refill {refill_id} (HR {claim['external_id']}).",
            )
            return self.get_claim(refill_id)
