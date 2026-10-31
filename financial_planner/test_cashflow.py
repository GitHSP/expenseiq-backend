from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from expenses.models import Account, MerchantRule, Statement, StatementTransaction
from .cashflow import build_cashflow
from .models import Debt, RecurringItem
from .services import refresh_average_pay


class CashFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="c@example.test", username="c", password="x-Test-12345")
        self.today = date(2026, 3, 10)
        RecurringItem.objects.create(user=self.user, label="Pay", amount=Decimal("1000"), category="income",
                                     frequency="biweekly", anchor_date=date(2026, 3, 13))
        RecurringItem.objects.create(user=self.user, label="Rent", amount=Decimal("625"), due_day=15,
                                     category="fixed_expense")

    def run_flow(self, **kw):
        return build_cashflow(self.user, today=self.today, **kw)

    def test_running_balance_and_order(self):
        out = self.run_flow(days=7, balance=Decimal("100"))
        self.assertEqual([e["label"] for e in out["events"]], ["Pay (Mar 13)", "Rent"])
        self.assertEqual(out["events"][1]["balance"], Decimal("475"))
        self.assertEqual(out["end_balance"], Decimal("475"))
        self.assertEqual(out["money_in"], Decimal("1000"))
        self.assertEqual(out["money_out"], Decimal("625"))
        self.assertIsNone(out["first_short"])

    def test_warns_at_the_first_shortfall(self):
        RecurringItem.objects.create(user=self.user, label="Phone", amount=Decimal("80"), due_day=11,
                                     category="fixed_expense")
        out = self.run_flow(days=7, balance=Decimal("50"))
        self.assertEqual(out["first_short"]["label"], "Phone")
        self.assertEqual(out["first_short"]["date"], "2026-03-11")
        self.assertEqual(out["lowest"], Decimal("-30"))
        self.assertEqual(out["lowest_date"], "2026-03-11")

    def test_paid_by_card_and_past_dates_are_left_out(self):
        RecurringItem.objects.create(user=self.user, label="Netflix", amount=Decimal("20"), due_day=12,
                                     category="fixed_expense", paid_by_card=True)
        RecurringItem.objects.create(user=self.user, label="Gym", amount=Decimal("30"), due_day=2,
                                     category="fixed_expense")
        labels = [e["label"] for e in self.run_flow(days=7, balance=Decimal("0"))["events"]]
        self.assertNotIn("Netflix", labels)
        self.assertNotIn("Gym", labels)

    def test_debt_minimum_included_and_extra_payment_is_not(self):
        Debt.objects.create(user=self.user, name="Card", current_balance=Decimal("500"), annual_interest_rate=Decimal("20"),
                            minimum_payment=Decimal("40"), due_day=12)
        labels = [e["label"] for e in self.run_flow(days=7, balance=Decimal("0"))["events"]]
        self.assertIn("Card minimum payment", labels)
        self.assertFalse(any("EXTRA" in x for x in labels))

    def test_starts_from_the_latest_bank_statement(self):
        acc = Account.objects.create(user=self.user, name="Chequing", kind="chequing")
        Statement.objects.create(user=self.user, account=acc, filename="a.pdf", period_start=date(2026, 2, 1),
                                 period_end=date(2026, 2, 28), new_balance=Decimal("750"))
        out = self.run_flow(days=7)
        self.assertEqual(out["start_balance"], Decimal("750"))
        self.assertFalse(out["stale"])
        self.assertEqual(self.run_flow(days=7, balance=Decimal("10"))["start_balance"], Decimal("10"))

    def test_api_private_and_tolerates_bad_input(self):
        client = APIClient()
        self.assertEqual(client.get("/api/fp/cash-flow/").status_code, 401)
        client.force_authenticate(self.user)
        res = client.get("/api/fp/cash-flow/?days=abc&balance=nan")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["days"], 60)
        self.assertFalse(res.data["entered"])


class VaryingBillTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="v@example.test", username="v", password="x-Test-12345")
        acc = Account.objects.create(user=self.user, name="Chequing", kind="chequing")
        st = Statement.objects.create(user=self.user, account=acc, filename="a.pdf", period_start=date(2026, 1, 1),
                                      period_end=date(2026, 4, 30))
        for month, paid in ((1, "100"), (2, "120"), (3, "140"), (4, "160")):
            StatementTransaction.objects.create(user=self.user, statement=st, account=acc, date=date(2026, month, 5),
                                                description="POWERCO BILL", title="PowerCo", amount=-Decimal(paid))

    def test_bill_amount_is_the_average_of_recent_payments(self):
        bill = RecurringItem.objects.create(user=self.user, label="Power", amount=Decimal("1"), due_day=5,
                                            category="fixed_expense", average_from="POWERCO")
        refresh_average_pay(self.user)
        bill.refresh_from_db()
        self.assertEqual(bill.amount, Decimal("130.00"))
        self.assertEqual(bill.average_of, 4)

    def test_shared_bill_averages_the_users_share(self):
        MerchantRule.objects.create(user=self.user, match="POWERCO", treat_as="bill", share_percent=Decimal("25"))
        bill = RecurringItem.objects.create(user=self.user, label="Power", amount=Decimal("1"), due_day=5,
                                            category="fixed_expense", average_from="POWERCO")
        refresh_average_pay(self.user)
        bill.refresh_from_db()
        self.assertEqual(bill.amount, Decimal("32.50"))
