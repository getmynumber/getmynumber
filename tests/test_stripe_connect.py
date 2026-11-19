import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import stripe
from test_checkout import Charity, app, db


class StripeConnectTest(unittest.TestCase):
    def setUp(self):
        with app.app_context():
            db.session.add(Charity(slug="connect-test", name="Connect Test"))
            db.session.commit()
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["admin_ok"] = True
        self.route = "/admin/charity/connect-test/connect-stripe"

    def tearDown(self):
        with app.app_context():
            Charity.query.filter_by(slug="connect-test").delete()
            db.session.commit()

    def test_new_account_and_retry_after_link_failure(self):
        with (
            patch("raffle_multi.stripe.StripeClient") as factory,
            patch("raffle_multi.stripe.Account.retrieve") as retrieve,
        ):
            with patch("raffle_multi.STRIPE_SECRET_KEY", "sk_test_fixture"):
                core = factory.return_value.v2.core
                core.accounts.create.return_value = NS(id="acct_test")
                core.account_links.create.side_effect = [
                    stripe.InvalidRequestError("fixture", None),
                    {"url": "https://accounts.stripe.com/test"},
                ]
                first = self.client.post(self.route)
                self.assertIn("/admin/charity/connect-test", first.location)
                with app.app_context():
                    self.assertEqual(Charity.query.filter_by(slug="connect-test").one().stripe_account_id,
                                     "acct_test")
                retrieve.return_value = NS(metadata=NS(getmynumber_onboarding="v2"))
                second = self.client.post(self.route)
                self.assertEqual(second.location, "https://accounts.stripe.com/test")
                core.accounts.create.assert_called_once()
                params = core.accounts.create.call_args.args[0]
                self.assertEqual(params["defaults"]["responsibilities"]["losses_collector"], "stripe")
                link = core.account_links.create.call_args.args[0]
                self.assertEqual(link["account"], "acct_test")
                self.assertEqual(link["use_case"]["account_onboarding"]["configurations"], ["merchant"])

    def test_installed_sdk_exposes_v2_onboarding(self):
        core = stripe.StripeClient("sk_test_fixture").v2.core
        self.assertTrue(callable(core.accounts.create))
        self.assertTrue(callable(core.account_links.create))

    def test_existing_legacy_account_keeps_v1_link(self):
        with app.app_context():
            Charity.query.filter_by(slug="connect-test").one().stripe_account_id = "acct_legacy"
            db.session.commit()
        with (
            patch("raffle_multi.stripe.StripeClient"),
            patch("raffle_multi.stripe.Account.retrieve", return_value=NS(metadata=None)),
            patch("raffle_multi.stripe.AccountLink.create", return_value={"url": "https://accounts.stripe.com/legacy"}) as link,
        ):
            response = self.client.post(self.route)
            self.assertEqual(response.location, "https://accounts.stripe.com/legacy")
            self.assertEqual(link.call_args.kwargs["account"], "acct_legacy")
