"""
Synthetic bank & credit-card statements for testing statement import.

Each statement is modelled on a real Canadian bank's export layout and
carries the transactions it should produce (`expected`), so the QA runner
can compare the importer's output row by row. All names and numbers are
made up.

Run `python tools/statement_qa/run.py` from backend/ to generate the files
and check them. PDFs need reportlab (requirements-dev.txt).
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

D = Decimal


@dataclass
class Txn:
    day: date
    amount: Decimal          # negative = money out of the account holder's pocket
    lines: list              # description lines as printed
    category: str = ""       # e.g. CIBC "Spend Categories" column
    extra: str = ""          # e.g. foreign-currency note printed under the line


@dataclass
class Statement:
    name: str
    kind: str                # "pdf" or "csv"
    bank: str
    notes: str
    expected: list = field(default_factory=list)   # [(date, Decimal signed)]
    content: bytes = b""


# ─────────────────────────────────────────────
# PDF drawing helpers
# ─────────────────────────────────────────────

def _money(v):
    return f"{abs(v):,.2f}"


def _new_canvas():
    from io import BytesIO
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    return c, buf


def _finish(c, buf):
    c.save()
    return buf.getvalue()


def _txt(c, x, y, s, size=8.5, bold=False, right=False):
    c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
    (c.drawRightString if right else c.drawString)(x, y, s)


# ─────────────────────────────────────────────
# Bank account statements
# ─────────────────────────────────────────────

CHEQUING = [
    Txn(date(2026, 9, 1), D("-30.00"), ["VISA DEBIT RETAIL PURCHASE", "PRESTO MOBI/SKC   624411249965"]),
    Txn(date(2026, 9, 1), D("-2550.00"), ["PREAUTHORIZED DEBIT", "Realstar Corp"]),
    Txn(date(2026, 9, 3), D("350.00"), ["E-TRANSFER        011693914016", "Alex Sample"]),
    Txn(date(2026, 9, 3), D("-350.00"), ["E-TRANSFER        106101762546", "Sam Example"]),
    Txn(date(2026, 9, 3), D("-1.92"), ["VISA DEBIT RETAIL PURCHASE", "TIM HORTONS #24   624637090333"]),
    Txn(date(2026, 9, 4), D("1372.97"), ["PAYROLL DEPOSIT", "WVRST INC"]),
    Txn(date(2026, 9, 10), D("-100.00"), ["ATM WITHDRAWAL", "BLOOR & YONGE"]),
    Txn(date(2026, 9, 12), D("-63.10"), ["INTERAC PURCHASE", "LOBLAWS #1043"]),
    Txn(date(2026, 9, 15), D("-85.00"), ["PREAUTHORIZED DEBIT", "ROGERS WIRELESS"]),
    Txn(date(2026, 9, 18), D("1500.00"), ["PAYROLL DEPOSIT", "WVRST INC"]),
    Txn(date(2026, 9, 18), D("-500.00"), ["ONLINE TRANSFER TO DEPOSIT ACCOUNT-1234"]),
    Txn(date(2026, 9, 22), D("-70.00"), ["ONLINE BANKING PAYMENT", "CIBC VISA"]),
    Txn(date(2026, 9, 25), D("-18.00"), ["PREAUTHORIZED DEBIT", "GOODLIFE FITNESS"]),
    Txn(date(2026, 9, 28), D("-500.00"), ["E-TRANSFER        106101999999", "Family Support"]),
    Txn(date(2026, 9, 30), D("-4.00"), ["MONTHLY FEE"]),
]
OPENING = D("2552.58")


def _running(txns, opening):
    bal, out = opening, []
    for t in txns:
        bal += t.amount
        out.append(bal)
    return out


def rbc_chequing_pdf():
    """RBC style: date once per day, 2-line descriptions, summary box above
    the table, header repeated on page 2, page footers."""
    c, buf = _new_canvas()
    balances = _running(CHEQUING, OPENING)
    rows_per_page = 9

    def header(y):
        _txt(c, 40, y, "Date", bold=True); _txt(c, 95, y, "Description", bold=True)
        _txt(c, 400, y, "Withdrawals ($)", bold=True, right=True)
        _txt(c, 485, y, "Deposits ($)", bold=True, right=True)
        _txt(c, 570, y, "Balance ($)", bold=True, right=True)
        c.line(40, y - 4, 572, y - 4)

    _txt(c, 40, 750, "RBC Royal Bank", 13, bold=True)
    _txt(c, 40, 734, "Your account statement   From September 1, 2026 to September 30, 2026", 9)
    _txt(c, 40, 706, "Summary of your account", 10, bold=True)
    deposits = sum(t.amount for t in CHEQUING if t.amount > 0)
    withdrawals = -sum(t.amount for t in CHEQUING if t.amount < 0)
    for i, (label, val) in enumerate([
        ("Your opening balance on September 1, 2026", f"${_money(OPENING)}"),
        ("Total deposits into your account", f"+ {_money(deposits)}"),
        ("Total withdrawals from your account", f"- {_money(withdrawals)}"),
        ("Your closing balance on September 30, 2026", f"= ${_money(balances[-1])}"),
    ]):
        _txt(c, 40, 690 - i * 13, label); _txt(c, 330, 690 - i * 13, val, right=True)

    y = 620
    header(y); y -= 18
    _txt(c, 40, y, "01 Sep"); _txt(c, 95, y, "Opening Balance"); _txt(c, 570, y, _money(OPENING), right=True); y -= 15
    last_day, page_rows, page = None, 0, 1
    for t, bal in zip(CHEQUING, balances):
        if page_rows == rows_per_page:
            _txt(c, 300, 30, f"{page} of 2", 7); c.showPage(); page += 1
            y = 740; header(y); y -= 18; page_rows = 0; last_day = None
        if t.day != last_day:
            _txt(c, 40, y, t.day.strftime("%d %b"))
            last_day = t.day
        _txt(c, 95, y, t.lines[0])
        _txt(c, 400 if t.amount < 0 else 485, y, _money(t.amount), right=True)
        _txt(c, 570, y, _money(bal) if bal >= 0 else f"-{_money(bal)}", right=True)
        for extra in t.lines[1:]:
            y -= 11; _txt(c, 95, y, extra)
        y -= 16; page_rows += 1
    _txt(c, 40, y, "30 Sep"); _txt(c, 95, y, "Closing Balance"); _txt(c, 570, y, _money(balances[-1]), right=True)
    _txt(c, 300, 30, f"{page} of 2", 7)
    return Statement("rbc-chequing.pdf", "pdf", "RBC chequing",
                     "date once per day, 2-line descriptions, summary box, 2 pages",
                     [(t.day, t.amount) for t in CHEQUING], _finish(c, buf))


def td_chequing_pdf():
    """TD style: DESCRIPTION | WITHDRAWALS | DEPOSITS | DATE | BALANCE with
    dates like SEP01 printed after the amounts."""
    c, buf = _new_canvas()
    balances = _running(CHEQUING, OPENING)
    _txt(c, 40, 750, "TD Canada Trust", 13, bold=True)
    _txt(c, 40, 734, "STATEMENT OF ACCOUNT   AUG 31/26 - SEP 30/26", 9)
    y = 690
    for x, label, right in ((40, "DESCRIPTION", False), (330, "WITHDRAWALS", True), (420, "DEPOSITS", True),
                            (470, "DATE", False), (570, "BALANCE", True)):
        _txt(c, x, y, label, 8, bold=True, right=right)
    y -= 16
    _txt(c, 40, y, "BALANCE FORWARD"); _txt(c, 470, y, "AUG31"); _txt(c, 570, y, _money(OPENING), right=True); y -= 14
    for t, bal in zip(CHEQUING, balances):
        desc = " ".join(t.lines)[:34].upper()
        _txt(c, 40, y, desc)
        _txt(c, 330 if t.amount < 0 else 420, y, _money(t.amount), right=True)
        _txt(c, 470, y, t.day.strftime("%b%d").upper())
        _txt(c, 570, y, _money(bal) + ("OD" if bal < 0 else ""), right=True)
        y -= 14
    _txt(c, 40, y - 6, "CLOSING BALANCE"); _txt(c, 570, y - 6, _money(balances[-1]), right=True)
    return Statement("td-chequing.pdf", "pdf", "TD chequing",
                     "date column after the amounts (SEP01), OD balances, single-line rows",
                     [(t.day, t.amount) for t in CHEQUING], _finish(c, buf))


def simplii_chequing_pdf():
    """Simplii/CIBC style: Date | Description | Withdrawals | Deposits | Balance
    with the date only on the first row of each day (the user's layout)."""
    c, buf = _new_canvas()
    balances = _running(CHEQUING, OPENING)
    _txt(c, 40, 750, "Simplii Financial", 13, bold=True)
    _txt(c, 40, 734, "Account statement  September 1, 2026 to September 30, 2026", 9)
    y = 700
    _txt(c, 40, y, "Date", 10, bold=True); _txt(c, 110, y, "Description", 10, bold=True)
    _txt(c, 380, y, "Withdrawals ($)", 10, bold=True, right=True)
    _txt(c, 480, y, "Deposits ($)", 10, bold=True, right=True)
    _txt(c, 572, y, "Balance ($)", 10, bold=True, right=True)
    y -= 18
    _txt(c, 40, y, "Sep 1", 10, bold=True); _txt(c, 110, y, "Opening balance", 10, bold=True)
    _txt(c, 572, y, f"${_money(OPENING)}", 10, bold=True, right=True); y -= 18
    last = None
    for t, bal in zip(CHEQUING, balances):
        if y < 80:
            c.showPage(); y = 740
            _txt(c, 40, y, "Date", 10, bold=True); _txt(c, 110, y, "Description", 10, bold=True)
            _txt(c, 380, y, "Withdrawals ($)", 10, bold=True, right=True)
            _txt(c, 480, y, "Deposits ($)", 10, bold=True, right=True)
            _txt(c, 572, y, "Balance ($)", 10, bold=True, right=True); y -= 18; last = None
        if t.day != last:
            _txt(c, 40, y, f"{t.day:%b} {t.day.day}", 10); last = t.day
        _txt(c, 110, y, t.lines[0], 10)
        _txt(c, 380 if t.amount < 0 else 480, y, _money(t.amount), 10, right=True)
        _txt(c, 572, y, _money(bal) if bal >= 0 else f"-{_money(bal)}", 10, right=True)
        for extra in t.lines[1:]:
            y -= 13; _txt(c, 110, y, extra, 10)
        y -= 19
    _txt(c, 40, y, "Sep 30", 10, bold=True); _txt(c, 110, y, "Closing balance", 10, bold=True)
    _txt(c, 572, y, f"${_money(balances[-1])}", 10, bold=True, right=True)
    return Statement("simplii-chequing.pdf", "pdf", "Simplii chequing",
                     "your layout: date once per day, 2-line descriptions, negative balances",
                     [(t.day, t.amount) for t in CHEQUING], _finish(c, buf))


# ─────────────────────────────────────────────
# Credit-card statements (amount > 0 on the statement = a charge)
# ─────────────────────────────────────────────

CARD = [
    Txn(date(2026, 9, 18), D("-4.50"), ["TIM HORTONS #1234 TORONTO ON"], "Restaurants"),
    Txn(date(2026, 9, 20), D("-25.99"), ["AMAZON.CA AMAZON.CA ON"], "Retail and Grocery"),
    Txn(date(2026, 9, 24), D("-16.56"), ["NETFLIX.COM 866-579-7172 CA"], "Personal and Household", "Foreign Currency 12.00 USD @ 1.380000"),
    Txn(date(2026, 9, 27), D("15.00"), ["AMAZON.CA REFUND"], "Retail and Grocery"),
    Txn(date(2026, 10, 1), D("-63.10"), ["LOBLAWS #1043 TORONTO ON"], "Retail and Grocery"),
    Txn(date(2026, 10, 3), D("-3.35"), ["PRESTO FARE/3K8XXXXX TORONTO ON"], "Transportation"),
    Txn(date(2026, 10, 9), D("-64.03"), ["PURCHASE INTEREST"], ""),
]
CARD_PAYMENT = Txn(date(2026, 10, 2), D("500.00"), ["PAYMENT THANK YOU/PAIEMENT MERCI"])


def cibc_visa_pdf():
    """CIBC style: account summary box beside the tables, a separate
    "Your payments" table with payments shown as positive numbers, a
    Spend Categories column, a foreign-currency note line, totals."""
    c, buf = _new_canvas()
    _txt(c, 40, 750, "CIBC Dividend Visa Infinite Card", 13, bold=True)
    _txt(c, 40, 734, "Statement Date October 14, 2026   Statement period September 15, 2026 to October 14, 2026", 8)
    # Summary box on the right
    bx = 400
    _txt(c, bx, 700, "Your account at a glance", 9, bold=True)
    charges = -sum(t.amount for t in CARD if t.amount < 0)
    credits = sum(t.amount for t in CARD if t.amount > 0)
    for i, (label, val) in enumerate([
        ("Previous balance", "$3,494.00"), ("Payments", "-$500.00"), ("Other credits", f"-${_money(credits)}"),
        ("Purchases", f"${_money(charges)}"), ("Interest", "$64.03"), ("Total balance", "$3,170.48"),
        ("Minimum payment", "$70.00"), ("Payment due date", "Nov 8, 2026"),
        ("Credit limit", "$3,500.00"), ("Available credit", "$329.52"),
    ]):
        _txt(c, bx, 684 - i * 12, label, 8); _txt(c, 572, 684 - i * 12, val, 8, right=True)

    def table_header(y, with_category):
        _txt(c, 40, y, "Trans", 8, bold=True); _txt(c, 40, y - 9, "date", 8, bold=True)
        _txt(c, 80, y, "Post", 8, bold=True); _txt(c, 80, y - 9, "date", 8, bold=True)
        _txt(c, 120, y - 9, "Description", 8, bold=True)
        if with_category:
            _txt(c, 255, y - 9, "Spend Categories", 8, bold=True)
        _txt(c, 380, y - 9, "Amount($)", 8, bold=True, right=True)
        c.line(40, y - 13, 382, y - 13)

    y = 690
    _txt(c, 40, y, "Your payments", 10, bold=True); y -= 18
    table_header(y, False); y -= 26
    _txt(c, 40, y, CARD_PAYMENT.day.strftime("%b %d")); _txt(c, 80, y, CARD_PAYMENT.day.strftime("%b %d"))
    _txt(c, 120, y, CARD_PAYMENT.lines[0]); _txt(c, 380, y, _money(CARD_PAYMENT.amount), right=True); y -= 14
    _txt(c, 120, y, "Total payments", bold=True); _txt(c, 380, y, "$500.00", bold=True, right=True); y -= 30

    _txt(c, 40, y, "Your new charges and credits", 10, bold=True); y -= 18
    table_header(y, True); y -= 26
    for t in CARD:
        _txt(c, 40, y, t.day.strftime("%b %d")); _txt(c, 80, y, t.day.strftime("%b %d"))
        _txt(c, 120, y, t.lines[0][:24]); _txt(c, 255, y, t.category, 7.5)
        shown = -t.amount  # card statements show charges positive, credits negative
        _txt(c, 380, y, ("-" if shown < 0 else "") + _money(shown), right=True)
        if t.extra:
            y -= 11; _txt(c, 120, y, t.extra, 7.5)
        y -= 14
    _txt(c, 120, y, "Total for 4500 XXXX XXXX 1234", bold=True); _txt(c, 380, y, f"${_money(charges - credits)}", bold=True, right=True)
    y -= 40
    _txt(c, 40, y, "If you make only the minimum payment each month, it will take approximately 12 years and 4 months", 7.5)
    _txt(c, 40, y - 11, "to pay off your balance. Estimated total cost $9,876.00", 7.5)
    expected = [(CARD_PAYMENT.day, CARD_PAYMENT.amount)] + [(t.day, t.amount) for t in CARD]
    return Statement("cibc-visa.pdf", "pdf", "CIBC Visa",
                     "summary box beside tables, separate payments table (positive amounts), "
                     "spend-category column, foreign-currency line, totals, minimum-payment warning",
                     expected, _finish(c, buf))


def scotia_visa_pdf():
    """Scotiabank style: REF.# first, credits with a trailing minus."""
    c, buf = _new_canvas()
    _txt(c, 40, 750, "Scotiabank Scene+ Visa", 13, bold=True)
    _txt(c, 40, 734, "Statement period Sep 15, 2026 - Oct 14, 2026", 8)
    _txt(c, 40, 712, "Previous balance $4,958.29   New balance $4,606.08   Minimum payment due $70.00", 8)
    y = 680
    for x, label, right in ((40, "REF.#", False), (80, "TRANS. DATE", False), (150, "POST DATE", False),
                            (210, "DETAILS", False), (520, "AMOUNT($)", True)):
        _txt(c, x, y, label, 8, bold=True, right=right)
    y -= 16
    rows = [CARD_PAYMENT] + CARD
    for i, t in enumerate(sorted(rows, key=lambda t: t.day), start=1):
        _txt(c, 40, y, f"{i:03d}")
        _txt(c, 80, y, t.day.strftime("%b %d")); _txt(c, 150, y, t.day.strftime("%b %d"))
        _txt(c, 210, y, t.lines[0][:40])
        shown = -t.amount
        _txt(c, 520, y, _money(shown) + ("-" if shown < 0 else ""), right=True)
        y -= 14
    _txt(c, 210, y - 6, "SUB-TOTAL CREDITS", bold=True); _txt(c, 520, y - 6, "515.00-", bold=True, right=True)
    _txt(c, 210, y - 20, "SUB-TOTAL DEBITS", bold=True); _txt(c, 520, y - 20, "177.53", bold=True, right=True)
    expected = sorted([(CARD_PAYMENT.day, CARD_PAYMENT.amount)] + [(t.day, t.amount) for t in CARD])
    return Statement("scotia-visa.pdf", "pdf", "Scotiabank Visa",
                     "REF.# column before the dates, credits with trailing minus, sub-totals",
                     expected, _finish(c, buf))


def mbna_pdf():
    """MBNA style: numeric MM/DD dates and a long reference-number column."""
    c, buf = _new_canvas()
    _txt(c, 40, 750, "MBNA Rewards Mastercard", 13, bold=True)
    _txt(c, 40, 734, "Statement Closing Date: 10/14/2026", 8)
    y = 700
    for x, label, right in ((40, "Transaction Date", False), (115, "Posting Date", False), (180, "Description", False),
                            (360, "Reference Number", False), (570, "Amount ($)", True)):
        _txt(c, x, y, label, 8, bold=True, right=right)
    y -= 16
    rows = sorted([CARD_PAYMENT] + CARD, key=lambda t: t.day)
    for i, t in enumerate(rows):
        _txt(c, 40, y, t.day.strftime("%m/%d")); _txt(c, 115, y, t.day.strftime("%m/%d"))
        _txt(c, 180, y, t.lines[0][:30]); _txt(c, 360, y, f"5550{i:019d}")
        _txt(c, 570, y, ("-" if t.amount > 0 else "") + _money(t.amount), right=True)
        y -= 14
    _txt(c, 180, y - 8, "Total New Balance", bold=True); _txt(c, 570, y - 8, "$4,606.08", bold=True, right=True)
    return Statement("mbna.pdf", "pdf", "MBNA Mastercard",
                     "MM/DD dates, reference-number column, payments negative",
                     sorted([(t.day, t.amount) for t in rows]), _finish(c, buf))


def amex_pdf():
    """Amex style: single date column, credits negative, a second page with
    no repeated table header."""
    c, buf = _new_canvas()
    _txt(c, 40, 750, "American Express Cobalt Card", 13, bold=True)
    _txt(c, 40, 734, "Statement period: September 15, 2026 to October 14, 2026", 8)
    y = 700
    _txt(c, 40, y, "Date", 8, bold=True); _txt(c, 120, y, "Description", 8, bold=True)
    _txt(c, 560, y, "Amount", 8, bold=True, right=True)
    y -= 16
    rows = sorted([CARD_PAYMENT] + CARD, key=lambda t: t.day)
    for i, t in enumerate(rows):
        if i == 5:
            c.showPage(); y = 740   # continues on page 2 without a header
        _txt(c, 40, y, t.day.strftime("%b %d").replace(" 0", " ")); _txt(c, 120, y, t.lines[0])
        _txt(c, 560, y, ("-" if t.amount > 0 else "") + "$" + _money(t.amount), right=True)
        y -= 14
    return Statement("amex.pdf", "pdf", "Amex",
                     "dollar signs on amounts, page 2 continues without a header",
                     [(t.day, t.amount) for t in rows], _finish(c, buf))


# ─────────────────────────────────────────────
# CSV exports
# ─────────────────────────────────────────────

def _csv(name, bank, notes, text, expected):
    return Statement(name, "csv", bank, notes, expected, text.encode("utf-8"))


def csv_statements():
    exp_chq = [(t.day, t.amount) for t in CHEQUING]
    card_rows = sorted([CARD_PAYMENT] + CARD, key=lambda t: t.day)
    exp_card = [(t.day, t.amount) for t in card_rows]
    out = []

    rbc = ['"Account Type","Account Number","Transaction Date","Cheque Number","Description 1","Description 2","CAD$","USD$"']
    for t in CHEQUING:
        d2 = t.lines[1] if len(t.lines) > 1 else ""
        rbc.append(f'Chequing,01234-5678901,{t.day.month}/{t.day.day}/{t.day.year},,"{t.lines[0]}","{d2}",{t.amount},')
    out.append(_csv("rbc.csv", "RBC", "header row, two description columns", "\n".join(rbc) + "\n", exp_chq))

    cibc = [f'{t.day.isoformat()},"{" ".join(t.lines)}",{abs(t.amount) if t.amount < 0 else ""},{t.amount if t.amount > 0 else ""}' for t in CHEQUING]
    out.append(_csv("cibc.csv", "CIBC", "no header, separate debit/credit columns", "\n".join(cibc) + "\n", exp_chq))

    td = []
    bal = OPENING
    for t in CHEQUING:
        bal += t.amount
        td.append(f'{t.day:%m/%d/%Y},"{" ".join(t.lines)}",{abs(t.amount) if t.amount < 0 else ""},{t.amount if t.amount > 0 else ""},{bal}')
    out.append(_csv("td.csv", "TD", "no header, debit/credit plus running balance", "\n".join(td) + "\n", exp_chq))

    sc = ["Filter,Date,Description,Sub-description,Status,Type of Transaction,Amount"]
    for t in CHEQUING:
        sc.append(f',{t.day.isoformat()},{t.lines[0]},{t.lines[1] if len(t.lines) > 1 else ""},Posted,{"Debit" if t.amount < 0 else "Credit"},{t.amount}')
    out.append(_csv("scotia.csv", "Scotiabank", "header with Filter/Status columns", "\n".join(sc) + "\n", exp_chq))

    simplii = ["Date, Transaction Details, Funds Out, Funds In"]
    for t in CHEQUING:
        simplii.append(f'{t.day:%m/%d/%Y}, "{" ".join(t.lines)}", {abs(t.amount) if t.amount < 0 else ""}, {t.amount if t.amount > 0 else ""}')
    out.append(_csv("simplii.csv", "Simplii", "spaces after commas, Funds Out/Funds In", "\n".join(simplii) + "\n", exp_chq))

    tang = ["Transaction date,Transaction,Name,Memo,Amount"]
    for t in CHEQUING:
        tang.append(f'{t.day:%m/%d/%Y},{"DEBIT" if t.amount < 0 else "CREDIT"},{t.lines[0]},"{" ".join(t.lines[1:])}",{t.amount}')
    out.append(_csv("tangerine.csv", "Tangerine", "Name + Memo columns", "\n".join(tang) + "\n", exp_chq))

    mbna = ["Posted Date,Payee,Address,Amount"]
    for t in card_rows:
        mbna.append(f'{t.day:%m/%d/%Y},"{t.lines[0]}",TORONTO ON,{t.amount}')
    out.append(_csv("mbna.csv", "MBNA", "charges negative, payment positive", "\n".join(mbna) + "\n", exp_card))

    amex = ["Date,Date Processed,Description,Cardmember,Amount"]
    for t in card_rows:
        amex.append(f'{t.day:%d %b %Y},{t.day:%d %b %Y},"{t.lines[0]}",SARATH P,{-t.amount}')
    out.append(_csv("amex.csv", "Amex", "charges positive (needs flip), day-first text dates", "\n".join(amex) + "\n", exp_card))

    edge = ["Account: Chequing 01234", "Statement period: 2026-09-01 to 2026-09-30", "",
            "Date,Description,Withdrawals,Deposits,Balance"]
    bal = OPENING
    for t in CHEQUING:
        bal += t.amount
        w = f'"${abs(t.amount):,.2f}"' if t.amount < 0 else ""
        dep = f'"${t.amount:,.2f}"' if t.amount > 0 else ""
        edge.append(f'{t.day.isoformat()},"{", ".join(t.lines)}",{w},{dep},"{bal:,.2f}"')
    edge += ["", "Total,,\"$4,181.92\",\"$3,222.97\","]
    out.append(_csv("preamble-quoted.csv", "Generic", "preamble lines, quoted $1,234.56 amounts, commas in descriptions, totals row",
                    "\n".join(edge) + "\n", exp_chq))
    return out


def cibc_visa_2026_pdf():
    """CIBC's 2026 card layout (from real statements, all values made up):
    "Your payments", a "Your interest" table with an annual-rate column and
    an interest reversal printed out of line with the amount column, a
    card-number line under the charges header, "Total for ..." starting in
    the date column, a fine-print page whose sentences contain heading words,
    and a Spend Report table with no dates."""
    c, buf = _new_canvas()
    _txt(c, 40, 750, "CIBC Dividend Visa Card", 13, bold=True)
    _txt(c, 40, 734, "Prepared for: MR ALEX SAMPLE   May 16 to June 15, 2026   Account number: 4505 XXXX XXXX 0042", 8)
    bx = 400
    for i, (label, val) in enumerate([("Previous balance", "$3,471.83"), ("Total credits", "- $280.18"),
                                      ("Total charges", "+ $162.25"), ("Total balance", "= $3,353.90"),
                                      ("Minimum payment", "$110.27"), ("Payment due date", "Jul 8, 2026")]):
        _txt(c, bx, 700 - i * 12, label, 8); _txt(c, 572, 700 - i * 12, val, 8, right=True)
    c.showPage()

    def header(y, extra=None):
        _txt(c, 36, y, "Trans", 8, bold=True); _txt(c, 77, y, "Post", 8, bold=True)
        _txt(c, 36, y - 8, "date", 8, bold=True); _txt(c, 77, y - 8, "date", 8, bold=True)
        _txt(c, 119, y - 8, "Description", 8, bold=True)
        if extra:
            _txt(c, 330, y - 8, extra, 8, bold=True)
        _txt(c, 540, y - 8, "Amount($)", 8, bold=True, right=True)

    def row(y, d1, d2, desc, amount, middle=None, amount_x=540):
        _txt(c, 36, y, d1); _txt(c, 77, y, d2); _txt(c, 119, y, desc)
        if middle:
            _txt(c, 330, y, middle, 7.5)
        _txt(c, amount_x, y, amount, right=True)

    y = 760
    _txt(c, 36, y, "Transactions", 11, bold=True); _txt(c, 147, y - 6, "from May 16 to June 15, 2026", 8); y -= 22
    _txt(c, 36, y, "Your payments", 10, bold=True); y -= 16
    header(y); y -= 20
    payments = [(date(2026, 5, 22), "May 22", "May 25", "130.00"), (date(2026, 5, 29), "May 29", "Jun 01", "150.00")]
    for _, d1, d2, amt in payments:
        row(y, d1, d2, "PAYMENT THANK YOU/PAIEMENT MERCI", amt); y -= 12
    _txt(c, 36, y, "Total payments $280.00", bold=True); y -= 18

    _txt(c, 36, y, "Your interest", 10, bold=True); y -= 16
    header(y, "Annual interest rate"); y -= 20
    row(y, "Jun 15", "Jun 15", "REGULAR PURCHASES", "51.05", "21.99%"); y -= 12
    row(y, "Jun 15", "Jun 15", "CASH ADVANCES", "2.94", "22.99%"); y -= 12
    row(y, "Jun 13", "Jun 16", "INTEREST REVERSAL", "-0.18***", amount_x=487); y -= 12   # printed out of line
    _txt(c, 36, y, "Total interest this period $53.99", bold=True); y -= 10
    _txt(c, 36, y, '*** The interest adjustments above are not included in the "Total Interest this period"', 7); y -= 16

    _txt(c, 36, y, "Your new charges and credits", 10, bold=True); y -= 16
    header(y, "Spend Categories"); y -= 18
    _txt(c, 36, y, "Card number 4505 XXXX XXXX 0042"); y -= 14
    charges = [(date(2026, 5, 19), "May 19", "May 20", "COINAMATIC A ON", "Personal and Household Expenses", "10.25"),
               (date(2026, 5, 23), "May 23", "May 25", "Amazon.ca*SAMPLE1 866-216-1072 ON", "Home and Office Improvement", "57.11"),
               (date(2026, 5, 23), "May 23", "May 25", "AMZN Mktp CA*SAMPLE2 866-216-1072 ON", "Personal and Household Expenses", "24.85"),
               (date(2026, 5, 29), "May 29", "Jun 01", "COINAMATIC A ON", "Personal and Household Expenses", "10.25")]
    for _, d1, d2, desc, cat, amt in charges:
        row(y, d1, d2, desc, amt, cat); y -= 13
    _txt(c, 36, y, "Total for 4505 XXXX XXXX 0042 $102.46", bold=True); y -= 30

    # Fine print in two columns; its sentences contain heading words.
    for left, right in [
        ("If you find an error or irregularity (including possible unauthorized or", "Amount Due, your payment due date will be"),
        ("fraudulent Transactions) in this statement you must tell us within 30", "payment due date. month and it includes the"),
        ("If interest is charged on a Purchase, it reflects Transactions that were", "posted as of the Statement Date. If you pay"),
        ("the payment due date. determined under Eastern Standard Time (EST)", "meaning payments are considered received by"),
    ]:
        _txt(c, 14, y, left, 6.5); _txt(c, 300, y, right, 6.5); y -= 7
    c.showPage()

    y = 720
    _txt(c, 72, y, "CIBC CreditSmart Spend Report", 11, bold=True); y -= 20
    for x, label in ((72, "Spend Categories"), (250, "Transactions"), (330, "Amount($)"), (400, "Budget ($)"),
                     (460, "Difference ($)"), (530, "Transactions"), (580, "Amount ($)")):
        _txt(c, x, y, label, 7, bold=True)
    y -= 14
    for cat, n, amt, ytd_n, ytd in [("Personal and Household Expenses", "3", "45.35", "10", "117.10"),
                                    ("Retail and Grocery", "0", "0.00", "8", "436.67"),
                                    ("Home and Office Improvement", "1", "57.11", "1", "57.11")]:
        _txt(c, 96, y, cat, 7); _txt(c, 260, y, n, 7); _txt(c, 360, y, amt, 7, right=True)
        _txt(c, 410, y, "-", 7); _txt(c, 470, y, "-", 7); _txt(c, 540, y, ytd_n, 7); _txt(c, 600, y, ytd, 7, right=True)
        y -= 14
    _txt(c, 96, y, "Total 4 102.46 34 $1,008.64", 7, bold=True); y -= 20
    _txt(c, 72, y, "Reminder: If you only make the minimum payment every month, it will take approximately 27 year(s)", 7)

    expected = [(d, D(a)) for d, _, _, a in payments] + \
        [(date(2026, 6, 15), D("-51.05")), (date(2026, 6, 15), D("-2.94")), (date(2026, 6, 13), D("0.18"))] + \
        [(d, -D(a)) for d, _, _, _, _, a in charges]
    return Statement("cibc-visa-2026.pdf", "pdf", "CIBC Visa (2026 layout)",
                     "interest table with rate column and out-of-line reversal, card-number line under header, "
                     "'Total for' in the date column, fine-print sentences with heading words, spend report with no dates",
                     expected, _finish(c, buf))


def scotia_value_visa_pdf():
    """Scotiabank Value Visa (from real statements, values made up): "REF.#"
    and "DATE" so close they read as one heading, an "Account Balance =
    $..." summary label on the header line, summary lines interleaved with
    the rows, foreign-currency "AMT ... USD" notes, SUB-TOTAL lines, and a
    fine-print page with the spaces missing."""
    c, buf = _new_canvas()
    _txt(c, 378, 770, "StatementPeriod Jun 12, 2026 -Jul 11, 2026", 7)
    _txt(c, 378, 760, "StatementDate Jul 11, 2026", 7)
    _txt(c, 378, 750, "Account# 4535 XXXX XXXX 0099", 7)
    _txt(c, 72, 710, "Scotiabank Value VISA* card", 10, bold=True)
    _txt(c, 378, 560, "Payment Due Date Aug 5, 2026", 8)
    _txt(c, 378, 547, "New Balance = $4,945.94", 8)
    _txt(c, 378, 510, "Previous balance,Jun 11/26 $5,011.36", 7)
    _txt(c, 378, 498, "Payments/credits - $200.00", 7)
    _txt(c, 378, 486, "Purchases/charges + $77.89", 7)
    _txt(c, 71, 448, "Transactions since your last statement", 8, bold=True)
    y = 434
    _txt(c, 103, y, "TRANS. POST", 7, bold=True); y -= 8
    _txt(c, 82, y, "REF.# DATE", 7, bold=True)          # one phrase: the gap is a single space
    _txt(c, 133, y, "DATE", 7, bold=True); _txt(c, 163, y, "DETAILS", 7, bold=True)
    _txt(c, 364, y, "AMOUNT($)", 7, bold=True, right=True)
    _txt(c, 379, y, "Account Balance", 8); _txt(c, 498, y, "=", 8); _txt(c, 572, y, "$4,945.94", 8, right=True)
    y -= 15
    _txt(c, 71, y, "MR ALEX SAMPLE - 4535 XXXX XXXX 0099", 7); y -= 11
    rows = [
        ("001", "Jun 11", "Jun 12", "UPG*PAYMENTICO.COM LIMASSOL", "30.03", "AMT 20.99 USD", date(2026, 6, 11), D("-30.03")),
        ("002", "Jun 12", "Jun 12", "PAYMENT FROM - *****04*0001", "100.00-", None, date(2026, 6, 12), D("100.00")),
        ("003", "Jun 12", "Jun 12", "PAYMENT FROM - *****04*0001", "100.00-", None, date(2026, 6, 12), D("100.00")),
        ("004", "Jul 8", "Jul 9", "JOHN'S MART TORONTO ON", "17.83", None, date(2026, 7, 8), D("-17.83")),
        ("005", "Jul 11", "Jul 11", "INTEREST CHARGES-PURCHASE", "42.69", None, date(2026, 7, 11), D("-42.69")),
        ("006", "Jul 11", "Jul 11", "INTEREST CHARGES-CASH", "14.00", None, date(2026, 7, 11), D("-14.00")),
    ]
    for i, (ref, d1, d2, desc, amt, note, _, _) in enumerate(rows):
        _txt(c, 82, y, ref, 7); _txt(c, 98, y, d1, 7); _txt(c, 133, y, d2, 7); _txt(c, 163, y, desc, 7)
        _txt(c, 364, y, amt, 7, right=True)
        if i == 1:
            _txt(c, 378, y, "Creditlimit $5,000.00", 7)   # summary box on the same line
        y -= 9
        if note:
            _txt(c, 162, y, note, 7); y -= 9
    _txt(c, 82, y, "SUB-TOTAL CREDITS-4535 XXXX XXXX 0099", 7); _txt(c, 367, y, "$200.00-", 7, right=True); y -= 9
    _txt(c, 82, y, "SUB-TOTAL DEBITS-4535 XXXX XXXX 0099", 7); _txt(c, 364, y, "$104.55", 7, right=True)
    _txt(c, 378, y, "Estimateofthetimeitwilltaketopaythe", 7)
    c.showPage()
    y = 700
    for line in ["INFORMATIONABOUTYOURSCOTIABANKSTATEMENT",
                 "1.PAYMENTS:TheTOTALMINIMUMPAYMENTonthisstatementincludesyourcurrentminimum",
                 "yourTotalMinimumPaymentbythePAYMENTDUEDATEshownonyourstatement."]:
        _txt(c, 25, y, line, 6.5); y -= 8
    return Statement("scotia-value-visa.pdf", "pdf", "Scotiabank Value Visa",
                     "REF.#+DATE merged heading, summary label with '=' on the header line, interleaved summary box, "
                     "foreign-currency notes, SUB-TOTAL lines, fine print without spaces",
                     [(d, a) for *_, d, a in rows], _finish(c, buf))


def build():
    return [rbc_chequing_pdf(), td_chequing_pdf(), simplii_chequing_pdf(),
            cibc_visa_pdf(), scotia_visa_pdf(), mbna_pdf(), amex_pdf(),
            cibc_visa_2026_pdf(), scotia_value_visa_pdf()] + csv_statements()
