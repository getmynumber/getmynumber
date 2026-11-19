import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

# Always isolate these tests from any developer or deployment configuration.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["FLASK_SECRET_KEY"] = "checkout-test-only"
os.environ["STRIPE_SECRET_KEY"] = ""
os.environ["STRIPE_WEBHOOK_SECRET"] = ""

from raffle_multi import Charity, Entry, FixedCheckout, app, db


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
                    "checkout_token": "a" * 32,
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
                    "id": "pi_test", "status": "succeeded", "currency": "gbp",
                    "amount": 550, "amount_received": 550,
                }, "metadata": {"reservation_token": "a" * 32}}
                charges.return_value = SimpleNamespace(data=[
                    SimpleNamespace(receipt_url="https://example.com/receipt")
                ])
                start = client.post("/checkout-test/start-hold")
                self.assertEqual(start.location, "https://example.com/checkout")
                self.assertNotIn("payment_method_types", create.call_args.kwargs)
                self.assertEqual(create.call_args.kwargs["stripe_account"], "acct_test")
                self.assertEqual(create.call_args.kwargs["line_items"][0]["price_data"]["unit_amount"], 550)
                with app.app_context():
                    reserved_entry = db.session.get(FixedCheckout, "a" * 32).entry
                    self.assertFalse(reserved_entry.paid)
                    reserved_id = reserved_entry.id
                with client.session_transaction() as session:
                    session["admin_ok"] = True
                self.assertEqual(client.post(f"/admin/entry/{reserved_id}/toggle-paid").status_code, 409)
                edit = client.get("/admin/charity/checkout-test")
                self.assertIn(b\'value="5.50"\', edit.data)
                result = client.get("/checkout-test/success?session_id=cs_test")
                self.assertEqual(result.status_code, 200)
                self.assertIn(b"5.50", result.data)
                self.assertIn(b"https://example.com/receipt", result.data)
                self.assertEqual(client.get("/checkout-test/success?session_id=cs_test").status_code, 200)

        with app.app_context():
            entry = Entry.query.filter_by(payment_intent_id="pi_test").one()
            self.assertEqual(entry.number, 1)
            self.assertEqual(entry.stripe_account_id, "acct_test")
            self.assertTrue(entry.paid)

    def test_webhook_recovers_paid_entry_without_browser_return(self):
        app.config["TESTING"] = True
        with app.app_context():
            db.session.add(Charity(slug="recover-test", name="Recovery Test",
                                   max_number=2, fixed_price_enabled=True,
                                   fixed_ticket_price_pence=550,
                                   stripe_account_id="acct_test"))
            db.session.commit()
        for token in ("b" * 32, "c" * 32):
            with app.test_client() as client:
                with client.session_transaction() as session:
                    session["pending_entry"] = {
                        "checkout_token": token, "slug": "recover-test",
                        "name": "Test Supporter", "email": "test@example.com",
                    }
                with patch("raffle_multi.stripe.checkout.Session.create",
                           return_value=SimpleNamespace(url="https://example.com/checkout")):
                    self.assertEqual(client.post("/recover-test/start-hold").location,
                                     "https://example.com/checkout")

        token = "b" * 32
        intent = {"id": "pi_recovered", "status": "succeeded", "currency": "gbp",
                  "amount_received": 550, "metadata": {"reservation_token": token}}
        event = {"type": "payment_intent.succeeded", "account": "acct_test",
                 "data": {"object": intent}}
        with app.test_client() as webhook_client:
            with (
                patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": "whsec_fixture"}),
                patch("raffle_multi.stripe.Webhook.construct_event", return_value=event),
            ):
                event["account"] = "acct_wrong"
                self.assertEqual(webhook_client.post("/stripe/webhook").status_code, 200)
                with app.app_context():
                    self.assertFalse(db.session.get(FixedCheckout, token).entry.paid)
                event["account"] = "acct_test"
                intent["amount_received"] = 549
                self.assertEqual(webhook_client.post("/stripe/webhook").status_code, 500)
                intent["amount_received"] = 550
                for _ in range(2):
                    self.assertEqual(webhook_client.post("/stripe/webhook").status_code, 200)
                with app.app_context():
                    self.assertEqual(Entry.query.filter_by(payment_intent_id="pi_recovered").count(), 1)
                    self.assertTrue(db.session.get(FixedCheckout, token).entry.paid)

                event["type"] = "checkout.session.expired"
                event["data"]["object"] = {"id": "cs_expired", "status": "expired",
                                           "payment_status": "unpaid",
                                           "metadata": {"reservation_token": "c" * 32}}
                self.assertEqual(webhook_client.post("/stripe/webhook").status_code, 200)
                with app.app_context():
                    self.assertIsNone(db.session.get(FixedCheckout, "c" * 32).entry_id)
                    charity = Charity.query.filter_by(slug="recover-test").one()
                    self.assertEqual(Entry.query.filter_by(charity_id=charity.id).count(), 1)
