from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from activity.models import ActivityLog
from expenses.models import Expense, Income

from .tools import delete_transactions
from .views import CLAIMS_CHANGE


class DeleteTransactionsToolTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="a@example.test", username="a", password="x-Test-12345")
        for d, amt in ((1, "30.00"), (3, "1.92"), (15, "2550.00")):
            Expense.objects.create(user=self.user, title=f"E{d}", amount=amt, category="Other", date=date(2026, 9, d))
        Expense.objects.create(user=self.user, title="October", amount=5, category="Other", date=date(2026, 10, 1))
        Income.objects.create(user=self.user, title="Pay", amount=1150, category="Salary", date=date(2026, 9, 4))

    def call(self, user_message, **kwargs):
        ctx = {"last_user_message": user_message, "previewed": set()}
        return delete_transactions(self.user, "2026-09-01", "2026-09-30", _ctx=ctx, **kwargs), ctx

    def test_preview_deletes_nothing(self):
        result, _ = self.call("delete all my September transactions")
        self.assertTrue(result["needs_confirmation"])
        self.assertEqual(result["count"], 4)
        self.assertIn("3 expense entries totalling $2581.92", result["message"])
        self.assertEqual(Expense.objects.count(), 4)

    def test_cannot_confirm_in_the_same_reply_as_the_preview(self):
        ctx = {"last_user_message": "yes", "previewed": set()}
        delete_transactions(self.user, "2026-09-01", "2026-09-30", _ctx=ctx)
        result = delete_transactions(self.user, "2026-09-01", "2026-09-30", confirm=True, expected_count=4, _ctx=ctx)
        self.assertFalse(result["ok"])
        self.assertEqual(Expense.objects.count(), 4)

    def test_needs_the_user_to_say_yes(self):
        result, _ = self.call("delete all my September expenses", confirm=True, expected_count=4)
        self.assertFalse(result["ok"])
        self.assertEqual(Expense.objects.count(), 4)

    def test_count_must_still_match(self):
        result, _ = self.call("yes", confirm=True, expected_count=3)
        self.assertFalse(result["ok"])
        self.assertIn("now 4", result["message"])

    def test_confirmed_delete_only_touches_the_range(self):
        result, _ = self.call("Yes, go ahead", confirm=True, expected_count=4)
        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted"], 4)
        self.assertEqual(list(Expense.objects.values_list("title", flat=True)), ["October"])
        self.assertFalse(Income.objects.exists())
        # One activity summary per kind, not one line per entry.
        self.assertEqual(
            sorted(ActivityLog.objects.filter(action="deleted").values_list("area", "title")),
            [("expense", "3 expenses (2026-09-01 to 2026-09-30)"), ("income", "1 income entry (2026-09-01 to 2026-09-30)")],
        )

    def test_other_users_untouched(self):
        other = get_user_model().objects.create_user(email="b@example.test", username="b", password="x-Test-12345")
        Expense.objects.create(user=other, title="Theirs", amount=9, category="Other", date=date(2026, 9, 2))
        self.call("yes", confirm=True, expected_count=4)
        self.assertTrue(Expense.objects.filter(user=other).exists())


class HonestyBackstopTests(TestCase):
    def test_flags_claims_of_changes(self):
        for text in ["Done! I've deleted all September expenses.",
                     "Your expense history for September has been cleared.",
                     "I added your lunch.", "I've marked rent as paid."]:
            self.assertIsNotNone(CLAIMS_CHANGE.search(text), text)

    def test_leaves_normal_answers_alone(self):
        for text in ["You have $4,800 left on Scotia Visa.",
                     "I can't delete a whole month, but the Expenses page has a Delete all button.",
                     "This would delete 23 expenses totalling $2,931.92. Shall I go ahead?"]:
            self.assertIsNone(CLAIMS_CHANGE.search(text), text)


class BulkDeleteApiTests(TestCase):
    def test_deletes_only_your_own_ids(self):
        me = get_user_model().objects.create_user(email="m@example.test", username="m", password="x-Test-12345")
        other = get_user_model().objects.create_user(email="o@example.test", username="o", password="x-Test-12345")
        mine = [Expense.objects.create(user=me, title=f"M{i}", amount=10, category="Other", date=date(2026, 9, 1)).id for i in range(3)]
        theirs = Expense.objects.create(user=other, title="T", amount=10, category="Other", date=date(2026, 9, 1)).id
        client = APIClient()
        client.force_authenticate(me)
        res = client.post("/api/expenses/bulk-delete/", {"ids": mine + [theirs], "label": "September 2026"}, format="json")
        self.assertEqual(res.data, {"deleted": 3, "total": "30.00"})
        self.assertTrue(Expense.objects.filter(id=theirs).exists())
        res = client.post("/api/income/bulk-delete/", {"ids": "nope"}, format="json")
        self.assertEqual(res.status_code, 400)
