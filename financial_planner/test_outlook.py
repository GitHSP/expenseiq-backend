from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Debt, RecurringItem
from .outlook import _debts, budget_for, build_outlook, simulate


def debt(id, balance, rate, pays, follows_interest=False, principal=10.0):
    return {"id": id, "name": f"D{id}", "balance": balance, "rate": rate / 100, "pays": pays,
            "follows_interest": follows_interest, "principal": principal, "limit": None}


class SimulateTests(TestCase):
    def test_no_interest_loan(self):
        out = simulate([debt(1, 1000, 0, 100)])
        self.assertEqual(out["months"], 10)
        self.assertEqual(out["interest"], 0)

    def test_payment_below_interest_never_finishes(self):
        self.assertIsNone(simulate([debt(1, 10000, 24, 100)])["months"])

    def test_card_minimum_follows_interest_so_it_runs_long(self):
        fixed = simulate([debt(1, 1000, 24, 30)])["months"]
        shrinking = simulate([debt(1, 1000, 24, 30, follows_interest=True, principal=10.0)])["months"]
        self.assertGreater(shrinking, fixed)

    def test_budget_clears_highest_rate_first_and_rolls_over(self):
        out = simulate([debt(1, 1000, 5, 10), debt(2, 1000, 20, 10)], budget=500)
        self.assertLess(out["finish"][2], out["finish"][1])
        self.assertEqual(out["months"], 5)

    def test_budget_for_is_the_lowest_that_works(self):
        ds = [debt(1, 12000, 12, 100)]
        need = budget_for(ds, 24)
        self.assertEqual(simulate(ds, budget=need, max_months=24)["months"], 24)
        self.assertIsNone(simulate(ds, budget=need - 1, max_months=24)["months"])


class OutlookTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="o@example.test", username="o", password="x-Test-12345")
        Debt.objects.create(user=self.user, name="Card", current_balance=Decimal("5000"), credit_limit=Decimal("5100"),
                            annual_interest_rate=Decimal("20"), minimum_payment=Decimal("100"), debt_type="credit_card")
        self.loan = Debt.objects.create(user=self.user, name="Loan", current_balance=Decimal("10000"), debt_type="loan",
                                        annual_interest_rate=Decimal("10"), minimum_payment=Decimal("200"))
        RecurringItem.objects.create(user=self.user, label="Pay", amount=Decimal("4000"), category="income")
        RecurringItem.objects.create(user=self.user, label="Rent", amount=Decimal("2500"), category="fixed_expense")

    def test_figures_without_statements_come_from_the_plan(self):
        out = build_outlook(self.user, target_months=24, today=date(2026, 1, 15))
        self.assertEqual(out["basis"]["source"], "plan")
        self.assertEqual(out["basis"]["budget"], 1500)
        self.assertEqual(out["total_debt"], 15000)
        pace, goal = out["scenarios"]["pace"], out["scenarios"]["goal"]
        self.assertEqual(goal["months"], 24)
        self.assertEqual(goal["date"], "2028-01-15")
        self.assertEqual(out["goal"]["income_needed"], goal["budget"] + 2500)
        self.assertEqual(out["goal"]["income_gap"], goal["budget"] - 1500)
        self.assertLess(pace["months"], out["scenarios"]["minimums"]["months"])
        titles = " ".join(a["title"] for a in out["actions"])
        self.assertIn("Card is close to its limit", titles)
        self.assertIn("Extra money saves the most on Card", titles)

    def test_entered_budget_and_short_budget(self):
        out = build_outlook(self.user, budget=100.0, today=date(2026, 1, 15))
        self.assertTrue(out["basis"]["entered"])
        self.assertIsNone(out["scenarios"]["pace"]["months"])
        self.assertTrue(any(a["title"].startswith("Payments are more") for a in out["actions"]))

    def test_goal_never_below_minimums(self):
        out = build_outlook(self.user, target_months=600, today=date(2026, 1, 15))
        self.assertGreaterEqual(out["goal"]["budget"], 300)

    def test_linked_recurring_payment_replaces_the_minimum(self):
        RecurringItem.objects.create(user=self.user, label="Loan payment", amount=Decimal("500"),
                                     category="fixed_expense", debt=self.loan)
        loan = next(d for d in _debts(self.user) if d["id"] == self.loan.id)
        self.assertEqual(loan["pays"], 500)

    def test_api_is_private_and_handles_bad_input(self):
        client = APIClient()
        self.assertEqual(client.get("/api/fp/outlook/").status_code, 401)
        client.force_authenticate(self.user)
        res = client.get("/api/fp/outlook/?target_months=abc&budget=nan")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["goal"]["target_months"], 36)
        self.assertFalse(res.data["basis"]["entered"])
        other = get_user_model().objects.create_user(email="p@example.test", username="p", password="x-Test-12345")
        client.force_authenticate(other)
        res = client.get("/api/fp/outlook/?target_months=12")
        self.assertFalse(res.data["has_debts"])
        self.assertEqual(res.data["total_debt"], 0)

    def test_utilization_milestones_and_each_debts_payment(self):
        out = build_outlook(self.user, budget=1000.0, today=date(2026, 1, 15))
        self.assertEqual(out["debts"][0]["utilization"], 98.0)
        pace = out["scenarios"]["pace"]
        card = next(d["id"] for d in out["debts"] if d["name"] == "Card")
        loan = next(d["id"] for d in out["debts"] if d["name"] == "Loan")
        # The 20% card takes everything above the loan's 200.
        self.assertEqual(pace["debts"][str(loan)]["payment"], 200)
        self.assertEqual(pace["debts"][str(card)]["payment"], 800)
        hit = pace["utilization"]["overall"]
        self.assertLess(hit["50"]["months"], hit["40"]["months"])
        self.assertLessEqual(hit["40"]["months"], hit["30"]["months"])
        # Only the card has a limit, so overall and the card's own match.
        self.assertEqual(hit, pace["utilization"]["cards"][str(card)])
        self.assertEqual(len(pace["balances"]), pace["months"])

    def test_already_under_a_target_is_zero_months(self):
        Debt.objects.filter(name="Card").update(current_balance=Decimal("1000"))
        out = build_outlook(self.user, budget=1000.0, today=date(2026, 1, 15))
        self.assertEqual(out["scenarios"]["pace"]["utilization"]["overall"]["30"]["months"], 0)
