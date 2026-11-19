import hashlib
import hmac
import json
import time
import unittest
from datetime import datetime as RealDateTime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_checkout import Charity, Entry, app, db
from raffle_multi import apply_scheduled_status_updates


class PaymentSafetyTest(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        with app.app_context():
            charity = Charity(slug="payment-safety-test", name="Safety Test",
                              campaign_status="inactive", stripe_account_id="acct_safety")
            db.session.add(charity)
            db.session.flush()
            entry = Entry(charity_id=charity.id, name="Test", email="test@example.com",
                          number=2, stripe_account_id="acct_safety",
                          payment_intent_id="pi_safety")
            db.session.add(entry)
            db.session.commit()
            self.entry_id = entry.id
        self.addCleanup(self.remove_fixture)
        self.client = app.test_client()

    def remove_fixture(self):
        with app.app_context():
            db.session.delete(db.session.get(Entry, self.entry_id))
            db.session.delete(Charity.query.filter_by(slug="payment-safety-test").one())
            db.session.commit()

    def test_other_session_cannot_capture_or_cancel(self):
        self.assertEqual(self.client.post(f"/confirm-payment/{self.entry_id}").status_code, 403)
        self.assertEqual(self.client.post(
            f"/entry/{self.entry_id}/continue-without-donating").status_code, 403)

    def test_partner_cannot_update_another_campaign(self):
        with self.client.session_transaction() as session:
            session.update(partner_ok=True, partner_slug="other", partner_charity_id=999)
        result = self.client.post(f"/admin/entry/{self.entry_id}/toggle-paid")
        self.assertEqual(result.status_code, 403)

    def test_closed_campaign_cannot_start_checkout(self):
        with self.client.session_transaction() as session:
            session["pending_entry"] = {"slug": "payment-safety-test"}
        with patch("raffle_multi.stripe.checkout.Session.create") as create:
            response = self.client.post("/payment-safety-test/start-hold")
        self.assertEqual(response.status_code, 302)
        create.assert_not_called()

    def send_signed(self, account):
        payload = json.dumps({"object": "event", "id": "evt_test",
                              "account": account, "type": "payment_intent.succeeded",
                              "data": {"object": {"object": "payment_intent",
                                                 "id": "pi_safety"}}})
        timestamp = int(time.time())
        signature = hmac.new(b"whsec_test", f"{timestamp}.{payload}".encode(),
                             hashlib.sha256).hexdigest()
        with patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": "whsec_test"}):
            return self.client.post("/stripe/webhook", data=payload,
                                    headers={"Stripe-Signature": f"t={timestamp},v1={signature}"})

    def test_webhook_requires_signature_and_matches_account(self):
        self.assertEqual(self.client.post("/stripe/webhook", data="{}").status_code, 503)
        with patch.dict("os.environ", {"STRIPE_WEBHOOK_SECRET": "whsec_test"}):
            self.assertEqual(self.client.post("/stripe/webhook", data="{}").status_code, 400)
        self.assertEqual(self.send_signed("acct_other").status_code, 200)
        with app.app_context():
            self.assertFalse(db.session.get(Entry, self.entry_id).paid)
        self.assertEqual(self.send_signed("acct_safety").status_code, 200)
        with app.app_context():
            paid_at = db.session.get(Entry, self.entry_id).paid_at
            self.assertIsNotNone(paid_at)
        self.assertEqual(self.send_signed("acct_safety").status_code, 200)
        with app.app_context():
            self.assertEqual(db.session.get(Entry, self.entry_id).paid_at, paid_at)

    def test_schedules_compare_uk_wall_time(self):
        uk_time = RealDateTime(2026, 7, 1, 19, 5, tzinfo=ZoneInfo("Europe/London"))
        with app.app_context():
            charity = Charity.query.filter_by(slug="payment-safety-test").one()
            charity.auto_live_enabled = True
            charity.auto_live_at = uk_time.replace(tzinfo=None) - timedelta(minutes=5)
            with patch("raffle_multi.datetime") as clock:
                clock.now.return_value = uk_time
                apply_scheduled_status_updates(charity)
                clock.now.assert_called_once_with(ZoneInfo("Europe/London"))
            self.assertEqual(charity.campaign_status, "live")

    def test_database_health_route(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
