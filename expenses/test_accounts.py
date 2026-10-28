from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from tools.statement_qa.corpus import build

from .accounts import find_recurring, guess_account, statement_period
from .models import Account, Expense, Statement, StatementTransaction
from .statement_import import parse_statement

CORPUS = {s.name: s for s in build()}


def parsed(name):
    s = CORPUS[name]
    return parse_statement(s.name, s.content)


class AccountDetectionTests(TestCase):
    def test_bank_named_in_the_letterhead_wins_over_banks_in_transactions(self):
        # The TD statement has "ONLINE BANKING PAYMENT CIBC VISA" in it.
        rows, info = parsed("td-chequing.pdf")
        self.assertEqual(guess_account("td-chequing.pdf", info)["name"], "TD Chequing")

    def test_credit_cards(self):
        cases = {"cibc-visa.pdf": "CIBC Visa •1234", "mbna.pdf": "MBNA Mastercard", "scotia-visa.pdf": "Scotiabank Visa"}
        for name, expected in cases.items():
            rows, info = parsed(name)
            guess = guess_account(name, info)
            self.assertEqual((guess["name"], guess["kind"]), (expected, "credit_card"), name)

    def test_simplii_is_not_mistaken_for_cibc(self):
        rows, info = parsed("simplii-chequing.pdf")
        self.assertEqual(guess_account("simplii-chequing.pdf", info)["institution"], "Simplii")

    def test_statement_without_account_number_matches_the_only_account_of_that_kind(self):
        from .accounts import match_existing_account
        user = get_user_model().objects.create_user(email="m@example.test", username="m", password="x-Test-12345")
        td = Account.objects.create(user=user, name="TD Chequing •4821", kind="chequing", institution="TD", last4="4821")
        guess = {"institution": "TD", "kind": "chequing", "last4": "", "name": "TD Chequing"}
        self.assertEqual(match_existing_account([td], guess), td)
        second = Account.objects.create(user=user, name="TD Savings-ish", kind="chequing", institution="TD", last4="9999")
        self.assertIsNone(match_existing_account([td, second], guess))     # ambiguous: let the user pick

    def test_bank_named_only_in_a_transaction_is_not_the_account(self):
        # Simplii's logo is an image; the first bank named in the text is a
        # Wealthsimple e-transfer, and "Simplii" only appears in the footer.
        header = ("your no fee chequing account statement period: July 30, 2026 - August 30, 2026 "
                  "account number: 0110001234 details trans. eff. transaction funds out funds in "
                  "Aug 3 E-TRANSFER 011700000001 500.00 Sample Wealthsimple Aug 9 PAYROLL 1,000.00 "
                  "please refer to the Simplii Financial Products and Services guide. "
                  "SIMPLII FINANCIAL DESIGNS are trademarks of CIBC.")
        rows = [{"description": "E-TRANSFER 011700000001 Sample Wealthsimple"}, {"description": "PAYROLL"}]
        guess = guess_account("eStatement_2026_08.pdf", {"header_text": header}, rows)
        self.assertEqual((guess["institution"], guess["kind"], guess["last4"]), ("Simplii", "chequing", "1234"))

    def test_card_number_in_a_transaction_is_not_the_account_number(self):
        header = ("CIBC Account Statement For Sep 1 to Sep 30, 2026 Account number 72-40042 "
                  "Sep 12 INTERNET TRANSFER 90.00 1,253.63 TO CARD 4505********0099 INTERNET BANKING")
        rows = [{"description": "INTERNET TRANSFER TO CARD 4505********0099"}]
        guess = guess_account("Account Statement cibc sep 26.pdf", {"header_text": header}, rows)
        self.assertEqual((guess["institution"], guess["kind"], guess["last4"]), ("CIBC", "chequing", "0042"))

    def test_csv_falls_back_to_the_file_name(self):
        guess = guess_account("Scotia_Momentum_Visa_2026-05.csv", {"header_text": ""})
        self.assertEqual((guess["institution"], guess["kind"]), ("Scotiabank", "credit_card"))


class StatementPeriodTests(TestCase):
    def test_printed_period_is_used(self):
        rows, info = parsed("cibc-visa.pdf")
        self.assertEqual(statement_period(info, rows), (date(2026, 9, 15), date(2026, 10, 14)))

    def test_short_td_period(self):
        rows, info = parsed("td-chequing.pdf")
        self.assertEqual(statement_period(info, rows), (date(2026, 8, 31), date(2026, 9, 30)))

    def test_payment_due_date_is_not_the_period_end(self):
        rows, info = parsed("cibc-visa.pdf")
        info = {**info, "header_text": ""}          # no printed period
        self.assertLessEqual(statement_period(info, rows)[1], date(2026, 10, 14))


def tx(user, account, statement, day, title, amount, kind="expense", description=None, category=""):
    return StatementTransaction.objects.create(
        user=user, account=account, statement=statement, date=day, title=title,
        description=description or title.upper(), amount=Decimal(str(amount)), kind=kind, category=category)


class RecurringTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="rec@example.test", username="rec", password="x-Test-12345")
        self.card = Account.objects.create(user=self.user, name="CIBC Visa", kind="credit_card")
        self.bank = Account.objects.create(user=self.user, name="TD Chequing", kind="chequing")
        self.st_card = Statement.objects.create(user=self.user, account=self.card, filename="c.pdf",
                                                period_start=date(2026, 1, 1), period_end=date(2026, 6, 30))
        self.st_bank = Statement.objects.create(user=self.user, account=self.bank, filename="b.pdf",
                                                period_start=date(2026, 1, 1), period_end=date(2026, 6, 30))

    def find(self):
        txs = StatementTransaction.objects.filter(user=self.user)
        ends = {self.card.id: date(2026, 6, 30), self.bank.id: date(2026, 6, 30)}
        return {i["name"]: i for i in find_recurring(txs, ends, lambda t: t.category or "Other")}

    def test_subscription_found_monthly(self):
        for m in range(1, 7):
            tx(self.user, self.card, self.st_card, date(2026, m, 24), "Netflix.Com", "-16.56", category="Subscriptions")
        item = self.find()["Netflix.Com"]
        self.assertEqual((item["frequency"], item["status"], item["monthly_amount"]), ("monthly", "active", "16.56"))
        self.assertEqual(item["next_date"], "2026-07-24")

    def test_groceries_are_not_a_recurring_charge(self):
        for m, amt in zip(range(1, 7), ["-63.10", "-120.44", "-41.00", "-88.12", "-63.10", "-99.99"]):
            tx(self.user, self.card, self.st_card, date(2026, m, 12), "Loblaws", amt, category="Food & Dining")
        self.assertNotIn("Loblaws", self.find())

    def test_pre_authorized_debit_found_even_once(self):
        tx(self.user, self.bank, self.st_bank, date(2026, 6, 1), "Realstar Corp", "-2550.00",
           description="PREAUTHORIZED DEBIT REALSTAR CORP", category="Housing")
        item = self.find()["Realstar Corp"]
        self.assertTrue(item["pre_authorized"])
        self.assertEqual(item["frequency"], "once")

    def test_varying_bill_still_recurs(self):
        for m, amt in zip(range(1, 5), ["-71.20", "-96.03", "-55.40", "-80.00"]):
            tx(self.user, self.bank, self.st_bank, date(2026, m, 15), "Toronto Hydro", amt, category="Bills & Utilities")
        item = self.find()["Toronto Hydro"]
        self.assertTrue(item["varies"])

    def test_cancelled_subscription_is_marked_stopped(self):
        for m in (1, 2, 3):
            tx(self.user, self.card, self.st_card, date(2026, m, 5), "Spotify", "-11.99", category="Subscriptions")
        item = self.find()["Spotify"]
        self.assertEqual(item["status"], "stopped")
        self.assertIsNone(item["monthly_amount"])

    def test_own_transfers_skipped_unless_automatic(self):
        for m in (1, 2, 3):
            tx(self.user, self.bank, self.st_bank, date(2026, m, 20), "Transfer To Simplii", "-500", kind="transfer")
            tx(self.user, self.bank, self.st_bank, date(2026, m, 8), "Cibc Visa", "-70.00", kind="transfer",
               description="PREAUTHORIZED PAYMENT CIBC VISA")
        found = self.find()
        self.assertNotIn("Transfer To Simplii", found)
        self.assertTrue(found["Cibc Visa"]["pre_authorized"])


class AccountsApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="acct@example.test", username="acct", password="x-Test-12345")
        self.other = get_user_model().objects.create_user(email="other@example.test", username="other", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def preview(self, *names):
        uploads = [SimpleUploadedFile(n, CORPUS[n].content) for n in names]
        return self.client.post("/api/statements/preview/", {"files": uploads}, format="multipart")

    def import_from_preview(self, data, account=None):
        """Import every row: spending/income where the preview would, the rest as transfers/skipped."""
        statements = []
        for i, f in enumerate(data["files"]):
            txs = []
            for r in data["rows"]:
                if r["file"] != i:
                    continue
                out = (Decimal(r["signed_amount"]) < 0) != f["suggest_flip"]
                skip = r["is_transfer"] or r["transfer_match"]
                txs.append({
                    "date": r["date"], "description": r["raw_description"], "title": r["title"],
                    "amount": str(-Decimal(r["amount"]) if out else Decimal(r["amount"])),
                    "kind": "transfer" if skip else "skipped",
                    **({} if skip else {"import": {
                        "kind": "expense" if out else "income", "title": r["title"],
                        "category": r["expense_category"] if out else r["income_category"]}}),
                })
            statements.append({
                "filename": f["filename"], "file_hash": f["file_hash"],
                "period_start": f["period_start"], "period_end": f["period_end"],
                "account": account or ({"id": f["account_id"]} if f["account_id"] else f["account_guess"]),
                "transactions": txs,
            })
        return self.client.post("/api/statements/import/", {"statements": statements}, format="json")

    def test_preview_suggests_account_and_period(self):
        f = self.preview("cibc-visa.pdf").data["files"][0]
        self.assertEqual(f["account_guess"]["name"], "CIBC Visa •1234")
        self.assertIsNone(f["account_id"])
        self.assertEqual((f["period_start"], f["period_end"]), ("2026-09-15", "2026-10-14"))
        self.assertIsNone(f["already_uploaded"])

    def test_import_keeps_every_row_including_transfers(self):
        data = self.preview("simplii-chequing.pdf", "cibc-visa.pdf").data
        res = self.import_from_preview(data)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["statements_saved"], 2)
        self.assertEqual(StatementTransaction.objects.filter(user=self.user).count(), len(data["rows"]))
        self.assertTrue(StatementTransaction.objects.filter(user=self.user, kind="transfer").exists())
        linked = StatementTransaction.objects.filter(user=self.user, kind="expense")
        self.assertTrue(linked.exists())
        self.assertTrue(all(t.expense_id for t in linked))
        self.assertEqual(Account.objects.filter(user=self.user).count(), 2)

    def test_second_upload_matches_the_account_and_is_flagged(self):
        self.import_from_preview(self.preview("cibc-visa.pdf").data)
        f = self.preview("cibc-visa.pdf").data["files"][0]
        account = Account.objects.get(user=self.user)
        self.assertEqual(f["account_id"], account.id)
        self.assertEqual(f["already_uploaded"]["account"], account.name)

    def test_same_file_twice_is_stored_once(self):
        data = self.preview("cibc-visa.pdf").data
        self.import_from_preview(data)
        rows = StatementTransaction.objects.count()
        res = self.import_from_preview(data)
        self.assertEqual(res.data["statements_saved"], 0)
        self.assertEqual(StatementTransaction.objects.count(), rows)
        self.assertEqual(Statement.objects.count(), 1)

    def test_reuploading_a_file_updates_its_period_without_duplicating_rows(self):
        data = self.preview("cibc-visa.pdf").data
        self.import_from_preview(data)
        st = Statement.objects.get()
        st.period_start = st.period_end = date(2026, 10, 1)          # as if it had been misread
        st.save()
        rows = StatementTransaction.objects.count()
        self.import_from_preview(self.preview("cibc-visa.pdf").data)
        st.refresh_from_db()
        self.assertEqual((st.period_start, st.period_end), (date(2026, 9, 15), date(2026, 10, 14)))
        self.assertEqual(StatementTransaction.objects.count(), rows)
        self.assertEqual(Statement.objects.count(), 1)

    def test_reupload_replaces_rows_that_were_misread(self):
        # As happened with RBC: the first upload read a payment-slip line as
        # the only "transaction" and it was saved as income.
        from .models import Income
        data = self.preview("cibc-visa.pdf").data
        self.import_from_preview(data)
        st = Statement.objects.get()
        good = StatementTransaction.objects.count()
        fake_income = Income.objects.create(user=self.user, title="Credit Card Payment Centre", amount=45,
                                            category="Other", date=date(2026, 11, 8), notes="Imported from cibc-visa.pdf")
        StatementTransaction.objects.create(user=self.user, statement=st, account=st.account, date=date(2026, 11, 8),
                                            description="CREDIT CARD PAYMENT CENTRE", title="Credit Card Payment Centre",
                                            amount=45, kind="income", income=fake_income)
        StatementTransaction.objects.filter(statement=st).order_by("id").first().delete()     # and one real row missing
        res = self.import_from_preview(self.preview("cibc-visa.pdf").data)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["resync"]["rows_removed"], 1)
        self.assertEqual(res.data["resync"]["rows_added"], 1)
        self.assertEqual(res.data["resync"]["entries_removed"], 1)
        self.assertFalse(Income.objects.filter(id=fake_income.id).exists())
        self.assertEqual(StatementTransaction.objects.count(), good)
        self.assertFalse(StatementTransaction.objects.filter(description="CREDIT CARD PAYMENT CENTRE").exists())

    def test_cannot_import_into_someone_elses_account(self):
        theirs = Account.objects.create(user=self.other, name="Their Visa", kind="credit_card")
        res = self.import_from_preview(self.preview("cibc-visa.pdf").data, account={"id": theirs.id})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(Statement.objects.exists())
        self.assertFalse(Expense.objects.exists())

    def test_accounts_list_with_month_coverage(self):
        self.import_from_preview(self.preview("cibc-visa.pdf").data)
        data = self.client.get("/api/statements/accounts/").data
        account = data["accounts"][0]
        self.assertEqual(account["coverage"], {"2026-09": 0.53, "2026-10": 0.45})
        self.assertIn("2026-09", data["months"])
        self.assertEqual(len(account["statements"]), 1)
        self.assertEqual(account["statements"][0]["count"], account["transaction_count"])

    def test_transactions_for_one_card_and_month(self):
        self.import_from_preview(self.preview("simplii-chequing.pdf", "cibc-visa.pdf").data)
        card = Account.objects.get(user=self.user, kind="credit_card")
        data = self.client.get(f"/api/statements/transactions/?account={card.id}&month=2026-09").data
        self.assertTrue(data["count"] > 0)
        self.assertTrue(all(t["account_id"] == card.id and t["date"].startswith("2026-09") for t in data["transactions"]))
        # Nobody else sees them.
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(f"/api/statements/transactions/?account={card.id}&month=2026-09").data["count"], 0)
        self.assertEqual(self.client.get("/api/statements/accounts/").data["accounts"], [])

    def test_recurring_endpoint(self):
        self.import_from_preview(self.preview("td-chequing.pdf").data)
        data = self.client.get("/api/statements/recurring/").data
        names = {i["name"] for i in data["items"]}
        self.assertIn("Realstar Corp", names)          # PREAUTHORIZED DEBIT REALSTAR CORP
        self.assertTrue(all(i["pre_authorized"] for i in data["items"] if i["name"] == "Realstar Corp"))

    def test_delete_statement_with_its_entries(self):
        self.import_from_preview(self.preview("cibc-visa.pdf").data)
        statement = Statement.objects.get()
        self.assertTrue(Expense.objects.exists())
        res = self.client.delete(f"/api/statements/{statement.id}/?delete_entries=1")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(Expense.objects.exists())
        self.assertFalse(StatementTransaction.objects.exists())

    def test_delete_account_keeps_saved_expenses(self):
        self.import_from_preview(self.preview("cibc-visa.pdf").data)
        count = Expense.objects.count()
        account = Account.objects.get()
        self.assertEqual(self.client.delete(f"/api/statements/accounts/{account.id}/").status_code, 204)
        self.assertEqual(Expense.objects.count(), count)
        self.assertFalse(Statement.objects.exists())

    def test_cannot_touch_another_users_statement(self):
        theirs = Account.objects.create(user=self.other, name="Their Visa", kind="credit_card")
        st = Statement.objects.create(user=self.other, account=theirs, filename="x.pdf",
                                      period_start=date(2026, 1, 1), period_end=date(2026, 1, 31))
        self.assertEqual(self.client.delete(f"/api/statements/{st.id}/").status_code, 404)
        self.assertEqual(self.client.patch(f"/api/statements/accounts/{theirs.id}/", {"name": "Mine"}, format="json").status_code, 404)
        self.assertTrue(Statement.objects.filter(id=st.id).exists())


class MergeAndMoveTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(email="mm@example.test", username="mm", password="x-Test-12345")
        self.other = User.objects.create_user(email="mm2@example.test", username="mm2", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.simplii = Account.objects.create(user=self.user, name="Simplii Chequing •0937", kind="chequing")
        self.wrong = Account.objects.create(user=self.user, name="Wealthsimple Chequing •0937", kind="chequing")
        self.st = Statement.objects.create(user=self.user, account=self.wrong, filename="s.pdf",
                                           period_start=date(2026, 5, 1), period_end=date(2026, 5, 31))
        tx(self.user, self.wrong, self.st, date(2026, 5, 3), "Rent", "-1000")

    def test_merge_moves_everything_and_removes_the_account(self):
        res = self.client.post(f"/api/statements/accounts/{self.wrong.id}/merge/", {"into": self.simplii.id}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(Account.objects.filter(id=self.wrong.id).exists())
        self.st.refresh_from_db()
        self.assertEqual(self.st.account_id, self.simplii.id)
        self.assertEqual(StatementTransaction.objects.get().account_id, self.simplii.id)

    def test_move_one_statement(self):
        res = self.client.patch(f"/api/statements/{self.st.id}/", {"account": self.simplii.id}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(StatementTransaction.objects.get().account_id, self.simplii.id)
        self.assertTrue(Account.objects.filter(id=self.wrong.id).exists())

    def test_cannot_merge_into_or_from_someone_elses_account(self):
        theirs = Account.objects.create(user=self.other, name="Theirs", kind="chequing")
        self.assertEqual(self.client.post(f"/api/statements/accounts/{self.wrong.id}/merge/", {"into": theirs.id}, format="json").status_code, 404)
        self.assertEqual(self.client.post(f"/api/statements/accounts/{theirs.id}/merge/", {"into": self.simplii.id}, format="json").status_code, 404)
        self.assertEqual(self.client.patch(f"/api/statements/{self.st.id}/", {"account": theirs.id}, format="json").status_code, 404)
        self.assertEqual(Statement.objects.get().account_id, self.wrong.id)


class CardDebtTests(TestCase):
    """Card statements keep the matching Planner debt up to date."""

    def setUp(self):
        from financial_planner.models import Debt
        self.user = get_user_model().objects.create_user(email="cd@example.test", username="cd", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.debt = Debt.objects.create(user=self.user, name="CIBC Visa", debt_type="credit_card",
                                        current_balance=1000, annual_interest_rate="21.99", minimum_payment=10)
        self.other = Debt.objects.create(user=self.user, name="RBC Master #2", debt_type="credit_card",
                                         current_balance=500, annual_interest_rate="20.99", minimum_payment=10)

    def import_card(self, previous, new, end, rows, minimum="70.00", due="2026-11-08", filename=None):
        txs = [{"date": d, "description": desc, "title": desc, "amount": a, "kind": k} for d, desc, a, k in rows]
        return self.client.post("/api/statements/import/", {"statements": [{
            "filename": filename or f"cibc-{end}.pdf", "file_hash": f"h-{end}", "period_start": rows[0][0], "period_end": end,
            "account": {"name": "CIBC Visa •1234", "kind": "credit_card", "institution": "CIBC", "last4": "1234"},
            "summary": {"previous_balance": previous, "new_balance": new, "minimum_payment": minimum,
                        "payment_due": due, "credit_limit": "3500.00"},
            "transactions": txs,
        }]}, format="json")

    def test_statement_updates_the_matching_debt(self):
        res = self.import_card("3494.00", "3170.48", "2026-10-14",
                               [("2026-10-02", "PAYMENT THANK YOU", "500.00", "transfer"),
                                ("2026-09-20", "AMAZON.CA", "-176.48", "skipped")])
        self.assertEqual(res.status_code, 201, res.data)
        self.debt.refresh_from_db()
        self.assertEqual(self.debt.current_balance, Decimal("3170.48"))
        self.assertEqual((self.debt.minimum_payment, self.debt.credit_limit, self.debt.due_day), (Decimal("70.00"), Decimal("3500.00"), 8))
        self.assertEqual(res.data["debts_updated"][0]["debt"], "CIBC Visa")
        self.other.refresh_from_db()
        self.assertEqual(self.other.current_balance, Decimal("500"))       # the RBC debt isn't touched
        from activity.models import ActivityLog
        log = ActivityLog.objects.filter(area="debt").latest("id")
        self.assertIn("statement ending Oct 14, 2026", log.reason)

    def test_an_older_statement_doesnt_overwrite_a_newer_balance(self):
        self.import_card("3494.00", "3170.48", "2026-10-14", [("2026-10-02", "PAYMENT", "500.00", "transfer")])
        self.import_card("3200.00", "3494.00", "2026-09-14", [("2026-09-02", "PAYMENT", "100.00", "transfer")])
        self.debt.refresh_from_db()
        self.assertEqual(self.debt.current_balance, Decimal("3170.48"))

    def test_card_history_and_payments_since(self):
        self.import_card("3494.00", "3170.48", "2026-10-14",
                         [("2026-10-02", "PAYMENT THANK YOU", "500.00", "transfer"),
                          ("2026-09-20", "AMAZON.CA", "-112.45", "skipped"),
                          ("2026-10-14", "PURCHASE INTEREST", "-64.03", "skipped")])
        # A later chequing statement shows a payment to the card.
        self.client.post("/api/statements/import/", {"statements": [{
            "filename": "chq.pdf", "file_hash": "chq", "period_start": "2026-10-15", "period_end": "2026-10-31",
            "account": {"name": "CIBC Chequing", "kind": "chequing", "institution": "CIBC"},
            "transactions": [{"date": "2026-10-20", "description": "INTERNET TRANSFER TO CARD 4500********1234",
                              "title": "Card payment", "amount": "-200.00", "kind": "transfer"},
                             {"date": "2026-10-21", "description": "LOBLAWS", "title": "Loblaws", "amount": "-50.00", "kind": "skipped"}],
        }]}, format="json")
        card = next(a for a in self.client.get("/api/statements/accounts/").data["accounts"] if a["kind"] == "credit_card")
        h = card["card"]["history"][0]
        self.assertEqual((h["paid"], h["spent"], h["interest"]), ("500.00", "112.45", "64.03"))
        self.assertEqual(card["card"]["paid_since"], "200.00")
        self.assertEqual(card["card"]["estimated_balance"], "2970.48")
        self.assertEqual(card["debt"]["name"], "CIBC Visa")

    def test_relinking_to_another_debt_syncs_it(self):
        self.import_card("3494.00", "3170.48", "2026-10-14", [("2026-10-02", "PAYMENT", "500.00", "transfer")])
        account = Account.objects.get(user=self.user)
        res = self.client.patch(f"/api/statements/accounts/{account.id}/", {"debt": self.other.id}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.other.refresh_from_db()
        self.assertEqual(self.other.current_balance, Decimal("3170.48"))

    def test_planner_interest_skips_cards_kept_up_to_date_by_statements(self):
        from datetime import date as d
        from financial_planner.services import _apply_monthly_interest
        self.import_card("3494.00", "3170.48", d.today().isoformat(), [(d.today().isoformat(), "PAYMENT", "500.00", "transfer")])
        _apply_monthly_interest(self.user, 1)
        self.debt.refresh_from_db(); self.other.refresh_from_db()
        self.assertEqual(self.debt.current_balance, Decimal("3170.48"))       # unchanged
        self.assertGreater(self.other.current_balance, Decimal("500"))         # estimated interest as before

    def test_monthly_summary_leaves_out_transfers(self):
        self.import_card("3494.00", "3170.48", "2026-10-14",
                         [("2026-10-02", "PAYMENT THANK YOU", "500.00", "transfer"),
                          ("2026-10-03", "AMAZON.CA", "-100.00", "skipped"),
                          ("2026-10-05", "AMAZON.CA REFUND", "20.00", "skipped")])
        self.client.post("/api/statements/import/", {"statements": [{
            "filename": "chq.pdf", "file_hash": "chq2", "period_start": "2026-10-01", "period_end": "2026-10-31",
            "account": {"name": "CIBC Chequing", "kind": "chequing", "institution": "CIBC"},
            "transactions": [{"date": "2026-10-04", "description": "PAYROLL", "amount": "2000.00", "kind": "skipped"},
                             {"date": "2026-10-06", "description": "TO CARD 1234", "amount": "-500.00", "kind": "transfer"}],
        }]}, format="json")
        data = self.client.get("/api/statements/summary/").data
        self.assertEqual(data["months"], [{"month": "2026-10", "income": "2000.00", "expenses": "80.00", "net": "1920.00", "shared_by_others": "0.00"}])


class RuleApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(email="ru@example.test", username="ru", password="x-Test-12345")
        self.other = User.objects.create_user(email="ru2@example.test", username="ru2", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_add_list_delete_and_privacy(self):
        res = self.client.post("/api/statements/rules/", {"match": "alex", "treat_as": "shared"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        rules = self.client.get("/api/statements/rules/").data["rules"]
        self.assertEqual([(r["match"], r["treat_as"]) for r in rules], [("alex", "shared")])
        self.assertEqual(self.client.post("/api/statements/rules/", {"match": "ab"}, format="json").status_code, 400)
        other = APIClient()
        other.force_authenticate(self.other)
        self.assertEqual(other.get("/api/statements/rules/").data["rules"], [])
        self.assertEqual(other.delete(f"/api/statements/rules/{rules[0]['id']}/").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/statements/rules/{rules[0]['id']}/").status_code, 204)
