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

    SHOWN = "Between 2026-09-01 and 2026-09-30 there are 4 entries totalling $3,731.92. Shall I delete them?"

    def call(self, user_message, shown=SHOWN, **kwargs):
        ctx = {"last_user_message": user_message, "previous_assistant_message": shown, "previewed": set()}
        return delete_transactions(self.user, "2026-09-01", "2026-09-30", _ctx=ctx, **kwargs), ctx

    def test_preview_deletes_nothing(self):
        result, _ = self.call("delete all my September transactions")
        self.assertTrue(result["needs_confirmation"])
        self.assertEqual(result["count"], 4)
        self.assertIn("3 expense entries totalling $2581.92", result["message"])
        self.assertEqual(Expense.objects.count(), 4)

    def test_cannot_delete_a_count_the_user_was_never_shown(self):
        # e.g. the user said "yes" to an unrelated question
        result, _ = self.call("yes", shown="Would you like a summary of your spending?", confirm=True, expected_count=4)
        self.assertFalse(result["ok"])
        self.assertEqual(Expense.objects.count(), 4)

    def test_rechecking_before_deleting_after_the_user_said_yes_works(self):
        # What happened in production: the user confirmed, the assistant
        # previewed again, then deleted — all in one reply.
        ctx = {"last_user_message": "yes", "previous_assistant_message": self.SHOWN, "previewed": set()}
        delete_transactions(self.user, "2026-09-01", "2026-09-30", _ctx=ctx)
        result = delete_transactions(self.user, "2026-09-01", "2026-09-30", confirm=True, expected_count=4, _ctx=ctx)
        self.assertTrue(result["ok"])
        self.assertEqual(list(Expense.objects.values_list("title", flat=True)), ["October"])

    def test_large_counts_shown_with_commas_are_recognised(self):
        ctx = {"last_user_message": "yes, go ahead", "previous_assistant_message": "You have **1,332 expense entries**.", "previewed": set()}
        result = delete_transactions(self.user, "2026-09-01", "2026-09-30", confirm=True, expected_count=1332, _ctx=ctx)
        self.assertIn("there are now 4", result["message"])  # passed the shown-count check, stopped at the live count

    def test_needs_the_user_to_say_yes(self):
        result, _ = self.call("delete all my September expenses", confirm=True, expected_count=4)
        self.assertFalse(result["ok"])
        self.assertEqual(Expense.objects.count(), 4)

    def test_count_must_still_match(self):
        result, _ = self.call("yes", shown="There are 3 entries. Delete them?", confirm=True, expected_count=3)
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


class FalseClaimReplyTests(TestCase):
    """If the model claims a change that a tool refused, the user sees what happened instead."""

    def test_claim_is_replaced_with_the_refusal(self):
        from unittest import mock
        from types import SimpleNamespace as NS
        user = get_user_model().objects.create_user(email="fc@example.test", username="fc", password="x-Test-12345")
        Expense.objects.create(user=user, title="E", amount=5, category="Other", date=date(2026, 9, 1))
        tool_call = NS(type="tool_use", id="t1", name="delete_transactions",
                       input={"date_from": "2026-09-01", "date_to": "2026-09-30", "confirm": True, "expected_count": 1})
        responses = [NS(stop_reason="tool_use", content=[tool_call]),
                     NS(stop_reason="end_turn", content=[NS(type="text", text="Done! I've deleted all 1 expense.")])]
        client = APIClient()
        client.force_authenticate(user)
        with mock.patch("assistant.views.anthropic.Anthropic") as A,              mock.patch("assistant.views.settings.ANTHROPIC_API_KEY", "test-key"):
            A.return_value.messages.create.side_effect = responses
            res = client.post("/api/assistant/chat/", {"messages": [
                {"role": "user", "content": "delete my september expenses"},
                {"role": "assistant", "content": "Would you like a summary?"},
                {"role": "user", "content": "yes"},
            ]}, format="json")
        self.assertTrue(res.data["reply"].startswith("Nothing was changed."))
        self.assertNotIn("Done", res.data["reply"])
        self.assertEqual(res.data["actions"], [])
        self.assertTrue(Expense.objects.exists())
