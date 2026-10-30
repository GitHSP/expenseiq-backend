import json
from datetime import datetime, timezone as dt_timezone
import tempfile
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import ChecklistItem, Debt, EmergencyFund, MonthlyPlan, RecurringItem
from .services import get_or_create_month_plan, sync_month_checklist


def make_user(email="planner@example.test"):
    return get_user_model().objects.create_user(email=email, username=email.split("@")[0], password="x-Test-12345")


class MonthlyChecklistTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.card = Debt.objects.create(
            user=self.user, name="Card", current_balance=Decimal("1000.00"),
            annual_interest_rate=Decimal("24.00"), minimum_payment=Decimal("50.00"),
            due_day=12, avalanche_order=1,
        )
        self.emi = Debt.objects.create(
            user=self.user, name="Phone EMI", debt_type="other", current_balance=Decimal("30.00"),
            annual_interest_rate=Decimal("0"), minimum_payment=Decimal("47.04"),
            due_day=28, avalanche_order=2,
        )
        self.rent = RecurringItem.objects.create(
            user=self.user, label="Rent", amount=Decimal("650.00"), due_day=26,
        )
        RecurringItem.objects.create(
            user=self.user, label="Salary", amount=Decimal("2000.00"), category="income",
        )

    def labels(self, plan):
        return sorted(plan.checklist_items.values_list("label", flat=True))

    def test_first_plan_builds_checklist_without_interest(self):
        plan, rolled_over = get_or_create_month_plan(self.user, 2026, 9)
        self.assertFalse(rolled_over)
        self.assertEqual(self.labels(plan), [
            "Card minimum payment", "Card — EXTRA avalanche payment (all surplus)",
            "Phone EMI payment", "Rent",   # income counts toward totals, not the checklist
        ])
        self.card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal("1000.00"))

    def test_emi_payment_is_capped_at_remaining_balance(self):
        plan, _ = get_or_create_month_plan(self.user, 2026, 9)
        emi_item = plan.checklist_items.get(linked_debt=self.emi, category="debt_min")
        self.assertEqual(emi_item.amount, Decimal("30.00"))

    def test_surplus_goes_to_extra_payment(self):
        plan, _ = get_or_create_month_plan(self.user, 2026, 9)
        extra = plan.checklist_items.get(category="debt_extra")
        # Surplus is 2000 - 650 - 50 - 30 = 1270, capped at the card's 1000 balance.
        self.assertEqual(extra.amount, Decimal("1000.00"))
        self.assertEqual(extra.linked_debt, self.card)

    def test_new_month_rolls_over_once(self):
        sept, _ = get_or_create_month_plan(self.user, 2026, 9)
        ChecklistItem.objects.create(monthly_plan=sept, label="One-off dentist", amount=80, category="fixed_expense")

        oct_plan, rolled_over = get_or_create_month_plan(self.user, 2026, 10)
        self.assertTrue(rolled_over)
        self.card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal("1020.00"))  # 24% APR = 2%/month
        self.assertNotIn("One-off dentist", self.labels(oct_plan))
        self.assertIn("Rent", self.labels(oct_plan))

        again, rolled_over = get_or_create_month_plan(self.user, 2026, 10)
        self.assertEqual(again.id, oct_plan.id)
        self.assertFalse(rolled_over)
        self.card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal("1020.00"))

    def test_skipped_months_get_catch_up_interest(self):
        get_or_create_month_plan(self.user, 2026, 9)
        get_or_create_month_plan(self.user, 2026, 11)
        self.card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal("1040.40"))

    def test_sync_respects_ticked_and_manual_items(self):
        plan, _ = get_or_create_month_plan(self.user, 2026, 9)
        rent_item = plan.checklist_items.get(recurring_item=self.rent)
        rent_item.is_completed = True
        rent_item.save()
        manual = ChecklistItem.objects.create(monthly_plan=plan, label="Dentist", amount=80, category="fixed_expense")

        self.rent.amount = Decimal("700.00")
        self.rent.save()
        sync_month_checklist(plan)

        rent_item.refresh_from_db()
        self.assertEqual(rent_item.amount, Decimal("650.00"))  # ticked = history
        self.assertTrue(ChecklistItem.objects.filter(id=manual.id).exists())

    def test_sync_updates_and_removes_unticked_items(self):
        plan, _ = get_or_create_month_plan(self.user, 2026, 9)
        self.rent.amount = Decimal("700.00")
        self.rent.save()
        sync_month_checklist(plan)
        self.assertEqual(plan.checklist_items.get(recurring_item=self.rent).amount, Decimal("700.00"))

        self.rent.is_active = False
        self.rent.save()
        sync_month_checklist(plan)
        self.assertNotIn("Rent", self.labels(plan))

    def test_paid_off_debt_drops_off(self):
        plan, _ = get_or_create_month_plan(self.user, 2026, 9)
        self.emi.current_balance = Decimal("0")
        self.emi.is_active = False
        self.emi.save()
        next_plan, _ = get_or_create_month_plan(self.user, 2026, 10)
        self.assertNotIn("Phone EMI payment", self.labels(next_plan))

    def test_deleting_debt_removes_its_unticked_payments(self):
        plan, _ = get_or_create_month_plan(self.user, 2026, 9)
        client = APIClient()
        client.force_authenticate(self.user)
        with mock.patch("financial_planner.services.timezone.now",
                        return_value=datetime(2026, 9, 15, tzinfo=dt_timezone.utc)):
            res = client.delete(f"/api/fp/debts/{self.emi.id}/")
        self.assertEqual(res.status_code, 204)
        self.assertNotIn("Phone EMI payment", self.labels(plan))

    def test_savings_item_until_fund_is_full(self):
        fund = EmergencyFund.objects.create(user=self.user, current_balance=0, target_amount=1000, monthly_contribution=100)
        plan, _ = get_or_create_month_plan(self.user, 2026, 9)
        self.assertTrue(plan.checklist_items.filter(category="savings").exists())
        fund.current_balance = 1000
        fund.save()
        sync_month_checklist(plan)
        self.assertFalse(plan.checklist_items.filter(category="savings").exists())


class RecurringApiTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_current_plan_endpoint_rolls_over(self):
        now = timezone.now()
        prev_year, prev_month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        MonthlyPlan.objects.create(user=self.user, year=prev_year, month=prev_month)
        RecurringItem.objects.create(user=self.user, label="Rent", amount=650, due_day=26)

        res = self.client.get("/api/fp/plans/current/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["rolled_over"])
        self.assertEqual([i["label"] for i in res.data["checklist_items"]], ["Rent"])

        res = self.client.get("/api/fp/plans/current/")
        self.assertFalse(res.data["rolled_over"])

    def test_recurring_crud_syncs_current_month(self):
        self.client.get("/api/fp/plans/current/")
        res = self.client.post("/api/fp/recurring/", {"label": "Gym", "amount": "18.00", "due_day": 17}, format="json")
        self.assertEqual(res.status_code, 201)
        plan = MonthlyPlan.objects.get(user=self.user)
        self.assertEqual(list(plan.checklist_items.values_list("label", flat=True)), ["Gym"])

        self.client.delete(f"/api/fp/recurring/{res.data['id']}/")
        self.assertFalse(plan.checklist_items.exists())

    def test_recurring_is_per_user(self):
        other = make_user("other@example.test")
        item = RecurringItem.objects.create(user=other, label="Theirs", amount=1)
        self.assertEqual(self.client.get("/api/fp/recurring/").data, [])
        self.assertEqual(self.client.delete(f"/api/fp/recurring/{item.id}/").status_code, 404)

    def test_rejects_bad_due_day(self):
        res = self.client.post("/api/fp/recurring/", {"label": "X", "amount": "1", "due_day": 40}, format="json")
        self.assertEqual(res.status_code, 400)


class ImportPlannerCommandTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def run_import(self, data, *args):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(data, f)
        out = StringIO()
        call_command("import_planner", f.name, "--email", self.user.email, *args, stdout=out)
        return out.getvalue()

    def test_renames_by_alias_without_duplicating(self):
        Debt.objects.create(user=self.user, name="RBC Visa #1", current_balance=1773,
                            annual_interest_rate=21.99, minimum_payment=40)
        self.run_import({"debts": [{
            "name": "RBC Visa #2K", "aliases": ["RBC Visa #1"], "current_balance": 1894,
            "annual_interest_rate": 21.99, "minimum_payment": 40,
        }]})
        self.assertEqual(list(Debt.objects.values_list("name", "current_balance")),
                         [("RBC Visa #2K", Decimal("1894.00"))])

    def test_keep_existing_balance(self):
        Debt.objects.create(user=self.user, name="Education Loan", current_balance=20000,
                            annual_interest_rate=9.9, minimum_payment=500)
        self.run_import({"debts": [{
            "name": "Education Loan", "debt_type": "loan", "current_balance": 1,
            "keep_existing_balance": True, "annual_interest_rate": 9.9, "minimum_payment": 500,
        }]})
        self.assertEqual(Debt.objects.get().current_balance, Decimal("20000.00"))

    def test_import_replaces_current_checklist_without_interest(self):
        now = timezone.now()
        MonthlyPlan.objects.create(user=self.user, year=2020, month=1)
        old_plan = MonthlyPlan.objects.create(user=self.user, year=now.year, month=now.month)
        ChecklistItem.objects.create(monthly_plan=old_plan, label="Old item", category="fixed_expense")

        self.run_import({
            "debts": [{"name": "Card", "current_balance": 500, "annual_interest_rate": 20, "minimum_payment": 25}],
            "recurring": [{"label": "Rent", "amount": 650, "due_day": 26}],
        })
        plan = MonthlyPlan.objects.get(user=self.user, year=now.year, month=now.month)
        labels = set(plan.checklist_items.values_list("label", flat=True))
        self.assertNotIn("Old item", labels)
        self.assertIn("Rent", labels)
        self.assertEqual(Debt.objects.get().current_balance, Decimal("500.00"))

    def test_dry_run_saves_nothing(self):
        out = self.run_import({"recurring": [{"label": "Rent", "amount": 650}]}, "--dry-run")
        self.assertIn("Dry run", out)
        self.assertFalse(RecurringItem.objects.exists())


class DebtOriginalCurrencyTests(TestCase):
    """A loan paid in INR keeps its rupee amounts as typed and follows interest and payments."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        self.user = get_user_model().objects.create_user(email="do@example.test", username="do", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        res = self.client.post("/api/fp/debts/", {
            "name": "Education Loan", "debt_type": "loan", "current_balance": "4426.25", "annual_interest_rate": "12",
            "minimum_payment": "442.63", "original_currency": "INR", "original_current_balance": "300000",
            "original_minimum_payment": "30000"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.debt_id = res.data["id"]

    def debt(self):
        from financial_planner.models import Debt
        return Debt.objects.get(id=self.debt_id)

    def test_kept_as_typed(self):
        d = self.debt()
        self.assertEqual((d.original_currency, d.original_current_balance, d.original_minimum_payment),
                         ("INR", Decimal("300000.00"), Decimal("30000.00")))

    def test_interest_moves_the_rupee_balance_too(self):
        from financial_planner.services import _apply_monthly_interest
        _apply_monthly_interest(self.user, 1)
        d = self.debt()
        self.assertEqual(d.current_balance, Decimal("4470.51"))           # + 1% interest
        self.assertEqual(d.original_current_balance, Decimal("303000.00"))  # same 1%, in rupees

    def test_checklist_payment_in_rupees(self):
        from financial_planner.services import get_or_create_month_plan, toggle_checklist_item
        from datetime import date
        today = date.today()
        plan = get_or_create_month_plan(self.user, today.year, today.month)
        plan = plan[0] if isinstance(plan, tuple) else plan
        item = plan.checklist_items.get(linked_debt_id=self.debt_id, category="debt_min")
        self.assertEqual((item.original_amount, item.original_currency), (Decimal("30000.00"), "INR"))
        toggle_checklist_item(item)
        d = self.debt()
        self.assertEqual(d.current_balance, Decimal("3983.62"))
        self.assertEqual(d.original_current_balance, Decimal("270000.00"))   # 300,000 - 30,000

    def test_editing_back_to_cad_clears(self):
        res = self.client.patch(f"/api/fp/debts/{self.debt_id}/", {"current_balance": "100", "original_currency": ""}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        d = self.debt()
        self.assertEqual((d.original_currency, d.original_current_balance), ("", None))


class RecurringFrequencyTests(TestCase):
    """Biweekly/weekly pay gets one checklist item per payday; 'varies' is an estimate."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        self.user = get_user_model().objects.create_user(email="rf@example.test", username="rf", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def add(self, **data):
        res = self.client.post("/api/fp/recurring/", {"category": "income", **data}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data

    def test_paydays(self):
        from datetime import date
        from types import SimpleNamespace as NS
        from financial_planner.services import paydays
        rec = NS(frequency="biweekly", anchor_date=date(2026, 10, 2))      # a Friday
        self.assertEqual([d.day for d in paydays(rec, 2026, 10)], [2, 16, 30])   # 3 paydays
        self.assertEqual([d.day for d in paydays(rec, 2026, 11)], [13, 27])
        self.assertEqual([d.day for d in paydays(rec, 2026, 9)], [4, 18])        # before the anchor too
        self.assertEqual(len(paydays(NS(frequency="weekly", anchor_date=date(2026, 10, 2)), 2026, 10)), 5)

    def test_biweekly_needs_a_payday(self):
        res = self.client.post("/api/fp/recurring/", {"label": "Pay", "amount": "1200", "category": "income",
                                                      "frequency": "biweekly"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_checklist_and_planned_income(self):
        from financial_planner.services import get_or_create_month_plan
        self.add(label="Job pay", amount="1200", frequency="biweekly", anchor_date="2026-10-02")
        self.add(label="Cleaning", amount="600", frequency="monthly", due_day=1)
        self.add(label="Tips", amount="300", frequency="varies")
        plan = get_or_create_month_plan(self.user, 2026, 10)
        plan = plan[0] if isinstance(plan, tuple) else plan
        self.assertFalse(plan.checklist_items.filter(category="income").exists())   # income isn't a to-do
        plan.refresh_from_db()
        self.assertEqual(plan.total_income, Decimal("4500.00"))          # 3 paydays × 1,200 + 600 + 300
        nov = get_or_create_month_plan(self.user, 2026, 11)
        nov = nov[0] if isinstance(nov, tuple) else nov
        nov.refresh_from_db()
        self.assertEqual(nov.total_income, Decimal("3300.00"))           # 2 paydays in November
        from assistant.advisor_income import planned_income_rows
        rows = {r["label"]: r["amount"] for r in planned_income_rows(self.user, 2026, 10)}
        self.assertEqual(rows["Job pay"], Decimal("3600.00"))

    def test_old_ticked_income_leaves_the_checklist(self):
        from financial_planner.models import ChecklistItem, RecurringItem
        from financial_planner.services import get_or_create_month_plan, sync_month_checklist
        pay = self.add(label="Cleaning", amount="600", frequency="monthly", due_day=1)
        plan = get_or_create_month_plan(self.user, 2026, 10)
        plan = plan[0] if isinstance(plan, tuple) else plan
        ChecklistItem.objects.create(monthly_plan=plan, label="Cleaning", amount=600, category="income",   # from before
                                     recurring_item=RecurringItem.objects.get(pk=pay["id"]), is_completed=True)
        sync_month_checklist(plan)
        self.assertFalse(plan.checklist_items.filter(category="income").exists())


class RecurringPaysDebtTests(TestCase):
    """A recurring payment linked to a debt is that debt's payment: listed once,
    counted once, and ticking it lowers the balance (in rupees for an INR loan)."""

    def setUp(self):
        self.user = make_user("loan@example.test")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.loan = Debt.objects.create(
            user=self.user, name="Study loan", debt_type="loan", current_balance=Decimal("7000.00"),
            annual_interest_rate=Decimal("0"), minimum_payment=Decimal("400.00"),
            original_currency="INR", original_current_balance=Decimal("500000.00"),
            original_minimum_payment=Decimal("28000.00"))

    def test_listed_once_and_ticking_pays_the_loan(self):
        from financial_planner.services import toggle_checklist_item
        res = self.client.post("/api/fp/recurring/", {
            "label": "Loan EMI", "amount": "420.00", "original_amount": "30000.00", "original_currency": "INR",
            "category": "transfer", "due_day": 5, "debt": self.loan.id}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        plan, _ = get_or_create_month_plan(self.user, 2026, 10)
        loan_items = plan.checklist_items.filter(linked_debt=self.loan)
        self.assertEqual(loan_items.count(), 1)                       # not the EMI *and* a minimum
        item = loan_items.get()
        self.assertEqual((item.label, item.category, item.amount), ("Loan EMI", "debt_min", Decimal("420.00")))
        plan.refresh_from_db()
        self.assertEqual(plan.total_debt_payments, Decimal("420.00"))
        self.assertEqual(plan.total_fixed_expenses, Decimal("0.00"))
        toggle_checklist_item(item)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.original_current_balance, Decimal("470000.00"))   # exactly ₹30,000 less

    def test_unlinked_loan_keeps_its_own_payment(self):
        plan, _ = get_or_create_month_plan(self.user, 2026, 10)
        self.assertEqual(plan.checklist_items.get(linked_debt=self.loan).label, "Study loan payment")

    def test_paid_off_loan_stops_the_payment(self):
        RecurringItem.objects.create(user=self.user, label="Loan EMI", amount=Decimal("420"), category="transfer",
                                     debt=self.loan)
        self.loan.current_balance = Decimal("0")
        self.loan.save()
        plan, _ = get_or_create_month_plan(self.user, 2026, 10)
        self.assertFalse(plan.checklist_items.filter(label="Loan EMI").exists())

    def test_someone_elses_debt_is_refused(self):
        other = make_user("other-loan@example.test")
        theirs = Debt.objects.create(user=other, name="x", current_balance=1, annual_interest_rate=0, minimum_payment=1)
        res = self.client.post("/api/fp/recurring/", {"label": "x", "amount": "1", "debt": theirs.id}, format="json")
        self.assertEqual(res.status_code, 400)


class NextMonthPreviewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.card = Debt.objects.create(user=self.user, name="Card", current_balance=Decimal("1000.00"),
                                        annual_interest_rate=Decimal("24.00"), minimum_payment=Decimal("50.00"),
                                        due_day=12, avalanche_order=1)
        RecurringItem.objects.create(user=self.user, label="Pay", amount=Decimal("1000"), category="income",
                                     frequency="biweekly", anchor_date=timezone.datetime(2026, 1, 2).date())
        RecurringItem.objects.create(user=self.user, label="Rent", amount=Decimal("625"), due_day=1,
                                     category="fixed_expense")

    def test_lists_next_months_payments_without_changing_anything(self):
        plans = MonthlyPlan.objects.count()
        res = self.client.get("/api/fp/plans/next/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(MonthlyPlan.objects.count(), plans)
        self.card.refresh_from_db()
        self.assertEqual(self.card.current_balance, Decimal("1000.00"))         # no interest applied
        self.assertEqual([r["label"] for r in res.data["payments"]], ["Rent", "Card minimum payment"])
        self.assertEqual(res.data["payments_total"], Decimal("675.00"))
        self.assertEqual(res.data["left_over"], res.data["income_total"] - Decimal("675.00"))

    def test_december_rolls_to_january(self):
        from datetime import date
        from .services import preview_next_month
        out = preview_next_month(self.user, today=date(2026, 12, 15))
        self.assertEqual((out["year"], out["month"]), (2027, 1))
        # Biweekly from Jan 2, 2026: Jan 1, 15, 29 of 2027 are paydays.
        self.assertEqual(len(out["income"]), 3)
