from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Expense, Income
from .statement_import import StatementError, clean_title, parse_statement, suggest_flip

# Sample exports in each bank's layout (made-up transactions).
RBC_CSV = """"Account Type","Account Number","Transaction Date","Cheque Number","Description 1","Description 2","CAD$","USD$"
Chequing,01234-5678901,10/3/2026,,"TIM HORTONS #1234","TORONTO ON",-4.50,
Chequing,01234-5678901,10/15/2026,,"PAYROLL DEPOSIT","WVRST",1150.00,
Chequing,01234-5678901,10/16/2026,,"ONLINE TRANSFER","TO SAVINGS",-200.00,
"""

CIBC_CSV = """2026-10-03,TIM HORTONS #1234 TORONTO ON,4.50,,4500********1234
2026-10-04,PAYMENT THANK YOU/PAIEMENT MERCI,,200.00,4500********1234
2026-10-05,NETFLIX.COM,16.49,,4500********1234
"""

TD_CSV = """10/03/2026,TIM HORTONS,4.50,,1234.56
10/04/2026,E-TRANSFER RECEIVED,,50.00,1284.56
10/13/2026,ROGERS WIRELESS,85.00,,1199.56
"""

SCOTIA_CSV = """Filter,Date,Description,Sub-description,Status,Type of Transaction,Amount
,2026-10-03,TIM HORTONS,,Posted,Debit,-4.50
,2026-10-05,GOODLIFE FITNESS,,Posted,Debit,-18.00
"""

MBNA_CSV = """Posted Date,Payee,Address,Amount
10/03/2026,AMAZON.CA,AMAZON.CA,25.99
10/04/2026,UBER* TRIP,TORONTO,14.20
10/06/2026,LOBLAWS,TORONTO,63.10
10/08/2026,PAYMENT RECEIVED,,-200.00
"""

DAY_FIRST_CSV = """Date,Description,Amount
25/10/2026,TIM HORTONS,-4.50
03/10/2026,STARBUCKS,-6.10
"""

OFX = """OFXHEADER:100
DATA:OFXSGML
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261003120000[-5:EST]<TRNAMT>-4.50<FITID>1<NAME>TIM HORTONS #1234</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261015<TRNAMT>1150.00<FITID>2<NAME>PAYROLL<MEMO>WVRST</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


def amounts(rows):
    return [(r["date"], r["amount"]) for r in rows]


class ParseStatementTests(TestCase):
    def test_rbc_header_csv(self):
        rows, info = parse_statement("rbc.csv", RBC_CSV.encode())
        self.assertEqual(amounts(rows), [
            (date(2026, 10, 3), Decimal("-4.50")),
            (date(2026, 10, 15), Decimal("1150.00")),
            (date(2026, 10, 16), Decimal("-200.00")),
        ])
        self.assertEqual(rows[0]["description"], "TIM HORTONS #1234 TORONTO ON")
        self.assertTrue(info["signed_amount"])

    def test_cibc_headerless_debit_credit(self):
        rows, info = parse_statement("cibc.csv", CIBC_CSV.encode())
        self.assertEqual(amounts(rows), [
            (date(2026, 10, 3), Decimal("-4.50")),
            (date(2026, 10, 4), Decimal("200.00")),
            (date(2026, 10, 5), Decimal("-16.49")),
        ])
        self.assertFalse(info["signed_amount"])

    def test_td_headerless_ignores_balance_column(self):
        rows, _ = parse_statement("td.csv", TD_CSV.encode())
        self.assertEqual(amounts(rows), [
            (date(2026, 10, 3), Decimal("-4.50")),
            (date(2026, 10, 4), Decimal("50.00")),
            (date(2026, 10, 13), Decimal("-85.00")),
        ])

    def test_scotia_header_csv(self):
        rows, _ = parse_statement("scotia.csv", SCOTIA_CSV.encode())
        self.assertEqual(amounts(rows)[1], (date(2026, 10, 5), Decimal("-18.00")))

    def test_card_csv_with_positive_purchases_suggests_flip(self):
        rows, info = parse_statement("mbna.csv", MBNA_CSV.encode())
        self.assertEqual(len(rows), 4)
        self.assertTrue(suggest_flip(rows, info))

    def test_bank_csv_does_not_suggest_flip(self):
        rows, info = parse_statement("scotia.csv", SCOTIA_CSV.encode())
        self.assertFalse(suggest_flip(rows, info))

    def test_day_first_dates(self):
        rows, _ = parse_statement("x.csv", DAY_FIRST_CSV.encode())
        self.assertEqual([r["date"] for r in rows], [date(2026, 10, 25), date(2026, 10, 3)])

    def test_ofx(self):
        rows, info = parse_statement("bank.qfx", OFX.encode())
        self.assertEqual(info["format"], "ofx")
        self.assertEqual(amounts(rows), [
            (date(2026, 10, 3), Decimal("-4.50")),
            (date(2026, 10, 15), Decimal("1150.00")),
        ])
        self.assertEqual(rows[1]["description"], "PAYROLL WVRST")

    def test_cp1252_encoding(self):
        text = "Date,Description,Amount\n2026-10-03,CAFÉ DÉPANNEUR,-3.00\n"
        rows, _ = parse_statement("x.csv", text.encode("cp1252"))
        self.assertEqual(rows[0]["description"], "CAFÉ DÉPANNEUR")

    def test_rejects_unreadable_files(self):
        with self.assertRaises(StatementError):
            parse_statement("x.csv", b"hello,world\nfoo,bar\n")
        with self.assertRaises(StatementError):
            parse_statement("x.pdf", b"%PDF-1.4 binary")

    def test_clean_title(self):
        self.assertEqual(clean_title("TIM HORTONS #1234  TORONTO ON"), "Tim Hortons Toronto On")
        self.assertEqual(clean_title("Amazon.ca 4500********1234"), "Amazon.ca")


class StatementApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="import@example.test", username="import", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def preview(self, name, content):
        return self.client.post("/api/statements/preview/",
                                {"file": SimpleUploadedFile(name, content.encode())}, format="multipart")

    def test_preview_categorises_and_flags(self):
        res = self.preview("rbc.csv", RBC_CSV)
        self.assertEqual(res.status_code, 200)
        tim, pay, transfer = res.data["rows"]
        self.assertEqual(tim["title"], "Tim Hortons Toronto On")
        self.assertEqual(tim["amount"], "4.50")
        self.assertEqual(tim["expense_category"], "Food & Dining")
        self.assertEqual(pay["income_category"], "Salary")
        self.assertTrue(transfer["is_transfer"])
        self.assertFalse(tim["is_transfer"])
        self.assertFalse(Expense.objects.exists())  # preview saves nothing

    def test_preview_learns_from_history(self):
        Expense.objects.create(user=self.user, title="Tim Hortons Toronto", amount=2,
                               category="Entertainment", date=date(2026, 9, 1))
        res = self.preview("rbc.csv", RBC_CSV)
        self.assertEqual(res.data["rows"][0]["expense_category"], "Entertainment")

    def test_preview_flags_duplicates_within_three_days(self):
        Expense.objects.create(user=self.user, title="Coffee", amount=Decimal("4.5"),
                               category="Food & Dining", date=date(2026, 10, 5))
        res = self.preview("rbc.csv", RBC_CSV)
        self.assertTrue(res.data["rows"][0]["duplicate_expense"])
        self.assertFalse(res.data["rows"][2]["duplicate_expense"])

    def test_preview_rejects_bad_file(self):
        res = self.preview("x.csv", "hello,world\n")
        self.assertEqual(res.status_code, 400)
        self.assertIn("error", res.data)

    def test_import_saves_rows(self):
        res = self.client.post("/api/statements/import/", {"filename": "rbc.csv", "rows": [
            {"kind": "expense", "title": "Tim Hortons", "amount": "4.50", "date": "2026-10-03", "category": "Food & Dining"},
            {"kind": "income", "title": "Payroll", "amount": "1150.00", "date": "2026-10-15", "category": "Salary"},
        ]}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data, {"imported_expenses": 1, "imported_income": 1})
        exp = Expense.objects.get()
        self.assertEqual(exp.user, self.user)
        self.assertEqual(exp.tags, ["imported"])
        self.assertEqual(Income.objects.get().category, "Salary")

    def test_import_is_all_or_nothing(self):
        res = self.client.post("/api/statements/import/", {"rows": [
            {"kind": "expense", "title": "Ok", "amount": "4.50", "date": "2026-10-03", "category": "Food & Dining"},
            {"kind": "expense", "title": "Bad", "amount": "4.50", "date": "2026-10-03", "category": "Not a category"},
        ]}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(Expense.objects.exists())

    def test_requires_login(self):
        res = APIClient().post("/api/statements/import/", {"rows": []}, format="json")
        self.assertEqual(res.status_code, 401)


SIMPLII_CSV = """Date,Transaction Details,Funds Out,Funds In
10/06/2026,INTERAC E-TRANSFER SENT,500.00,
10/06/2026,MBNA PAYMENT,300.00,
10/09/2026,PIZZA PIZZA,25.00,
"""

CIBC_RECEIVING_CSV = """2026-10-07,E-TRANSFER RECEIVED,,500.00,
2026-10-08,LOBLAWS #55,63.10,,
2026-10-10,REFUND PIZZA,,25.00,
"""


class TransferMatchingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="transfers@example.test", username="transfers", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def preview(self, *files):
        uploads = [SimpleUploadedFile(name, content.encode()) for name, content in files]
        return self.client.post("/api/statements/preview/", {"files": uploads}, format="multipart")

    def by_title(self, res):
        return {r["title"]: r for r in res.data["rows"]}

    def test_pairs_transfer_between_two_banks(self):
        res = self.preview(("simplii.csv", SIMPLII_CSV), ("cibc.csv", CIBC_RECEIVING_CSV))
        self.assertEqual(res.status_code, 200)
        self.assertEqual([f["filename"] for f in res.data["files"]], ["simplii.csv", "cibc.csv"])
        rows = self.by_title(res)
        sent, received = rows["Interac E-Transfer Sent"], rows["E-Transfer Received"]
        self.assertEqual(sent["transfer_match"]["type"], "pair")
        self.assertEqual(sent["transfer_match"]["row"], received["id"])
        self.assertEqual(received["transfer_match"]["row"], sent["id"])
        self.assertEqual((sent["file"], received["file"]), (0, 1))

    def test_ordinary_purchase_and_refund_are_not_paired(self):
        rows = self.by_title(self.preview(("simplii.csv", SIMPLII_CSV), ("cibc.csv", CIBC_RECEIVING_CSV)))
        self.assertIsNone(rows["Pizza Pizza"]["transfer_match"])
        self.assertIsNone(rows["Refund Pizza"]["transfer_match"])
        self.assertIsNone(rows["Loblaws"]["transfer_match"])

    def test_single_file_is_never_self_paired(self):
        csv_text = "Date,Description,Amount\n2026-10-06,TRANSFER OUT,-100\n2026-10-06,TRANSFER IN,100\n"
        rows = self.preview(("one.csv", csv_text)).data["rows"]
        self.assertTrue(all(r["transfer_match"] is None for r in rows))

    def test_card_payment_from_another_bank_pairs_with_flipped_card_statement(self):
        rows = self.by_title(self.preview(("simplii.csv", SIMPLII_CSV), ("mbna.csv", MBNA_CSV.replace("200.00", "300.00"))))
        self.assertEqual(rows["Mbna Payment"]["transfer_match"]["type"], "pair")
        self.assertEqual(rows["Payment Received"]["transfer_match"]["type"], "pair")

    def test_matches_other_half_imported_earlier(self):
        Expense.objects.create(user=self.user, title="Interac E-Transfer Sent", amount=500, category="Other",
                               date=date(2026, 10, 6), notes="Imported from simplii.csv")
        rows = self.by_title(self.preview(("cibc.csv", CIBC_RECEIVING_CSV)))
        match = rows["E-Transfer Received"]["transfer_match"]
        self.assertEqual(match["type"], "existing")
        self.assertEqual(match["kind"], "expense")
        self.assertEqual(match["title"], "Interac E-Transfer Sent")

    def test_manual_entries_are_not_treated_as_earlier_imports(self):
        Expense.objects.create(user=self.user, title="Sent to landlord", amount=500, category="Housing",
                               date=date(2026, 10, 6))
        rows = self.by_title(self.preview(("cibc.csv", CIBC_RECEIVING_CSV)))
        self.assertIsNone(rows["E-Transfer Received"]["transfer_match"])

    def test_overlapping_statements_flag_duplicates(self):
        rows = self.preview(("a.csv", SIMPLII_CSV), ("b.csv", SIMPLII_CSV)).data["rows"]
        second_copy = [r for r in rows if r["file"] == 1]
        self.assertTrue(all(r["duplicate_expense"] for r in second_copy))

    def test_import_notes_record_each_rows_source(self):
        self.client.post("/api/statements/import/", {"filename": "batch", "rows": [
            {"kind": "expense", "title": "Loblaws", "amount": "63.10", "date": "2026-10-08",
             "category": "Food & Dining", "source": "cibc.csv"},
        ]}, format="json")
        self.assertEqual(Expense.objects.get().notes, "Imported from cibc.csv")

    def test_too_many_files(self):
        res = self.preview(*[(f"{i}.csv", SCOTIA_CSV) for i in range(11)])
        self.assertEqual(res.status_code, 400)
