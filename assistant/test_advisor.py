from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from expenses.models import Account, Expense, MerchantRule, SpendingBudget, Statement, StatementTransaction
from financial_planner.models import Debt

from .advisor import get_alerts, monthly_review, plan_card_payments
from .insights import search_transactions
from .models import Memory
from .personal import add_rule, delete_rule, forget, list_budgets, remember, set_budget


class Base(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(email="adv@example.test", username="adv", password="x-Test-12345")
        self.other = User.objects.create_user(email="adv2@example.test", username="adv2", password="x-Test-12345")
        self.chq = Account.objects.create(user=self.user, name="CIBC Chequing •0001", kind="chequing", institution="CIBC")
        self.visa = Account.objects.create(user=self.user, name="CIBC Visa •0042", kind="credit_card", institution="CIBC", last4="0042")

    def statement(self, account, start, end, **summary):
        return Statement.objects.create(user=account.user, account=account, filename="s.pdf",
                                        period_start=start, period_end=end, **summary)

    def tx(self, statement, d, title, amount, kind="skipped", desc=None, expense=None):
        return StatementTransaction.objects.create(
            user=statement.user, statement=statement, account=statement.account, date=d, title=title,
            description=desc or title.upper(), amount=Decimal(amount), kind=kind, expense=expense)


class RuleTests(Base):
    def test_rename_and_categorise_existing_and_future(self):
        st = self.statement(self.visa, date(2026, 9, 1), date(2026, 9, 30))
        e = Expense.objects.create(user=self.user, title="Amznmktpca", amount=20, category="Other", date=date(2026, 9, 3))
        self.tx(st, date(2026, 9, 3), "Amznmktpca", "-20", kind="expense", desc="AMZNMktpCA*533DS7JR1 TORONTO ON", expense=e)
        r = add_rule(self.user, "amzn", rename_to="Amazon", category="Shopping")
        self.assertTrue(r["ok"])
        e.refresh_from_db()
        self.assertEqual((e.title, e.category), ("Amazon", "Shopping"))
        self.assertEqual(StatementTransaction.objects.get().title, "Amazon")
        # Future imports.
        from expenses.rules import apply_to_preview
        rows = apply_to_preview(self.user, [{"raw_description": "AMZN MKTP CA*XYZ", "title": "Amzn Mktp Ca",
                                             "expense_category": "Other", "is_transfer": False, "transfer_match": None}])
        self.assertEqual((rows[0]["title"], rows[0]["expense_category"]), ("Amazon", "Shopping"))

    def test_own_account_rule_stops_counting_it_as_spending(self):
        st = self.statement(self.chq, date(2026, 9, 1), date(2026, 9, 30))
        e = Expense.objects.create(user=self.user, title="E-Transfer Wealthsimple", amount=500, category="Other", date=date(2026, 9, 8))
        self.tx(st, date(2026, 9, 8), "E-Transfer Wealthsimple", "-500", kind="expense", expense=e)
        self.tx(st, date(2026, 9, 9), "Loblaws", "-60")
        self.assertEqual(search_transactions(self.user)["total_spent"], "560.00")
        r = add_rule(self.user, "wealthsimple", treat_as="transfer")
        self.assertEqual(r["entries_removed"], 1)
        self.assertFalse(Expense.objects.filter(id=e.id).exists())
        self.assertEqual(search_transactions(self.user)["total_spent"], "60.00")

    def test_rule_validation_and_delete(self):
        self.assertFalse(add_rule(self.user, "ab", rename_to="X")["ok"])
        self.assertFalse(add_rule(self.user, "tim hortons", category="Coffee")["ok"])     # not a category
        add_rule(self.user, "tim hortons", category="Food & Dining")
        self.assertTrue(delete_rule(self.user, "tim")["ok"])
        self.assertFalse(MerchantRule.objects.exists())


class BudgetAndMemoryTests(Base):
    def test_budget_tracks_spending(self):
        today = date.today()
        st = self.statement(self.visa, today.replace(day=1), today)
        self.tx(st, today, "Tim Hortons", "-45.00")
        self.assertTrue(set_budget(self.user, "Coffee", 60, theme="coffee")["ok"])
        b = list_budgets(self.user)["budgets"][0]
        self.assertEqual((b["spent"], b["left"], b["percent_used"]), ("45.00", "15.00", 75.0))
        self.tx(st, today, "Starbucks", "-20.00")
        alerts = get_alerts(self.user)["alerts"]
        self.assertTrue(any(a["type"] == "budget" and "Over budget" in a["title"] for a in alerts))
        self.assertFalse(set_budget(self.user, "X", 0, theme="coffee")["ok"])
        self.assertFalse(set_budget(self.user, "X", 50)["ok"])          # covers nothing

    def test_memory_is_per_user_and_reaches_the_prompt(self):
        self.assertTrue(remember(self.user, "Payday is every second Friday")["ok"])
        self.assertEqual(remember(self.user, "payday is every second friday")["message"], "Already noted.")
        Memory.objects.create(user=self.other, text="Someone else's note")
        client = APIClient()
        client.force_authenticate(self.user)
        answer = NS(stop_reason="end_turn", content=[NS(type="text", text="Hi!")])
        with mock.patch("assistant.views.anthropic.Anthropic") as A, \
             mock.patch("assistant.views.settings.ANTHROPIC_API_KEY", "k"):
            A.return_value.messages.create.return_value = answer
            client.post("/api/assistant/chat/", {"messages": [{"role": "user", "content": "hi"}]}, format="json")
            system = A.return_value.messages.create.call_args.kwargs["system"]
        self.assertIn("Payday is every second Friday", system)
        self.assertNotIn("Someone else's note", system)
        self.assertTrue(forget(self.user, "payday")["ok"])
        self.assertFalse(Memory.objects.filter(user=self.user).exists())


class AlertTests(Base):
    def test_price_rise_fee_and_double_charge(self):
        st = self.statement(self.visa, date(2026, 6, 1), date(2026, 9, 30))
        for m in (6, 7, 8):
            self.tx(st, date(2026, m, 5), "Netflix.Com", "-16.49")
        self.tx(st, date(2026, 9, 5), "Netflix.Com", "-18.99")
        self.tx(st, date(2026, 9, 10), "Tim Hortons", "-3.87")
        self.tx(st, date(2026, 9, 10), "Tim Hortons", "-3.87")                 # two coffees: fine
        self.tx(st, date(2026, 9, 12), "Best Buy", "-249.99", desc="BEST BUY #123")
        self.tx(st, date(2026, 9, 12), "Best Buy", "-249.99", desc="BEST BUY #123")
        self.tx(st, date(2026, 9, 20), "Foreign Transaction Fee", "-2.50")
        alerts = {a["type"]: a for a in get_alerts(self.user)["alerts"]}
        self.assertIn("16.49 → $18.99", alerts["price_increase"]["detail"])
        self.assertIn("Best Buy", alerts["possible_duplicate"]["detail"])
        self.assertNotIn("Tim Hortons", alerts["possible_duplicate"]["detail"])
        self.assertIn("$2.50", alerts["fees"]["title"])                         # coffee isn't a fee

    def test_overdraft_risk_before_payday(self):
        st = self.statement(self.chq, date(2026, 6, 1), date(2026, 9, 30), new_balance=Decimal("300.00"))
        for m in (6, 7, 8, 9):
            self.tx(st, date(2026, m, 1), "Realstar Corp", "-1500", desc="PREAUTHORIZED DEBIT REALSTAR CORP")
            self.tx(st, date(2026, m, 15), "Payroll", "2000", desc="PAYROLL DEPOSIT ACME")
        alerts = [a for a in get_alerts(self.user)["alerts"] if a["type"] == "overdraft_risk"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("Realstar", alerts[0]["detail"])

    def test_alerts_endpoint_is_private(self):
        self.assertEqual(APIClient().get("/api/assistant/alerts/").status_code, 401)
        c = APIClient()
        c.force_authenticate(self.other)
        self.assertEqual(c.get("/api/assistant/alerts/").data["alerts"], [])


class PlanAndReviewTests(Base):
    def setUp(self):
        super().setUp()
        debt = Debt.objects.create(user=self.user, name="CIBC Visa", current_balance=3000, annual_interest_rate="21.99",
                                   minimum_payment=70, debt_type="credit_card")
        self.visa.debt = debt
        self.visa.save()
        self.statement(self.visa, date(2026, 9, 15), date(2026, 10, 14), new_balance=Decimal("3000.00"),
                       minimum_payment=Decimal("70.00"), payment_due=date(2026, 11, 8))
        Debt.objects.create(user=self.user, name="Store Card", current_balance=800, annual_interest_rate="29.99",
                            minimum_payment=25, debt_type="credit_card")

    def test_plan_puts_extra_on_the_highest_rate(self):
        p = plan_card_payments(self.user, monthly_budget=500)
        self.assertTrue(p["ok"], p)
        plan = p["plans"][0]
        this_month = {x["card"]: Decimal(x["pay"]) for x in plan["this_month"]}
        self.assertEqual(sum(this_month.values()), Decimal("500.00"))
        self.assertGreater(this_month["Store Card"], Decimal("25"))            # 29.99% card gets the extra
        self.assertLess(plan["months_to_debt_free"], p["minimums_only"]["months_to_debt_free"])
        self.assertGreater(Decimal(plan["interest_saved_vs_minimums"]), 0)
        self.assertFalse(plan_card_payments(self.user, monthly_budget=50)["ok"])   # below the minimums

    def test_monthly_review_compares_months(self):
        st = self.statement(self.chq, date(2026, 8, 1), date(2026, 9, 30))
        self.tx(st, date(2026, 8, 3), "Payroll", "3000")
        self.tx(st, date(2026, 8, 5), "Loblaws", "-200")
        self.tx(st, date(2026, 9, 3), "Payroll", "3000")
        self.tx(st, date(2026, 9, 5), "Loblaws", "-350")
        self.tx(st, date(2026, 9, 6), "To Visa", "-500", kind="transfer")
        r = monthly_review(self.user, month="2026-09")
        self.assertEqual((r["income"], r["spent"], r["spent_last_month"], r["saved"]), ("3000.00", "350.00", "200.00", "2650.00"))
        self.assertEqual(r["biggest_merchant_changes"][0]["change"], "150.00")
