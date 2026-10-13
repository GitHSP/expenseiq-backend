from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from expenses.models import Expense
from financial_planner.models import ChecklistItem, Debt, EmergencyFund, MonthlyPlan, RecurringItem
from financial_planner.services import get_or_create_month_plan, toggle_checklist_item

from .context import activity_context
from .models import ActivityLog


def make_user(email="activity@example.test"):
    return get_user_model().objects.create_user(email=email, username=email.split("@")[0], password="x-Test-12345")


class ActivityRecordingTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.debt = Debt.objects.create(
            user=self.user, name="CIBC Visa", current_balance=Decimal("3494.00"),
            annual_interest_rate=Decimal("24.00"), minimum_payment=Decimal("70.00"), due_day=13,
        )

    def logs(self, **filters):
        return list(ActivityLog.objects.filter(user=self.user, **filters).order_by("id"))

    def test_creating_a_debt_is_logged(self):
        log = self.logs(area="debt")[0]
        self.assertEqual((log.action, log.title, log.source), ("created", "CIBC Visa", "app"))
        self.assertIn({"field": "current_balance", "label": "Balance", "before": None, "after": "3494.00", "money": True}, log.changes)

    def test_debt_edit_records_before_and_after(self):
        self.debt.current_balance = Decimal("3000.00")
        self.debt.minimum_payment = Decimal("60.00")
        self.debt.save()
        log = self.logs(area="debt", action="updated")[0]
        self.assertEqual(
            [(c["field"], c["before"], c["after"]) for c in log.changes],
            [("current_balance", "3494.00", "3000.00"), ("minimum_payment", "70.00", "60.00")],
        )

    def test_untracked_field_changes_are_not_logged(self):
        self.debt.avalanche_order = 5
        self.debt.save()
        self.assertEqual(self.logs(area="debt", action="updated"), [])

    def test_ticking_a_payment_logs_the_tick_and_the_balance_drop(self):
        plan = MonthlyPlan.objects.create(user=self.user, year=2026, month=10)
        item = ChecklistItem.objects.create(monthly_plan=plan, label="CIBC Visa minimum payment", amount=70,
                                            category="debt_min", linked_debt=self.debt)
        toggle_checklist_item(item)

        tick = self.logs(area="checklist")[0]
        self.assertEqual((tick.action, tick.title), ("ticked", "CIBC Visa minimum payment"))
        self.assertEqual(tick.details, {"amount": "70.00", "month": "2026-10"})

        drop = self.logs(area="debt", action="updated")[0]
        self.assertEqual(drop.changes[0]["before"], "3494.00")
        self.assertEqual(drop.changes[0]["after"], "3424.00")
        self.assertEqual(drop.reason, 'Ticked "CIBC Visa minimum payment"')

    def test_checklist_creation_and_syncing_is_not_logged(self):
        RecurringItem.objects.create(user=self.user, label="Rent", amount=650, due_day=26)
        get_or_create_month_plan(self.user, 2026, 9)
        self.assertEqual(self.logs(area="checklist"), [])

    def test_month_rollover_logs_interest_as_automatic(self):
        get_or_create_month_plan(self.user, 2026, 9)
        get_or_create_month_plan(self.user, 2026, 10)

        interest = self.logs(area="debt", action="updated")[0]
        self.assertEqual(interest.source, "system")
        self.assertEqual(interest.reason, "Monthly interest (October 2026)")
        self.assertEqual((interest.changes[0]["before"], interest.changes[0]["after"]), ("3494.00", "3563.88"))

        started = self.logs(area="plan")
        self.assertEqual([(p.title, p.details["rolled_over"]) for p in started],
                         [("September 2026", False), ("October 2026", True)])

    def test_assistant_changes_are_attributed(self):
        with activity_context(source="assistant"):
            self.debt.current_balance = Decimal("100.00")
            self.debt.save()
        self.assertEqual(self.logs(area="debt", action="updated")[0].source, "assistant")
        # ...and the context doesn't leak into later changes.
        self.debt.current_balance = Decimal("90.00")
        self.debt.save()
        self.assertEqual(self.logs(area="debt", action="updated")[1].source, "app")

    def test_deleting_is_logged(self):
        self.debt.delete()
        log = self.logs(area="debt", action="deleted")[0]
        self.assertEqual(log.title, "CIBC Visa")

    def test_recurring_and_fund_changes(self):
        rent = RecurringItem.objects.create(user=self.user, label="Rent", amount=650)
        rent.amount = Decimal("700")
        rent.save()
        fund = EmergencyFund.objects.create(user=self.user, current_balance=0, target_amount=1000)
        fund.current_balance = Decimal("100")
        fund.save()
        self.assertEqual(self.logs(area="recurring", action="updated")[0].changes[0]["after"], "700.00")
        self.assertEqual(self.logs(area="fund", action="updated")[0].changes[0]["after"], "100.00")

    def test_deleting_the_account_still_works(self):
        Expense.objects.create(user=self.user, title="Coffee", amount=3, category="Food & Dining", date=date(2026, 10, 1))
        self.user.delete()
        self.assertFalse(ActivityLog.objects.exists())


class StatementImportActivityTests(TestCase):
    def test_import_logs_one_summary_not_one_line_per_row(self):
        user = make_user("imp@example.test")
        client = APIClient()
        client.force_authenticate(user)
        res = client.post("/api/statements/import/", {"rows": [
            {"kind": "expense", "title": "Coffee", "amount": "4.50", "date": "2026-10-03", "category": "Food & Dining", "source": "cibc.csv"},
            {"kind": "expense", "title": "Bus", "amount": "3.35", "date": "2026-10-04", "category": "Transport", "source": "cibc.csv"},
            {"kind": "income", "title": "Pay", "amount": "1150", "date": "2026-10-15", "category": "Salary", "source": "simplii.pdf"},
        ]}, format="json")
        self.assertEqual(res.status_code, 201)
        logs = list(ActivityLog.objects.filter(user=user))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].area, "import")
        self.assertEqual(logs[0].details["files"], ["cibc.csv", "simplii.pdf"])
        self.assertEqual((logs[0].details["expenses"], logs[0].details["expense_total"]), (2, "7.85"))


class ActivityApiTests(TestCase):
    def setUp(self):
        self.user = make_user("api@example.test")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        for i in range(5):
            Debt.objects.create(user=self.user, name=f"Card {i}", current_balance=100,
                                annual_interest_rate=20, minimum_payment=10)

    def test_lists_newest_first_with_paging_and_filters(self):
        res = self.client.get("/api/activity/?limit=2")
        self.assertEqual([r["title"] for r in res.data["results"]], ["Card 4", "Card 3"])
        self.assertTrue(res.data["has_more"])
        res = self.client.get(f"/api/activity/?limit=10&before={res.data['results'][-1]['id']}")
        self.assertEqual([r["title"] for r in res.data["results"]], ["Card 2", "Card 1", "Card 0"])
        self.assertFalse(res.data["has_more"])
        self.assertEqual(self.client.get("/api/activity/?area=income").data["results"], [])

    def test_only_your_own_activity(self):
        other = APIClient()
        other.force_authenticate(make_user("other@example.test"))
        self.assertEqual(other.get("/api/activity/").data["results"], [])
