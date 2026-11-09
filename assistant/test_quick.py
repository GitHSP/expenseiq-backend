from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from expenses.models import Expense
from financial_planner.models import Debt, RecurringItem, SavingsGoal
from .quick import answer


class QuickAnswerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="qa@example.test", username="qa", password="x-Test-12345")
        self.today = date(2026, 3, 10)
        Debt.objects.create(user=self.user, name="Card", current_balance=Decimal("1000"), credit_limit=Decimal("2000"),
                            annual_interest_rate=Decimal("24"), minimum_payment=Decimal("40"), due_day=12)
        RecurringItem.objects.create(user=self.user, label="Pay", amount=Decimal("3000"), due_day=1, category="income")
        RecurringItem.objects.create(user=self.user, label="Rent", amount=Decimal("1500"), due_day=1, category="fixed_expense")
        Expense.objects.create(user=self.user, title="Lunch", amount=Decimal("25"), category="Food & Dining", date=self.today)
        SavingsGoal.objects.create(user=self.user, name="Trip", target=Decimal("1000"), saved=Decimal("250"))

    def ask(self, q):
        return answer(self.user, q, today=self.today)

    def test_common_questions(self):
        self.assertEqual(self.ask("When am I debt free?")["intent"], "debt_free")
        self.assertIn("debt-free by", self.ask("when will i be debt-free")["reply"])
        self.assertIn("Card minimum payment: $40.00", self.ask("What's due this week?")["reply"])
        self.assertIn("$25.00", self.ask("How much did I spend this month?")["reply"])
        self.assertIn("Trip: $250.00 of $1,000.00 (25%)", self.ask("how are my goals")["reply"])
        self.assertIn("$20.00 a month in interest", self.ask("how much interest am i paying")["reply"])
        self.assertIn("50%", self.ask("what's my credit utilization")["reply"])
        self.assertEqual(self.ask("which card should I pay first?")["intent"], "extra")
        self.assertEqual(self.ask("What's my net worth")["intent"], "net_worth")
        self.assertEqual(self.ask("how much can I still spend this month")["intent"], "left_to_spend")

    def test_other_questions_go_to_the_ai(self):
        self.assertIsNone(self.ask("Write me a poem about glaciers"))
        self.assertIsNone(self.ask("x " * 100))

    @override_settings(ANTHROPIC_API_KEY="")
    def test_chat_answers_without_the_ai(self):
        client = APIClient()
        client.force_authenticate(self.user)
        res = client.post("/api/assistant/chat/", {"messages": [{"role": "user", "content": "what's due this week"}]}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["quick"])
        self.assertIn("Card minimum payment", res.data["reply"])
        res = client.post("/api/assistant/chat/", {"messages": [{"role": "user", "content": "tell me a joke"}]}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertIn("I can't reach my AI right now", res.data["reply"])
        self.assertIn("When am I debt-free?", res.data["reply"])
