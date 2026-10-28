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
        self.assertEqual({k: res.data[k] for k in ("imported_expenses", "imported_income", "ticked_checklist", "statements_saved")},
                         {"imported_expenses": 1, "imported_income": 1, "ticked_checklist": 0, "statements_saved": 0})
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

    def test_same_amount_hopping_through_accounts_pairs_each_hop(self):
        # $500 Simplii → CIBC, then $500 CIBC → Visa, same day, files picked
        # in any order (Visa before CIBC here).
        simplii = "Date,Description,Amount\n2026-05-06,TRANSFER TO CIBC,-500.00\n"
        cibc = ("Date,Description,Amount\n2026-05-06,INTERNET TRANSFER FROM SIMPLII,500.00\n"
                "2026-05-06,PAYMENT CIBC VISA,-500.00\n2026-05-06,LOBLAWS,-42.10\n")
        visa = "Date,Description,Amount\n2026-05-03,UBER EATS,-30.00\n2026-05-06,PAYMENT THANK YOU,500.00\n"
        res = self.preview(("simplii.csv", simplii), ("visa.csv", visa), ("cibc.csv", cibc))
        rows = self.by_title(res)
        hop1 = rows["Transfer To Cibc"]["transfer_match"]
        hop2 = rows["Payment Cibc Visa"]["transfer_match"]
        self.assertEqual(res.data["rows"][hop1["row"]]["file"], 2)   # Simplii → CIBC deposit
        self.assertEqual(res.data["rows"][hop2["row"]]["file"], 1)   # CIBC → Visa credit
        self.assertIsNone(rows["Loblaws"]["transfer_match"])
        self.assertIsNone(rows["Uber Eats"]["transfer_match"])

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
        res = self.preview(*[(f"{i}.csv", SCOTIA_CSV) for i in range(51)])
        self.assertEqual(res.status_code, 400)


# ─────────────────────────────────────────────
# PDF statements
# ─────────────────────────────────────────────

def make_pdf(pages):
    """Build a minimal text PDF. `pages` is a list of pages, each a list of
    (x, y, text) placed in 9pt Helvetica on a US-letter page."""
    def esc(t):
        return t.replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")

    objects = []  # bodies; object n is objects[n-1]
    page_ids = []
    objects.append(None)                                   # 1 catalog
    objects.append(None)                                   # 2 pages
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")  # 3 font
    for items in pages:
        stream = "".join(f"BT /F1 9 Tf {x} {y} Td ({esc(t)}) Tj ET\n" for x, y, t in items)
        objects.append(f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}endstream")
        content_id = len(objects)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>")
        page_ids.append(len(objects))
    objects[0] = "<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_ids)}] /Count {len(page_ids)} >>"

    out = b"%PDF-1.4\n"
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def rows_of(pdf_bytes):
    rows, info = parse_statement("statement.pdf", pdf_bytes)
    return [(r["date"], r["amount"], r["description"]) for r in rows], info


CARD_PDF = make_pdf([[
    (50, 760, "CIBC Dividend Visa"),
    (50, 745, "Statement period September 15, 2026 to October 14, 2026"),
    (50, 700, "Trans date"), (110, 700, "Post date"), (170, 700, "Description"), (480, 700, "Amount($)"),
    (50, 685, "Sep 28"), (110, 685, "Sep 30"), (170, 685, "TIM HORTONS #1234 TORONTO ON"), (490, 685, "4.50"),
    (50, 670, "Oct 02"), (110, 670, "Oct 03"), (170, 670, "PAYMENT THANK YOU/PAIEMENT MERCI"), (490, 670, "-500.00"),
    (50, 655, "Oct 05"), (110, 655, "Oct 06"), (170, 655, "AMAZON.CA"), (490, 655, "25.99"),
    (170, 643, "AMZN.COM/BILL ON"),
    (50, 620, "Total for 4500 XXXX XXXX 1234"), (490, 620, "30.49"),
]])

BANK_PDF = make_pdf([[
    (50, 760, "RBC Royal Bank - Your account statement"),
    (50, 745, "From October 1, 2026 to October 31, 2026"),
    (50, 700, "Date"), (110, 700, "Description"), (330, 700, "Withdrawals ($)"), (420, 700, "Deposits ($)"), (510, 700, "Balance ($)"),
    (50, 685, "01 Oct"), (110, 685, "Opening Balance"), (515, 685, "1,000.00"),
    (50, 670, "03 Oct"), (110, 670, "Tim Hortons"), (340, 670, "4.50"), (515, 670, "995.50"),
    (50, 655, "15 Oct"), (110, 655, "Payroll Deposit WVRST"), (425, 655, "1,150.00"), (515, 655, "2,145.50"),
    (50, 640, "16 Oct"), (110, 640, "Online Transfer to Savings"), (340, 640, "200.00"), (515, 640, "1,945.50"),
    (50, 625, "31 Oct"), (110, 625, "Closing Balance"), (515, 625, "1,945.50"),
]])


class PdfStatementTests(TestCase):
    def test_credit_card_pdf(self):
        rows, info = rows_of(CARD_PDF)
        self.assertEqual(info["format"], "pdf")
        self.assertEqual([(d, a) for d, a, _ in rows], [
            (date(2026, 9, 28), Decimal("-4.50")),     # purchase = money out
            (date(2026, 10, 2), Decimal("500.00")),    # payment = money in
            (date(2026, 10, 5), Decimal("-25.99")),
        ])
        self.assertEqual(rows[0][2], "TIM HORTONS #1234 TORONTO ON")
        self.assertEqual(rows[2][2], "AMAZON.CA AMZN.COM/BILL ON")  # wrapped line joined

    def test_bank_account_pdf_uses_columns(self):
        rows, _ = rows_of(BANK_PDF)
        self.assertEqual([(d, a) for d, a, _ in rows], [
            (date(2026, 10, 3), Decimal("-4.50")),
            (date(2026, 10, 15), Decimal("1150.00")),
            (date(2026, 10, 16), Decimal("-200.00")),
        ])

    def test_december_charge_on_january_statement(self):
        pdf = make_pdf([[
            (50, 745, "Statement period December 15, 2026 to January 14, 2027"),
            (50, 700, "Trans date"), (170, 700, "Description"), (480, 700, "Amount"),
            (50, 685, "Dec 28"), (170, 685, "CINEPLEX"), (490, 685, "18.00"),
            (50, 670, "Jan 03"), (170, 670, "PRESTO"), (490, 670, "3.35"),
        ]])
        rows, _ = rows_of(pdf)
        self.assertEqual([d for d, _, _ in rows], [date(2026, 12, 28), date(2027, 1, 3)])

    def test_headerless_pdf_uses_running_balance(self):
        pdf = make_pdf([[
            (50, 745, "Statement date October 31, 2026"),
            (50, 685, "Oct 01"), (110, 685, "Opening balance"), (510, 685, "1,000.00"),
            (50, 670, "Oct 03"), (110, 670, "COFFEE"), (420, 670, "4.50"), (510, 670, "995.50"),
            (50, 655, "Oct 05"), (110, 655, "E-TRANSFER"), (420, 655, "100.00"), (510, 655, "1,095.50"),
        ]])
        rows, _ = rows_of(pdf)
        self.assertEqual([a for _, a, _ in rows], [Decimal("-4.50"), Decimal("100.00")])

    def test_pdf_without_text(self):
        with self.assertRaisesRegex(StatementError, "scanned"):
            parse_statement("scan.pdf", make_pdf([[]]))

    def test_not_really_a_pdf(self):
        with self.assertRaises(StatementError):
            parse_statement("fake.pdf", b"%PDF-1.4 garbage")

    def test_pdf_upload_through_api(self):
        user = get_user_model().objects.create_user(email="pdf@example.test", username="pdf", password="x-Test-12345")
        client = APIClient()
        client.force_authenticate(user)
        res = client.post("/api/statements/preview/",
                          {"files": [SimpleUploadedFile("cibc-visa.pdf", CARD_PDF, content_type="application/pdf")]},
                          format="multipart")
        self.assertEqual(res.status_code, 200)
        rows = {r["title"]: r for r in res.data["rows"]}
        self.assertEqual(res.data["files"][0]["format"], "pdf")
        self.assertFalse(res.data["files"][0]["suggest_flip"])
        self.assertTrue(rows["Payment Thank You/Paiement Merci"]["is_transfer"])
        self.assertEqual(rows["Tim Hortons Toronto On"]["expense_category"], "Food & Dining")


class PlannerCrossCheckTests(TestCase):
    def setUp(self):
        from financial_planner.models import Debt, MonthlyPlan, ChecklistItem
        self.user = get_user_model().objects.create_user(
            email="planner-match@example.test", username="pm", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.debt = Debt.objects.create(user=self.user, name="CIBC Visa", current_balance=Decimal("3494.00"),
                                        annual_interest_rate=Decimal("21.99"), minimum_payment=Decimal("70.00"), due_day=13)
        plan = MonthlyPlan.objects.create(user=self.user, year=2026, month=10)
        self.rent = ChecklistItem.objects.create(monthly_plan=plan, label="Rent", amount=650, due_day=26, category="fixed_expense")
        self.cibc = ChecklistItem.objects.create(monthly_plan=plan, label="CIBC Visa minimum payment", amount=70,
                                                 due_day=13, category="debt_min", linked_debt=self.debt)
        self.rogers = ChecklistItem.objects.create(monthly_plan=plan, label="Mobile Bill", amount=30, due_day=26, category="fixed_expense")

    def preview(self, *files):
        uploads = [SimpleUploadedFile(name, content.encode()) for name, content in files]
        res = self.client.post("/api/statements/preview/", {"files": uploads}, format="multipart")
        self.assertEqual(res.status_code, 200)
        return {r["title"]: r for r in res.data["rows"]}

    def test_matches_by_amount_and_due_date_or_name(self):
        bank = ("Date,Description,Amount\n"
                "2026-10-24,E-TRANSFER TO LANDLORD,-650.00\n"     # near Rent's due day
                "2026-10-12,ROGERS WIRELESS,-30.00\n"             # 14 days from due, no name overlap
                "2026-10-15,GROCERIES,-650.00\n")                 # same amount as rent but already used
        rows = self.preview(("chequing.csv", bank))
        self.assertEqual(rows["E-Transfer To Landlord"]["checklist_match"]["id"], self.rent.id)
        self.assertIsNone(rows["Rogers Wireless"]["checklist_match"])
        self.assertIsNone(rows["Groceries"]["checklist_match"])

    def test_card_payment_matches_debt_minimum_via_filename(self):
        card = "Date,Description,Amount\n2026-10-02,PAYMENT THANK YOU,70.00\n2026-10-05,AMAZON.CA,-70.00\n"
        rows = self.preview(("cibc-visa-oct.csv", card))
        self.assertEqual(rows["Payment Thank You"]["checklist_match"]["id"], self.cibc.id)
        self.assertIsNone(rows["Amazon.Ca"]["checklist_match"])  # item already matched once

    def test_other_months_and_ticked_items_are_ignored(self):
        self.rent.is_completed = True
        self.rent.save()
        bank = "Date,Description,Amount\n2026-10-25,RENT,-650.00\n2026-11-25,RENT,-650.00\n"
        rows = self.preview(("chequing.csv", bank))
        self.assertIsNone(rows["Rent"]["checklist_match"])

    def test_import_ticks_items_and_pays_down_debt(self):
        res = self.client.post("/api/statements/import/", {"rows": [
            {"kind": "expense", "title": "Rent", "amount": "650", "date": "2026-10-24", "category": "Housing"},
        ], "checklist_items": [self.rent.id, self.cibc.id]}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["ticked_checklist"], 2)
        self.rent.refresh_from_db(); self.cibc.refresh_from_db(); self.debt.refresh_from_db()
        self.assertTrue(self.rent.is_completed and self.cibc.is_completed)
        self.assertEqual(self.debt.current_balance, Decimal("3424.00"))

    def test_can_tick_without_importing_rows(self):
        res = self.client.post("/api/statements/import/", {"rows": [], "checklist_items": [self.cibc.id]}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["ticked_checklist"], 1)

    def test_cannot_tick_someone_elses_items(self):
        other = APIClient()
        other.force_authenticate(get_user_model().objects.create_user(email="o@example.test", username="o", password="x-Test-12345"))
        res = other.post("/api/statements/import/", {"rows": [], "checklist_items": [self.rent.id]}, format="json")
        self.assertEqual(res.data["ticked_checklist"], 0)
        self.rent.refresh_from_db()
        self.assertFalse(self.rent.is_completed)


class TransferWordingTests(TestCase):
    def test_e_transfer_to_someone_is_not_an_internal_transfer(self):
        from .statement_import import TRANSFER_PATTERNS
        self.assertIsNone(TRANSFER_PATTERNS.search("E-TRANSFER TO LANDLORD"))
        self.assertIsNone(TRANSFER_PATTERNS.search("INTERAC ETRANSFER TO JOHN"))
        self.assertIsNotNone(TRANSFER_PATTERNS.search("ONLINE TRANSFER TO SAVINGS"))
        self.assertIsNotNone(TRANSFER_PATTERNS.search("TRANSFER TO 00123-4567"))

    def test_planner_bill_match_overrides_transfer_wording(self):
        from financial_planner.models import ChecklistItem, MonthlyPlan
        user = get_user_model().objects.create_user(email="tw@example.test", username="tw", password="x-Test-12345")
        plan = MonthlyPlan.objects.create(user=user, year=2026, month=10)
        ChecklistItem.objects.create(monthly_plan=plan, label="Family transfer", amount=500, due_day=28, category="transfer")
        client = APIClient()
        client.force_authenticate(user)
        res = client.post("/api/statements/preview/", {"files": [SimpleUploadedFile(
            "chq.csv", b"Date,Description,Amount\n2026-10-27,ONLINE TRANSFER TO FAMILY,-500.00\n")]}, format="multipart")
        row = res.data["rows"][0]
        self.assertIsNotNone(row["checklist_match"])
        self.assertFalse(row["is_transfer"])


# Chequing statement where only the first transaction of each day shows
# its date and each description spans two lines (type, then merchant).
SAME_DAY_PDF = make_pdf([[
    (40, 760, "Account statement  September 1, 2026 to September 30, 2026"),
    (40, 720, "Date"), (120, 720, "Description"), (330, 720, "Withdrawals ($)"), (440, 720, "Deposits ($)"), (520, 720, "Balance ($)"),
    (40, 705, "Sep 1"), (120, 705, "Opening balance"), (520, 705, "$2,552.58"),
    (40, 690, "Sep 1"), (120, 690, "VISA DEBIT RETAIL  PURCHASE"), (360, 690, "30.00"), (525, 690, "2,522.58"),
    (120, 678, "PRESTO MOBI/SKC   624411249965"),
    (120, 663, "PREAUTHORIZED DEBIT"), (350, 663, "2,550.00"), (530, 663, "-27.42"),
    (120, 651, "Realstar Corp"),
    (40, 636, "Sep 3"), (120, 636, "E-TRANSFER        011693914016"), (455, 636, "350.00"), (525, 636, "322.58"),
    (120, 624, "Alex Sample"),
    (120, 609, "E-TRANSFER        106101762546"), (360, 609, "350.00"), (530, 609, "-27.42"),
    (120, 597, "Sam Example"),
    (120, 582, "VISA DEBIT RETAIL  PURCHASE"), (365, 582, "1.92"), (530, 582, "-29.34"),
    (120, 570, "TIM HORTONS #24   624637090333"),
    (40, 555, "Sep 4"), (120, 555, "E-TRANSFER        011695311404"), (445, 555, "1,372.97"), (520, 555, "1,343.63"),
    (120, 543, "Payroll Co"),
    (40, 520, "Sep 30"), (120, 520, "Closing balance"), (520, 520, "$1,343.63"),
]])


class SameDayPdfTests(TestCase):
    def test_transactions_without_their_own_date_are_kept(self):
        rows, _ = parse_statement("chequing.pdf", SAME_DAY_PDF)
        self.assertEqual([(r["date"], r["amount"]) for r in rows], [
            (date(2026, 9, 1), Decimal("-30.00")),
            (date(2026, 9, 1), Decimal("-2550.00")),
            (date(2026, 9, 3), Decimal("350.00")),
            (date(2026, 9, 3), Decimal("-350.00")),
            (date(2026, 9, 3), Decimal("-1.92")),
            (date(2026, 9, 4), Decimal("1372.97")),
        ])

    def test_titles_lead_with_the_merchant(self):
        rows, _ = parse_statement("chequing.pdf", SAME_DAY_PDF)
        titles = [clean_title(r["description"]) for r in rows]
        self.assertEqual(titles, [
            "Presto Mobi/Skc", "Realstar Corp", "E-Transfer Alex Sample",
            "E-Transfer Sam Example", "Tim Hortons", "E-Transfer Payroll Co",
        ])

    def test_categories(self):
        from .statement_import import EXPENSE_RULES, _match
        rows, _ = parse_statement("chequing.pdf", SAME_DAY_PDF)
        self.assertEqual(_match(EXPENSE_RULES, rows[0]["description"], "Other"), "Transport")
        self.assertEqual(_match(EXPENSE_RULES, rows[1]["description"], "Other"), "Housing")
        self.assertEqual(_match(EXPENSE_RULES, rows[4]["description"], "Other"), "Food & Dining")


# Card statement with a summary box beside the transactions and summary
# lines after them — none of those figures are spending.
CARD_WITH_SUMMARY_PDF = make_pdf([[
    (40, 770, "Statement period September 15, 2026 to October 14, 2026"),
    (40, 740, "Trans date"), (100, 740, "Post date"), (160, 740, "Description"), (330, 740, "Amount ($)"),
    (420, 740, "Your account summary"),
    (40, 725, "Sep 28"), (100, 725, "Sep 30"), (160, 725, "TIM HORTONS #1234"), (345, 725, "4.50"),
    (420, 725, "Previous balance"), (530, 725, "$3,494.00"),
    (40, 710, "Oct 02"), (100, 710, "Oct 03"), (160, 710, "PAYMENT THANK YOU"), (340, 710, "-500.00"),
    (420, 710, "Payments & credits"), (530, 710, "-$500.00"),
    (40, 695, "Oct 05"), (100, 695, "Oct 06"), (160, 695, "AMAZON.CA"), (343, 695, "25.99"),
    (420, 695, "Purchases & charges"), (530, 695, "$30.49"),
    (420, 680, "Minimum due"), (530, 680, "$70.00"),
    (420, 665, "Statement balance"), (530, 665, "$3,024.49"),
    (160, 640, "Total for 4500 XXXX XXXX 1234"), (343, 640, "30.49"),
    (40, 610, "Purchases & charges"), (343, 610, "30.49"),
    (40, 595, "Amount due"), (343, 595, "70.00"),
    (40, 580, "If you make only the minimum payment each month, it will take 12 years"), (343, 580, "9,876.00"),
]])


class PdfSummaryLinesTests(TestCase):
    def test_summary_figures_are_not_transactions(self):
        rows, _ = parse_statement("visa.pdf", CARD_WITH_SUMMARY_PDF)
        self.assertEqual([(r["date"], r["amount"], r["description"]) for r in rows], [
            (date(2026, 9, 28), Decimal("-4.50"), "TIM HORTONS #1234"),
            (date(2026, 10, 2), Decimal("500.00"), "PAYMENT THANK YOU"),
            (date(2026, 10, 5), Decimal("-25.99"), "AMAZON.CA"),
        ])

    def test_merchant_named_total_is_kept(self):
        pdf = make_pdf([[
            (40, 745, "Statement period September 15, 2026 to October 14, 2026"),
            (40, 700, "Trans date"), (160, 700, "Description"), (330, 700, "Amount"),
            (40, 685, "Oct 03"), (160, 685, "TOTAL WINE & MORE"), (343, 685, "45.00"),
            (40, 670, "Oct 04"), (160, 670, "PRESTO"), (345, 670, "3.35"),
        ]])
        rows, _ = parse_statement("visa.pdf", pdf)
        self.assertEqual([r["description"] for r in rows], ["TOTAL WINE & MORE", "PRESTO"])


class MixedUploadTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="mix@example.test", username="mix", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_bad_file_is_skipped_and_the_rest_previewed(self):
        res = self.client.post("/api/statements/preview/", {"files": [
            SimpleUploadedFile("good.csv", SCOTIA_CSV.encode()),
            SimpleUploadedFile("scan.pdf", make_pdf([[]])),
        ]}, format="multipart")
        self.assertEqual(res.status_code, 200)
        self.assertEqual([f["filename"] for f in res.data["files"]], ["good.csv"])
        self.assertEqual(res.data["skipped"][0]["filename"], "scan.pdf")
        self.assertIn("scanned", res.data["skipped"][0]["error"])
        self.assertEqual(len(res.data["rows"]), 2)

    def test_all_bad_files_is_an_error(self):
        res = self.client.post("/api/statements/preview/", {"files": [
            SimpleUploadedFile("x.csv", b"hello,world\n")]}, format="multipart")
        self.assertEqual(res.status_code, 400)
        self.assertIn("error", res.data)


class FriendlyErrorTests(TestCase):
    def test_non_statement_text_file(self):
        with self.assertRaisesRegex(StatementError, "doesn't look like a bank statement"):
            parse_statement("notes.txt", b"just some notes, not a statement")


class OriginalCurrencyTests(TestCase):
    """₹7,000 typed in INR is kept as typed next to its CAD amount."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(email="oc@example.test", username="oc", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_expense_keeps_what_was_typed(self):
        res = self.client.post("/api/expenses/", {"title": "Abroad", "amount": "103.29", "original_amount": "7000",
                                                  "original_currency": "inr", "category": "Abroad Expense",
                                                  "date": "2026-10-28"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual((res.data["original_amount"], res.data["original_currency"]), ("7000.00", "INR"))
        # A CAD-only change (e.g. from Aurora) drops the old original.
        res = self.client.patch(f"/api/expenses/{res.data['id']}/", {"amount": "110.00"}, format="json")
        self.assertEqual((res.data["original_amount"], res.data["original_currency"]), (None, ""))

    def test_cad_or_bad_currency(self):
        res = self.client.post("/api/income/", {"title": "Pay", "amount": "100", "original_amount": "100",
                                                "original_currency": "CAD", "category": "Salary", "date": "2026-10-28"}, format="json")
        self.assertEqual((res.data["original_amount"], res.data["original_currency"]), (None, ""))
        res = self.client.post("/api/income/", {"title": "Pay", "amount": "100", "original_amount": "100",
                                                "original_currency": "XYZ", "category": "Salary", "date": "2026-10-28"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_recurring_original_reaches_the_checklist(self):
        from financial_planner.services import get_or_create_month_plan
        res = self.client.post("/api/fp/recurring/", {"label": "RD-Abroad", "amount": "103.29", "original_amount": "7000",
                                                      "original_currency": "INR", "category": "temp_payment", "due_day": 12},
                               format="json")
        self.assertEqual(res.status_code, 201, res.data)
        today = date.today()
        plan = get_or_create_month_plan(self.user, today.year, today.month)
        if isinstance(plan, tuple):
            plan = plan[0]
        item = plan.checklist_items.get(label="RD-Abroad")
        self.assertEqual((item.original_amount, item.original_currency), (Decimal("7000.00"), "INR"))
