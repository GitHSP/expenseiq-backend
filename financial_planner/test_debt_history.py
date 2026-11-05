from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from expenses.models import Account, Statement
from .debt_history import card_history, month_change
from .models import Debt


class CardHistoryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="d@example.test", username="d", password="x-Test-12345")
        self.a = Debt.objects.create(user=self.user, name="A", current_balance=Decimal("700"), annual_interest_rate=Decimal("20"),
                                     minimum_payment=Decimal("25"))
        self.b = Debt.objects.create(user=self.user, name="B", current_balance=Decimal("300"), annual_interest_rate=Decimal("20"),
                                     minimum_payment=Decimal("25"))
        for debt, rows in ((self.a, (("2026-08-15", "900"), ("2026-09-15", "800"), ("2026-10-15", "700"))),
                           (self.b, (("2026-09-20", "-5"), ("2026-10-20", "300")))):
            acc = Account.objects.create(user=self.user, name=f"Card {debt.name}", kind="credit_card", debt=debt)
            for end, bal in rows:
                d = date.fromisoformat(end)
                Statement.objects.create(user=self.user, account=acc, filename=f"{debt.name}{end}.pdf",
                                         period_start=d.replace(day=1), period_end=d, new_balance=Decimal(bal))

    def test_per_card_and_month_end_totals(self):
        h = card_history(self.user, date(2026, 10, 31))
        amounts = lambda rows: [Decimal(p["balance"]) for p in rows]
        self.assertEqual(amounts(h["cards"][self.a.id]), [900, 800, 700])
        self.assertEqual(amounts(h["cards"][self.b.id])[0], 0)                 # a credit balance counts as 0
        # August is left out: card B had no statement yet, so the total would look too low.
        self.assertEqual([p["month"] for p in h["total"]], ["2026-09", "2026-10"])
        self.assertEqual(amounts(h["total"]), [800, 1000])

    def test_month_change_and_api(self):
        self.assertIsNone(month_change(self.user, "2026-08"))
        self.assertEqual(Decimal(month_change(self.user, "2026-10")["change"]), 200)
        client = APIClient()
        client.force_authenticate(self.user)
        data = client.get("/api/fp/debts/history/").data
        self.assertIn(str(self.a.id), {str(k) for k in data["cards"]})
        other = get_user_model().objects.create_user(email="e@example.test", username="e", password="x-Test-12345")
        client.force_authenticate(other)
        self.assertEqual(client.get("/api/fp/debts/history/").data, {"cards": {}, "total": []})
