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
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["r@example.test"])
        self.assertEqual(msg.subject, "Glacier: 1 payment due this week · CA$50.00")    # the card is due after 3 days
        self.assertIn("Phone  CA$50.00", msg.body)                     # plain-text version
        html, kind = msg.alternatives[0]
        self.assertEqual(kind, "text/html")
        self.assertIn("Open Glacier", html)
        self.assertIn("Cards near their limit", html)


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


class StatementCloseAndChangeTests(TestCase):
    def setUp(self):
        from expenses.models import Account, Statement, StatementTransaction
        self.user = get_user_model().objects.create_user(email="sc@example.test", username="sc", password="x-Test-12345")
        self.card = Debt.objects.create(user=self.user, name="Visa", current_balance=Decimal("800"), credit_limit=Decimal("1000"),
                                        annual_interest_rate=Decimal("20"), minimum_payment=Decimal("25"))
        acc = Account.objects.create(user=self.user, name="Visa card", kind="credit_card", debt=self.card)
        self.st = Statement.objects.create(user=self.user, account=acc, filename="v.pdf", period_start=date(2026, 2, 16),
                                           period_end=date(2026, 3, 15), new_balance=Decimal("800"))
        # Four months at the usual price, then a higher one.
        for day, amt in ((date(2025, 11, 5), "15.99"), (date(2025, 12, 5), "15.99"), (date(2026, 1, 5), "15.99"),
                         (date(2026, 2, 5), "15.99"), (date(2026, 3, 5), "22.99")):
            StatementTransaction.objects.create(user=self.user, statement=self.st, account=acc, date=day,
                                                description="STREAMCO", title="StreamCo", amount=-Decimal(amt), kind="expense",
                                                category="Subscriptions")

    def test_card_closing_soon_and_amount_to_get_under_30(self):
        r = build_reminders(self.user, today=date(2026, 4, 10))
        self.assertEqual(r["closing"], [{"type": "close", "label": "Visa", "date": "2026-04-15", "days": 5,
                                         "percent": 80, "to_target": Decimal("500.00")}])
        self.assertEqual(build_reminders(self.user, today=date(2026, 4, 1))["closing"], [])   # 14 days away
        Debt.objects.filter(pk=self.card.pk).update(current_balance=Decimal("200"))
        self.assertEqual(build_reminders(self.user, today=date(2026, 4, 10))["closing"], [])  # already under 30%

    def test_price_went_up(self):
        changes = build_reminders(self.user, today=date(2026, 3, 20))["changes"]
        self.assertEqual([(c["kind"], c["label"], c["amount"], c["usual"]) for c in changes],
                         [("up", "StreamCo", Decimal("22.99"), Decimal("15.99"))])


class SavingsGoalTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="sg@example.test", username="sg", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_goal_progress_and_adding_money(self):
        res = self.client.post("/api/fp/goals/", {"name": "Trip", "target": "1200", "target_date": "2099-12-01"}, format="json")
        self.assertEqual(res.status_code, 201)
        gid = res.data["id"]
        res = self.client.patch(f"/api/fp/goals/{gid}/", {"add": "300"}, format="json")
        self.assertEqual((res.data["saved"], res.data["left"], res.data["percent"]), ("300.00", "900.00", 25.0))
        self.assertIsNotNone(res.data["monthly"])
        res = self.client.patch(f"/api/fp/goals/{gid}/", {"add": "-500"}, format="json")
        self.assertEqual(res.data["saved"], "0.00")                       # never below zero
        self.assertEqual(self.client.post("/api/fp/goals/", {"name": "x", "target": "10"}, format="json").status_code, 400)
        other = get_user_model().objects.create_user(email="sh@example.test", username="sh", password="x-Test-12345")
        client = APIClient()
        client.force_authenticate(other)
        self.assertEqual(client.patch(f"/api/fp/goals/{gid}/", {"add": "5"}, format="json").status_code, 404)

class WeeklySummaryTests(TestCase):
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_weekly_email(self):
        from unittest import mock
        user = get_user_model().objects.create_user(email="w@example.test", username="w", password="x-Test-12345")
        Expense.objects.create(user=user, title="Lunch", amount=Decimal("40"), category="Food & Dining", date=date(2026, 3, 12))
        Expense.objects.create(user=user, title="Older", amount=Decimal("80"), category="Food & Dining", date=date(2026, 2, 20))
        with mock.patch("financial_planner.weekly.timezone.localdate", return_value=date(2026, 3, 15)), \
             mock.patch("financial_planner.reminders.timezone.localdate", return_value=date(2026, 3, 15)), \
             mock.patch("financial_planner.management.commands.send_reminders.timezone.localdate", return_value=date(2026, 3, 15)):
            call_command("send_reminders", stdout=StringIO())                 # Mar 15, 2026 is a Sunday
        weekly = [m for m in mail.outbox if "Your week" in m.subject]
        self.assertEqual(len(weekly), 1)
        self.assertEqual(weekly[0].subject, "Glacier: Your week: CA$40.00 spent, CA$30.00 more than usual")
        self.assertIn("Food &amp; Dining", weekly[0].alternatives[0][0])
