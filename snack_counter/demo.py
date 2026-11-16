"""Run with python -m snack_counter.demo. Everything here is synthetic."""

import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .integrations import FakeChat, FakeHR, Integrations
from .service import SnackService


# A synthetic one-pixel PNG stands in for an uploaded receipt. No personal data.
RECEIPT = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG1sAAAAASUVORK5CYII="
)


def main():
    now = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)
    with TemporaryDirectory() as directory:
        service = SnackService(Path(directory) / "demo.sqlite3", clock=lambda: now)
        try:
            service.configure_office("oslo", "NOK", 600_000, 120_000, "Europe/Oslo")
            service.configure_office("bengaluru", "INR", 1_500_000, 300_000, "Asia/Kolkata")
            for employee, office, currency in [
                ("anders", "oslo", "NOK"), ("ingrid", "oslo", "NOK"),
                ("asha", "bengaluru", "INR"), ("bala", "bengaluru", "INR"),
            ]:
                service.add_employee(employee, office, currency)
                service.set_presence(employee, office, "2026-11-16")
            chat, hr = FakeChat(), FakeHR()
            worker = Integrations(service, chat, hr)
            print("Synthetic demo: no real chat, HR, receipts or exchange rates.\n")

            oslo = service.report_empty("oslo", "ingrid")
            duplicate = service.report_empty("oslo", "anders")
            assert oslo["id"] == duplicate["id"]
            first = service.offer_next(oslo["id"])
            now += timedelta(hours=4, seconds=1)
            service.tick()
            offered = service.get_refill(oslo["id"])
            print(f"1. Duplicate empty reports merged. Expired offer: {first['assignee_id']} -> {offered['assignee_id']}.")
            buyer = offered["assignee_id"]
            service.commit(oslo["id"], buyer)
            service.purchase(oslo["id"], buyer, 85_000, RECEIPT,
                             [{"name": "nuts", "quantity": 4}, {"name": "fruit", "quantity": 8}])
            worker.submit_claim(oslo["id"])
            worker.confirm_approved(oslo["id"], "SIMULATED HR approval export 001")
            worker.confirm_paid(oslo["id"], "SIMULATED finance payment 001")
            print("2. Oslo: bought and stocked 850.00 NOK; claim submitted, approved, then paid.")

            india = service.report_empty("bengaluru", "bala")
            buyer = service.offer_next(india["id"])["assignee_id"]
            service.commit(india["id"], buyer)
            service.purchase(india["id"], buyer, 180_000, RECEIPT,
                             [{"name": "nuts", "quantity": 3}, {"name": "biscuits", "quantity": 6}])
            hr.timeout_after_create = True
            worker.submit_claim(india["id"])
            worker.submit_claim(india["id"])  # Does not create a second claim.
            print(f"3. Bengaluru: HR accepted but timed out; status UNKNOWN, {len(hr.claims)} total claims for 2 refills.")
            # In real use a coordinator would locate and verify this external claim.
            external_id = next(key for key, payload in hr.claims.items()
                               if india["id"] in payload["description"])
            worker.reconcile_claim(india["id"], external_id)
            worker.confirm_approved(india["id"], "SIMULATED HR approval export 002")
            print("4. Bengaluru claim reconciled and approved. It remains unpaid; buyer is protected from another assignment.")

            blocked = service.report_empty("oslo", "anders", cap_minor=515_001)
            assert service.offer_next(blocked["id"])["state"] == "OPEN"
            print("5. A new Oslo request above the remaining budget stays OPEN and alerts its coordinator.")
            report = service.report("2026-11", "NOK", {"NOK": Decimal("1"), "INR": Decimal("0.125")})
            print("\nMonthly report (illustrative rate: 1 INR = 0.125 NOK; integer minor units):")
            print(json.dumps(report, indent=2))
            for office in ("oslo", "bengaluru"):
                print(f"Budget {office}: {json.dumps(service.budget(office, '2026-11'))}")
            worker.deliver_chat()
            print(f"\n{len(chat.messages)} simulated chat notifications delivered from the durable outbox.")
        finally:
            service.close()


if __name__ == "__main__":
    main()
