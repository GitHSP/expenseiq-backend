from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from financial_planner.models import RecurringItem

from .income_policy import deposit_class, income_words
from .models import Account, Statement, StatementTransaction
from .rules import apply_to_preview


class DepositClassTests(TestCase):
    def test_pay_benefits_and_atm_count(self):
        for text in ("PAYROLL DEP ACME", "DIRECT DEPOSIT ACME", "ACME WIDGETS LTD.", "MOBILE DEPOSIT ACME CORP",
                     "ATM DEPOSIT BLOOR AND YONGE", "ABM DEPOSIT", "DEPOSIT CANADA", "DEPOSIT TPS/GST",
                     "DEPOSIT Alloc. travail CA", "SERVICE CANADA EI", "CANADA CARBON REBATE",
                     "ONTARIO TRILLIUM BENEFIT", "CRA TAX REFUND"):
            self.assertEqual(deposit_class(text), "income", text)

    def test_friends_own_transfers_and_apps_dont(self):
        for text in ("E-TRANSFER 0116 ALEX SMITH", "E-TRANSFER ACME INC", "INTERAC E-TRANSFER JO",
                     "E-TRANSFER LemFi", "INTERNET DEPOSIT"):
            self.assertIsNone(deposit_class(text), text)

    def test_refunds(self):
        self.assertEqual(deposit_class("VISA DEBIT PURCHASE REVERSAL"), "refund")

    def test_users_own_pay_sources(self):
        user = get_user_model().objects.create_user(email="ip@example.test", username="ip", password="x-Test-12345")
        RecurringItem.objects.create(user=user, label="Salary ACMEX (140 hrs)", amount=1, category="income")
        RecurringItem.objects.create(user=user, label="Tips", amount=1, category="income", average_from="gratuity co")
        words = income_words(user)
        self.assertEqual(words, ["acmex", "gratuity co"])
        self.assertEqual(deposit_class("E-TRANSFER 0116 ACMEX UNION", words), "income")
        self.assertIsNone(deposit_class("E-TRANSFER ACMEXX", words))      # whole words only


class IncomePolicyFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="if@example.test", username="if", password="x-Test-12345")

    def test_preview_unticks_non_income_deposits(self):
        rows = [
            {"file": 0, "raw_description": "PAYROLL DEP ACME", "title": "Acme", "signed_amount": "1000.00"},
            {"file": 0, "raw_description": "E-TRANSFER ALEX", "title": "Alex", "signed_amount": "50.00"},
            {"file": 0, "raw_description": "PURCHASE REVERSAL", "title": "Shop", "signed_amount": "9.00"},
            {"file": 0, "raw_description": "COFFEE", "title": "Coffee", "signed_amount": "-4.00"},
            {"file": 1, "raw_description": "PAYMENT FROM - 1234", "title": "Payment", "signed_amount": "200.00"},
            {"file": 1, "raw_description": "AMZNMKTPCA TORONTO ON", "title": "Amazon", "signed_amount": "12.42"},
        ]
        out = apply_to_preview(self.user, rows, file_kinds={0: "chequing", 1: "credit_card"})
        self.assertEqual([r.get("not_income") for r in out], [None, "other", "refund", None, None, "refund"])
        self.assertTrue(out[4]["is_transfer"])                 # a card payment is own money moving

    def test_summary_counts_only_saved_income_and_refunds_lower_spending(self):
        acc = Account.objects.create(user=self.user, name="Chequing", kind="chequing")
        st = Statement.objects.create(user=self.user, account=acc, filename="s.pdf",
                                      period_start=date(2026, 9, 1), period_end=date(2026, 9, 30))
        for desc, amt, kind in (("PAYROLL DEP ACME", "1000.00", "income"), ("E-TRANSFER ALEX", "50.00", "skipped"),
                                ("PURCHASE REVERSAL", "9.00", "skipped"), ("GROCERY", "-100.00", "expense")):
            StatementTransaction.objects.create(user=self.user, statement=st, account=acc, date=date(2026, 9, 3),
                                                description=desc, title=desc, amount=Decimal(amt), kind=kind)
        client = APIClient()
        client.force_authenticate(self.user)
        month = client.get("/api/statements/summary/").data["months"][0]
        self.assertEqual((month["income"], month["expenses"]), ("1000.00", "91.00"))


class ChangeTransactionKindTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(email="tk@example.test", username="tk", password="x-Test-12345")
        acc = Account.objects.create(user=self.user, name="Chequing", kind="chequing")
        st = Statement.objects.create(user=self.user, account=acc, filename="sep.pdf",
                                      period_start=date(2026, 9, 1), period_end=date(2026, 9, 30))
        self.tx = StatementTransaction.objects.create(user=self.user, statement=st, account=acc, date=date(2026, 9, 3),
                                                      description="E-TRANSFER GIG CO", title="E-Transfer Gig Co",
                                                      amount=Decimal("300.00"), kind="skipped")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def patch(self, **data):
        return self.client.patch(f"/api/statements/transactions/{self.tx.id}/", data, format="json")

    def test_income_then_back_to_not_saved(self):
        from .models import Income
        res = self.patch(kind="income")
        self.assertEqual(res.status_code, 200, res.data)
        inc = Income.objects.get(user=self.user)
        self.assertEqual((inc.title, inc.amount, inc.notes), ("E-Transfer Gig Co", Decimal("300.00"), "Imported from sep.pdf"))
        self.assertEqual(self.patch(kind="income").status_code, 200)           # no second entry
        self.assertEqual(Income.objects.filter(user=self.user).count(), 1)
        self.patch(kind="skipped")
        self.assertFalse(Income.objects.filter(user=self.user).exists())
        self.tx.refresh_from_db()
        self.assertEqual((self.tx.kind, self.tx.income_id), ("skipped", None))

    def test_expense_and_privacy(self):
        from .models import Expense
        self.assertEqual(self.patch(kind="expense").status_code, 200)
        self.assertEqual(Expense.objects.get(user=self.user).amount, Decimal("300.00"))
        self.assertEqual(self.patch(kind="duplicate").status_code, 400)
        other = APIClient()
        other.force_authenticate(get_user_model().objects.create_user(email="tk2@example.test", username="tk2",
                                                                      password="x-Test-12345"))
        self.assertEqual(other.patch(f"/api/statements/transactions/{self.tx.id}/", {"kind": "income"},
                                     format="json").status_code, 404)


class BillShareAndChargesTests(TestCase):
    """A shared bill is saved at the user's share; interest and fees aren't expenses."""

    def setUp(self):
        from .models import Expense
        User = get_user_model()
        self.user = User.objects.create_user(email="bs@example.test", username="bs", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        acc = Account.objects.create(user=self.user, name="Chequing", kind="chequing")
        st = Statement.objects.create(user=self.user, account=acc, filename="s.pdf",
                                      period_start=date(2026, 9, 1), period_end=date(2026, 9, 30))
        def row(desc, amt, kind="expense", expense=True):
            e = Expense.objects.create(user=self.user, title=desc.title(), amount=abs(Decimal(amt)), category="Other",
                                       date=date(2026, 9, 2)) if expense else None
            return StatementTransaction.objects.create(user=self.user, statement=st, account=acc, date=date(2026, 9, 2),
                                                       description=desc, title=desc.title(), amount=Decimal(amt),
                                                       kind=kind, expense=e)
        self.rent = row("PREAUTHORIZED DEBIT NORTHGATE HOMES", "-2400.00")
        self.power = row("HYDRO SPARK", "-100.00")
        row("OVERDRAFT INTEREST CHARGE", "-5.00")
        row("GROCERY", "-50.00")
        row("E-TRANSFER ALEX", "600.00", kind="skipped", expense=False)

    def month(self):
        return self.client.get("/api/statements/summary/").data["months"][0]

    def test_share_saves_my_part_and_roommates_just_pay_back(self):
        from .models import Expense
        for match, share in (("northgate", "600"), ("hydro spark", "25%")):
            res = self.client.post("/api/statements/rules/", {"match": match, "treat_as": "bill", "my_share": share},
                                   format="json")
            self.assertEqual(res.status_code, 201, res.data)
        self.client.post("/api/statements/rules/", {"match": "alex", "treat_as": "shared"}, format="json")
        self.rent.refresh_from_db(); self.power.refresh_from_db()
        self.assertEqual(Expense.objects.get(id=self.rent.expense_id).amount, Decimal("600.00"))
        self.assertEqual(Expense.objects.get(id=self.power.expense_id).amount, Decimal("25.00"))
        m = self.month()
        # 600 rent + 25 power + 50 groceries; interest left out; Alex's 600 not taken off again.
        self.assertEqual(m["expenses"], "675.00")
        self.assertEqual(m["my_shares"], {"Hydro Spark": "25.00", "Northgate": "600.00"})
        self.assertEqual(m["shared_bills"], {"Hydro Spark": "100.00", "Northgate": "2400.00"})
        self.assertTrue(m["bills_at_share"])
        rules = {r["match"]: r["my_share"] for r in self.client.get("/api/statements/rules/").data["rules"]}
        self.assertEqual((rules["northgate"], rules["hydro spark"]), ("600", "25%"))

    def test_bad_share_is_refused(self):
        for bad in ("abc", "0", "150%"):
            res = self.client.post("/api/statements/rules/", {"match": "northgate", "treat_as": "bill", "my_share": bad},
                                   format="json")
            self.assertEqual(res.status_code, 400, bad)

    def test_interest_and_fees_arent_spending(self):
        self.assertEqual(self.month()["expenses"], "2550.00")    # 2,400 + 100 + 50; the 5.00 interest left out
        out = apply_to_preview(self.user, [
            {"file": 0, "raw_description": "PURCHASE INTEREST 20.99%", "title": "Interest", "signed_amount": "-9.00"},
            {"file": 0, "raw_description": "ANNUAL FEE", "title": "Annual Fee", "signed_amount": "-120.00"},
            {"file": 0, "raw_description": "COFFEE SHOP", "title": "Coffee", "signed_amount": "-4.00"},
        ], file_kinds={0: "credit_card"})
        self.assertEqual([r.get("not_expense") for r in out], ["charge", "charge", None])
