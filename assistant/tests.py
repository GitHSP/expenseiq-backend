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


class SearchTransactionsToolTests(TestCase):
    """Aurora can answer 'how much on coffee, and on which card?' from statements and manual entries."""

    def setUp(self):
        from expenses.models import Account, Statement, StatementTransaction
        self.user = get_user_model().objects.create_user(email="st@example.test", username="st", password="x-Test-12345")
        other = get_user_model().objects.create_user(email="st2@example.test", username="st2", password="x-Test-12345")
        visa = Account.objects.create(user=self.user, name="CIBC Visa •0042", kind="credit_card", institution="CIBC")
        chq = Account.objects.create(user=self.user, name="CIBC Chequing •0001", kind="chequing", institution="CIBC")
        def st(acct, u=None):
            return Statement.objects.create(user=u or self.user, account=acct, filename="s.pdf",
                                            period_start=date(2026, 9, 1), period_end=date(2026, 9, 30))
        sv, sc = st(visa), st(chq)
        def tx(statement, d, title, amount, kind="skipped", desc=None, u=None):
            StatementTransaction.objects.create(user=u or self.user, statement=statement, account=statement.account,
                                                date=date(2026, 9, d), title=title, description=desc or title.upper(),
                                                amount=Decimal(amount), kind=kind)
        tx(sv, 3, "Tim Hortons Toronto On", "-4.50")
        tx(sv, 9, "Starbucks", "-6.25")
        tx(sc, 4, "TIM HORTONS #24", "-2.10")
        tx(sv, 5, "Loblaws", "-63.10")
        tx(sv, 20, "Payment Thank You", "500.00", kind="transfer")
        tx(sc, 20, "Internet Transfer To Card", "-500.00", kind="transfer")
        Expense.objects.create(user=self.user, title="Coffee at Balzac's", amount=Decimal("5.00"), category="Food & Dining",
                               date=date(2026, 9, 12))
        theirs = Account.objects.create(user=other, name="Their Visa", kind="credit_card")
        tx(st(theirs, other), 3, "Tim Hortons", "-99.00", u=other)

    def test_coffee_by_card(self):
        from .insights import search_transactions
        r = search_transactions(self.user, theme="coffee")
        self.assertEqual((r["count"], r["total_spent"]), (4, "17.85"))
        by = {a["account"]: a["total"] for a in r["by_account"]}
        self.assertEqual(by, {"CIBC Visa •0042": "10.75", "CIBC Chequing •0001": "2.10",
                              "Entered in Glacier (no statement)": "5.00"})
        # "Tim Hortons Toronto On" and "TIM HORTONS #24" are one merchant.
        tims = [m for m in r["by_merchant"] if "tim hortons" in m["merchant"].lower()]
        self.assertEqual([(len(tims), tims[0]["count"], tims[0]["total"])], [(1, 2, "6.60")])

    def test_transfers_and_other_users_are_left_out(self):
        from .insights import search_transactions
        r = search_transactions(self.user, account="CIBC Visa")
        self.assertEqual(r["total_spent"], "73.85")          # no card payment, no other user's rows
        r = search_transactions(self.user, query="transfer", include_transfers=True)
        self.assertEqual(r["count"], 1)                      # spending side only: the chequing transfer out

    def test_registered_as_read_only_tools(self):
        from .tools import MUTATING_TOOLS, TOOL_HANDLERS, TOOL_SCHEMAS
        names = {t["name"] for t in TOOL_SCHEMAS}
        for tool in ("search_transactions", "get_accounts_overview"):
            self.assertIn(tool, names)
            self.assertIn(tool, TOOL_HANDLERS)
            self.assertNotIn(tool, MUTATING_TOOLS)
        self.assertTrue(TOOL_HANDLERS["get_accounts_overview"](self.user)["ok"])


class SearchThroughChatTests(SearchTransactionsToolTests):
    """The chat endpoint runs search_transactions when the model asks for it."""

    def test_chat_runs_the_search_tool(self):
        from types import SimpleNamespace as NS
        from unittest import mock
        call = NS(type="tool_use", id="t1", name="search_transactions", input={"theme": "coffee"})
        answer = NS(stop_reason="end_turn", content=[NS(type="text", text="You spent **$17.85** on coffee.")])
        client = APIClient()
        client.force_authenticate(self.user)
        with mock.patch("assistant.views.anthropic.Anthropic") as A, \
             mock.patch("assistant.views.settings.ANTHROPIC_API_KEY", "test-key"):
            A.return_value.messages.create.side_effect = [NS(stop_reason="tool_use", content=[call]), answer]
            res = client.post("/api/assistant/chat/", {"messages": [
                {"role": "user", "content": "How much did I spend on coffee, and on which card?"}]}, format="json")
            history = A.return_value.messages.create.call_args_list[1].kwargs["messages"]
            sent = next(block["content"] for m in history if m["role"] == "user" and isinstance(m["content"], list)
                        for block in m["content"] if isinstance(block, dict) and block.get("type") == "tool_result")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn('"total_spent": "17.85"', sent)            # the tool result Claude received
        self.assertIn("CIBC Visa", sent)
        self.assertEqual(res.data["reply"], "You spent **$17.85** on coffee.")
        self.assertEqual(res.data["actions"], [])


class GroundingCheckTests(SearchTransactionsToolTests):
    """Amounts Aurora states must come from this reply's tool results."""

    def chat(self, replies):
        from types import SimpleNamespace as NS
        from unittest import mock
        call = NS(type="tool_use", id="t1", name="search_transactions", input={"theme": "coffee"})
        script = [NS(stop_reason="tool_use", content=[call])] + [
            NS(stop_reason="end_turn", content=[NS(type="text", text=t)]) for t in replies]
        client = APIClient()
        client.force_authenticate(self.user)
        with mock.patch("assistant.views.anthropic.Anthropic") as A, \
             mock.patch("assistant.views.settings.ANTHROPIC_API_KEY", "k"):
            A.return_value.messages.create.side_effect = script
            res = client.post("/api/assistant/chat/", {"messages": [{"role": "user", "content": "coffee?"}]}, format="json")
            calls = A.return_value.messages.create.call_count
        return res.data["reply"], calls

    def test_invented_amount_is_sent_back_and_fixed(self):
        reply, calls = self.chat(["You spent $27.38 on coffee.", "You spent **$17.85** on coffee ($10.75 on CIBC Visa)."])
        self.assertEqual(calls, 3)
        self.assertEqual(reply, "You spent **$17.85** on coffee ($10.75 on CIBC Visa).")

    def test_amount_still_unverified_is_flagged(self):
        reply, calls = self.chat(["You spent $27.38 on coffee.", "You spent $27.38 on coffee."])
        self.assertIn("couldn't verify $27.38", reply)

    def test_sums_and_user_amounts_pass(self):
        reply, calls = self.chat(["Coffee: $17.85 in total, $10.75 + $2.10 + $5.00 = $17.85."])
        self.assertEqual(calls, 2)
        self.assertNotIn("verify", reply)
