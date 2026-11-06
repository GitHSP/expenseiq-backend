from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from expenses.models import Account, Statement
from .models import ChecklistItem, Debt
from .reminders import build_reminders
from .services import get_or_create_month_plan


class PaidAmountTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="p@example.test", username="p", password="x-Test-12345")
        self.card = Debt.objects.create(user=self.user, name="Card", current_balance=Decimal("1000"),
                                        annual_interest_rate=Decimal("20"), minimum_payment=Decimal("40"), due_day=10)
        plan, _ = get_or_create_month_plan(self.user, 2026, 3)
        self.item = ChecklistItem.objects.get(monthly_plan=plan, linked_debt=self.card, category="debt_min")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def toggle(self, **data):
        return self.client.post(f"/api/fp/checklist/{self.item.id}/toggle/", data, format="json")

    def test_paying_more_than_the_minimum(self):
        res = self.toggle(amount="250")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["item"]["paid_amount"], "250.00")
        self.card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal("750.00"))
        self.toggle()                                          # untick: nothing paid recorded any more
        self.item.refresh_from_db()
        self.assertIsNone(self.item.paid_amount)

    def test_minimum_by_default_and_bad_amounts(self):
        self.assertEqual(self.toggle(amount="-5").status_code, 400)
        self.assertEqual(self.toggle(amount="abc").status_code, 400)
        self.toggle()
        self.card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal("960.00"))


class ClosedAccountTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="q@example.test", username="q", password="x-Test-12345")
        self.old = Account.objects.create(user=self.user, name="Old bank", kind="chequing")
        Statement.objects.create(user=self.user, account=self.old, filename="o.pdf", period_start=date(2026, 1, 1),
                                 period_end=date(2026, 1, 31), new_balance=Decimal("300"))
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_closing_stops_reminders_and_leaves_net_worth(self):
        self.assertEqual(len(build_reminders(self.user, today=date(2026, 3, 15))["uploads"]), 1)
        self.assertEqual(self.client.get("/api/fp/net-worth/").data["banks"][0]["balance"], "300.00")
        res = self.client.patch(f"/api/statements/accounts/{self.old.id}/", {"closed": True}, format="json")
        self.assertTrue(res.data["closed"])
        self.assertEqual(build_reminders(self.user, today=date(2026, 3, 15))["uploads"], [])
        self.assertEqual(self.client.get("/api/fp/net-worth/").data["banks"], [])
        self.assertEqual(self.client.patch(f"/api/statements/accounts/{self.old.id}/", {"closed": "yes"}, format="json").status_code, 400)
