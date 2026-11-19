import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

# Always isolate these tests from any developer or deployment configuration.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["FLASK_SECRET_KEY"] = "checkout-test-only"
os.environ["STRIPE_SECRET_KEY"] = ""
os.environ["STRIPE_WEBHOOK_SECRET"] = ""

from raffle_multi import Charity, Entry, app, db


class FixedPriceCheckoutTest(unittest.TestCase):
    def test_payment_confirmation_creates_paid_entry(self):
        app.config["TESTING"] = True
        with app.app_context():
            db.session.add(Charity(slug="checkout-test", name="Checkout Test",
                                   max_number=1, fixed_price_enabled=True,
                                   fixed_ticket_price_pence=550,
                                   stripe_account_id="acct_test"))
            db.session.commit()

        with app.test_client() as client:
            with client.session_transaction() as session:
                session["pending_entry"] = {
                    "slug": "checkout-test", "name": "Test Supporter",
                    "email": "test@example.com", "phone": "07000000000",
                }
            with (
                patch("raffle_multi.stripe.checkout.Session.create") as create,
                patch("raffle_multi.stripe.checkout.Session.retrieve") as retrieve,
                patch("raffle_multi.stripe.PaymentIntent.modify"),
                patch("raffle_multi.stripe.Charge.list") as charges,
            ):
                create.return_value = SimpleNamespace(url="https://example.com/checkout")
                retrieve.return_value = {"payment_intent": {
                    "id": "pi_test", "status": "succeeded",
                    "amount": 550, "amount_received": 550,
                }}
                charges.return_value = SimpleNamespace(data=[
                    SimpleNamespace(receipt_url="https://example.com/receipt")
                ])
                start = client.post("/checkout-test/start-hold")
                self.assertEqual(start.location, "https://example.com/checkout")
                self.assertEqual(create.call_args.kwargs["stripe_account"], "acct_test")
                self.assertEqual(create.call_args.kwargs["line_items"][0]["price_data"]["unit_amount"], 550)
                result = client.get("/checkout-test/success?session_id=cs_test")
                self.assertEqual(result.status_code, 200)
                self.assertIn(b"5.50", result.data)
                self.assertIn(b"https://example.com/receipt", result.data)

        with app.app_context():
            entry = Entry.query.filter_by(payment_intent_id="pi_test").one()
            self.assertEqual(entry.number, 1)
            self.assertEqual(entry.stripe_account_id, "acct_test")
            self.assertTrue(entry.paid)
