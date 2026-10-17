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


def build():
    return [rbc_chequing_pdf(), td_chequing_pdf(), simplii_chequing_pdf(),
            cibc_visa_pdf(), scotia_visa_pdf(), mbna_pdf(), amex_pdf()] + csv_statements()
