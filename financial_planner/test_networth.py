from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from expenses.models import Account, Statement
from .models import Debt, Investment, NetWorthSnapshot
from .networth import history


class NetWorthHistoryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="n@example.test", username="n", password="x-Test-12345")
        bank = Account.objects.create(user=self.user, name="Bank", kind="chequing")
        card = Account.objects.create(user=self.user, name="Card", kind="credit_card")
        for end, bal in ((date(2026, 8, 31), "1000"), (date(2026, 9, 30), "1500")):
            Statement.objects.create(user=self.user, account=bank, filename=f"b{end}.pdf", period_start=end.replace(day=1),
                                     period_end=end, new_balance=Decimal(bal))
        Statement.objects.create(user=self.user, account=card, filename="c.pdf", period_start=date(2026, 8, 20),
                                 period_end=date(2026, 9, 19), new_balance=Decimal("400"))
        Debt.objects.create(user=self.user, name="Card", current_balance=Decimal("380"), annual_interest_rate=Decimal("20"),
                            minimum_payment=Decimal("20"))
        Debt.objects.create(user=self.user, name="Loan", debt_type="loan", current_balance=Decimal("5000"),
                            annual_interest_rate=Decimal("8"), minimum_payment=Decimal("200"))
        Investment.objects.create(user=self.user, kind="fd", name="FD", currency="INR", start_date=date(2026, 1, 1),
                                  payment=Decimal("10000"), rate=Decimal("0"), months=12)

    def test_this_month_saved_and_earlier_months_estimated(self):
        rows = history(self.user, today=date(2026, 10, 15))
        self.assertEqual([r["month"] for r in rows], ["2026-08", "2026-09", "2026-10"])
        aug, sep, octo = rows
        self.assertTrue(aug["estimated"])
        self.assertEqual(aug["banks"], Decimal("1000"))
        self.assertEqual(aug["debts"], Decimal("5000"))                # no card statement by Aug 31 yet; loan at today's
        self.assertEqual(sep["debts"], Decimal("5400"))                # card 400 as printed + loan
        self.assertFalse(octo["estimated"])
        self.assertEqual(octo["banks"], Decimal("1500"))
        self.assertEqual(octo["debts"], Decimal("5380"))
        self.assertEqual(octo["investments"], {"INR": "10000.00"})
        self.assertEqual(NetWorthSnapshot.objects.filter(user=self.user).count(), 1)

    def test_a_saved_month_is_kept_as_it_was(self):
        history(self.user, today=date(2026, 10, 15))
        Debt.objects.filter(name="Loan").update(current_balance=Decimal("4000"))
        rows = history(self.user, today=date(2026, 11, 2))
        october = next(r for r in rows if r["month"] == "2026-10")
        self.assertEqual(october["debts"], Decimal("5380"))           # October's snapshot doesn't change in November
        self.assertEqual(rows[-1]["debts"], Decimal("4380"))

    def test_api_returns_history_and_is_private(self):
        client = APIClient()
        client.force_authenticate(self.user)
        data = client.get("/api/fp/net-worth/").data
        self.assertTrue(len(data["history"]) >= 1)
        other = get_user_model().objects.create_user(email="o2@example.test", username="o2", password="x-Test-12345")
        client.force_authenticate(other)
        self.assertEqual(client.get("/api/fp/net-worth/").data["history"][-1]["debts"], "0.00")
