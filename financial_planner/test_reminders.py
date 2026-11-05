from datetime import date
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from expenses.models import Account, Expense, SpendingBudget, Statement
from .models import Debt, RecurringItem
from .reminders import build_reminders
from .services import get_or_create_month_plan


class ReminderTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="r@example.test", username="r", password="x-Test-12345")
        self.today = date(2026, 3, 10)
        RecurringItem.objects.create(user=self.user, label="Phone", amount=Decimal("50"), due_day=12, category="fixed_expense")
        RecurringItem.objects.create(user=self.user, label="Rent", amount=Decimal("625"), due_day=1, category="fixed_expense")
        RecurringItem.objects.create(user=self.user, label="Pay", amount=Decimal("2000"), due_day=11, category="income")
        self.card = Debt.objects.create(user=self.user, name="Card", current_balance=Decimal("980"), credit_limit=Decimal("1000"),
                                        annual_interest_rate=Decimal("20"), minimum_payment=Decimal("30"), due_day=15)

    def test_due_soon_skips_income_past_and_ticked(self):
        r = build_reminders(self.user, today=self.today, days=7)
        self.assertEqual([d["label"] for d in r["due"]], ["Phone", "Card minimum payment"])
        self.assertEqual(r["due"][0]["days"], 2)
        plan, _ = get_or_create_month_plan(self.user, 2026, 3)
        phone = plan.checklist_items.get(label="Phone")
        phone.is_completed = True
        phone.save()
        r = build_reminders(self.user, today=self.today, days=7)
        self.assertEqual([d["label"] for d in r["due"]], ["Card minimum payment"])

    def test_month_end_rolls_into_next_month(self):
        r = build_reminders(self.user, today=date(2026, 3, 28), days=7)
        self.assertIn(("Rent", "2026-04-01"), [(d["label"], d["date"]) for d in r["due"]])

    def test_limits_budgets_and_uploads(self):
        SpendingBudget.objects.create(user=self.user, label="Food & Dining", category="Food & Dining", monthly_limit=Decimal("100"))
        Expense.objects.create(user=self.user, title="Lunch", amount=Decimal("90"), category="Food & Dining", date=self.today)
        acc = Account.objects.create(user=self.user, name="Chequing", kind="chequing")
        Statement.objects.create(user=self.user, account=acc, filename="a.pdf", period_start=date(2026, 1, 1),
                                 period_end=date(2026, 1, 31))
        r = build_reminders(self.user, today=self.today)
        self.assertEqual([c["label"] for c in r["limits"]], ["Card"])
        self.assertEqual(r["budgets"][0]["label"], "Food & Dining")
        self.assertEqual([u["label"] for u in r["uploads"]], ["Chequing"])

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_daily_email(self):
        from unittest import mock
        with mock.patch("financial_planner.reminders.timezone.localdate", return_value=self.today):
            call_command("send_reminders", stdout=StringIO())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Phone: $50.00 due in 2 days", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, ["r@example.test"])


class BudgetApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="b@example.test", username="b", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_add_list_delete(self):
        res = self.client.post("/api/fp/budgets/", {"category": "Shopping", "monthly_limit": "150"}, format="json")
        self.assertEqual(res.status_code, 201)
        budgets = self.client.get("/api/fp/budgets/").data["budgets"]
        self.assertEqual((budgets[0]["label"], budgets[0]["limit"]), ("Shopping", "150.00"))
        self.assertEqual(self.client.post("/api/fp/budgets/", {"category": "Nope", "monthly_limit": "1"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/fp/budgets/", {"category": "Shopping", "monthly_limit": "-5"}, format="json").status_code, 400)
        other = get_user_model().objects.create_user(email="c@example.test", username="c", password="x-Test-12345")
        client = APIClient()
        client.force_authenticate(other)
        self.assertEqual(client.delete(f"/api/fp/budgets/{budgets[0]['id']}/").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/fp/budgets/{budgets[0]['id']}/").status_code, 204)

    def test_recap_needs_data(self):
        self.assertEqual(self.client.get("/api/fp/recap/?month=2026-01").status_code, 200)
        self.assertEqual(self.client.get("/api/fp/recap/?month=junk").status_code, 400)


class RecapTests(TestCase):
    def test_defaults_to_the_last_month_all_statements_cover(self):
        from datetime import date as d
        from expenses.models import Account, Statement, StatementTransaction
        from unittest import mock
        user = get_user_model().objects.create_user(email="rc@example.test", username="rc", password="x-Test-12345")
        bank = Account.objects.create(user=user, name="Bank", kind="chequing")
        card = Account.objects.create(user=user, name="Card", kind="credit_card")
        st = Statement.objects.create(user=user, account=bank, filename="b.pdf", period_start=d(2026, 8, 1), period_end=d(2026, 10, 31))
        Statement.objects.create(user=user, account=card, filename="c.pdf", period_start=d(2026, 8, 20), period_end=d(2026, 9, 21))
        StatementTransaction.objects.create(user=user, statement=st, account=bank, date=d(2026, 8, 5),
                                            description="SHOP", title="Shop", amount=Decimal("-40"), kind="expense")
        RecurringItem.objects.create(user=user, label="Rent", amount=Decimal("600"), due_day=1, category="fixed_expense")
        client = APIClient()
        client.force_authenticate(user)
        with mock.patch("financial_planner.views.timezone.localdate", return_value=d(2026, 11, 5)):
            data = client.get("/api/fp/recap/").data
        self.assertEqual(data["month"], "2026-08")             # the card's statement ends Sep 21, so Sep isn't whole
        self.assertEqual([r["item"] for r in data["plan"]["items"]], ["Rent"])
