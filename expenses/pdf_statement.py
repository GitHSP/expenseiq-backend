# expenses/pdf_statement.py
#
# Reads transactions out of a text-based PDF bank or credit-card statement.
#
# A PDF only has positioned words, so this works line by line:
#   - a transaction line starts with a date ("Oct 03", "03 Oct",
#     "2026-10-03", "10/03"), often two (transaction + posting date)
#   - money amounts sit at the right; which column each one is under
#     (withdrawals / deposits / amount / balance) comes from the column
#     headers' positions on the page
#   - without usable headers, a running balance tells money in from out
#   - many banks print the date only on a day's first transaction, so a
#     line with amounts but no date (inside the table) reuses the last date
#   - lines without a date or amount continue the previous description
# Statements usually omit the year, so it's taken from the latest full date
# printed on the statement (the period end).

import io
import re
from datetime import date, timedelta
from decimal import Decimal

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"

# Date at the start of a line; captures (month-name, day) in either order,
# ISO dates, and numeric month/day(/year).
DATE_PATTERNS = [
    ("md",  re.compile(rf"^{MONTH_RE}\s*(\d{{1,2}})\b", re.IGNORECASE)),
    ("dm",  re.compile(rf"^(\d{{1,2}})\s*{MONTH_RE}(?![a-z])", re.IGNORECASE)),
    ("iso", re.compile(r"^(\d{4})-(\d{2})-(\d{2})\b")),
    ("num", re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")),
]

FULL_DATE_RE = re.compile(rf"\b{MONTH_RE}\s+(\d{{1,2}}),?\s+(20\d\d)\b", re.IGNORECASE)
ISO_DATE_RE = re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b")

AMOUNT_RE = re.compile(r"^\(?-?\$?\d{1,3}(?:,\d{3})*(?:\.\d{2})\)?(?:CR|DR|-)?$|^\(?-?\$?\d+\.\d{2}\)?(?:CR|DR|-)?$", re.IGNORECASE)

# Statement summary wording — these figures are never transactions.
SKIP_LINE_RE = re.compile(
    r"opening balance|closing balance|balance forward|previous balance|new balance|"
    r"statement balance|total balance|balance due|amount due|past due|"
    r"minimum payment|min\.? payment|minimum (amount )?due|payment due|"
    r"credit limit|available credit|credit available|over ?limit|"
    r"purchases? ?(&|and) ?(other )?charges|payments? ?(&|and) ?(other )?credits|cash advances?|"
    r"fees? charged|interest charged|interest rate|annual interest|"
    r"rewards? (earned|balance|summary)|points (earned|balance)|cash ?back (earned|balance)|"
    r"estimated time|if you (make|pay) only|years? to pay|account summary|"
    r"page \d|statement period|statement date|account number",
    re.IGNORECASE,
)

# A line starting with one of these closes the transaction table; anything
# after it (summaries, interest tables, messages) is ignored until the next
# table header.
TABLE_END_RE = re.compile(
    r"^(total|sub-?total|closing balance|new balance|statement balance|ending balance)\b",
    re.IGNORECASE,
)

HEADER_WORDS = {
    "out":     re.compile(r"withdraw|debit|charges?$|purchases?$|cheques?$|paid out|funds out", re.IGNORECASE),
    "in":      re.compile(r"deposit|credit|payments?$|paid in|funds in", re.IGNORECASE),
    "balance": re.compile(r"balance", re.IGNORECASE),
    "amount":  re.compile(r"^amount|^\$$|^cad", re.IGNORECASE),
}


class PdfStatementError(ValueError):
    pass


def _parse_money(token):
    t = token.strip()
    negative = False
    if t.startswith("(") and t.endswith(")"):
        negative, t = True, t[1:-1]
    upper = t.upper()
    if upper.endswith("CR"):
        negative, t = True, t[:-2]          # credit on a card statement = money in
    elif upper.endswith("DR"):
        t = t[:-2]
    elif t.endswith("-"):
        negative, t = True, t[:-1]
    t = t.replace("$", "").replace(",", "")
    if t.startswith("-"):
        negative, t = not negative, t[1:]
    try:
        value = Decimal(t)
    except Exception:
        return None
    return -value if negative else value


def _statement_end(text):
    """Latest full date printed anywhere on the statement."""
    found = []
    for m in FULL_DATE_RE.finditer(text):
        try:
            found.append(date(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2))))
        except ValueError:
            pass
    for m in ISO_DATE_RE.finditer(text):
        try:
            found.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    return max(found) if found else None


def _leading_date(text, end):
    """(date, rest_of_text) if the text starts with a date, else (None, text)."""
    for kind, pattern in DATE_PATTERNS:
        m = pattern.match(text)
        if not m:
            continue
        year = None
        try:
            if kind == "md":
                month, day = MONTHS[m.group(1)[:3].lower()], int(m.group(2))
            elif kind == "dm":
                day, month = int(m.group(1)), MONTHS[m.group(2)[:3].lower()]
            elif kind == "iso":
                year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                month, day = int(m.group(1)), int(m.group(2))
                if m.group(3):
                    year = int(m.group(3))
                    year += 2000 if year < 100 else 0
            if year is None:
                ref = end or date.today()
                year = ref.year
                d = date(year, month, day)
                # A December charge on a January statement belongs to last year.
                if d > ref + timedelta(days=7):
                    d = date(year - 1, month, day)
            else:
                d = date(year, month, day)
        except (ValueError, KeyError):
            return None, text
        return d, text[m.end():].strip()
    return None, text


def _group_lines(words, tolerance=3):
    lines = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= tolerance:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _find_columns(line):
    """
    If this line is a transaction-table header, return (columns, right_edge):
    column kind -> x-centre, and the x where the table ends (anything further
    right, like a summary box beside the table, isn't part of it).
    """
    if any(AMOUNT_RE.match(w["text"]) for w in line):
        return None  # header rows don't contain money amounts
    columns, right = {}, 0
    for w in line:
        for kind, pattern in HEADER_WORDS.items():
            if kind not in columns and pattern.search(w["text"]):
                columns[kind] = (w["x0"] + w["x1"]) / 2
                right = max(right, w["x1"])
                break
    has_money_col = any(k in columns for k in ("out", "in", "amount"))
    has_date = any(re.search(r"date", w["text"], re.IGNORECASE) for w in line)
    if not (has_money_col and (has_date or len(columns) >= 2)):
        return None
    # Money is right-aligned and can stick out past its header a little.
    return columns, right + 45


def parse_pdf(raw):
    try:
        import pdfplumber
    except ImportError:  # pragma: no cover - deployment problem, not user error
        raise PdfStatementError("PDF import isn't available on the server yet.")

    try:
        pdf = pdfplumber.open(io.BytesIO(raw))
    except Exception:
        raise PdfStatementError("Couldn't open that PDF. If it's password-protected, remove the password or download CSV instead.")

    with pdf:
        pages = [p.extract_words(keep_blank_chars=False, use_text_flow=False) for p in pdf.pages[:50]]

    all_text = " ".join(w["text"] for page in pages for w in page)
    if not all_text.strip():
        raise PdfStatementError("This PDF has no readable text (it's probably a scanned image). Please download a CSV instead.")
    end = _statement_end(all_text)

    rows = []
    columns = None        # set by a table header: column kind -> x-centre
    table_right = None    # words further right than this are outside the table
    in_table = False
    prev_balance = None
    last_date = None
    for words in pages:
        for line in _group_lines(words):
            header = _find_columns(line)
            if header:
                columns, table_right = header
                in_table = True
                continue

            if columns:
                # Drop anything beside the table (e.g. an account summary box).
                line = [w for w in line if w["x0"] <= table_right]
                if not line:
                    continue

            text = " ".join(w["text"] for w in line)
            d, rest = _leading_date(text, end)
            if d:
                # Skip a second (posting) date.
                _, rest = _leading_date(rest, end)
            amounts = [w for w in line if AMOUNT_RE.match(w["text"])]

            # Totals never start with a date; a dated "TOTAL WINE" is a purchase.
            if not d and TABLE_END_RE.search(text):
                if amounts and re.search(r"balance", text, re.IGNORECASE):
                    prev_balance = _parse_money(amounts[-1]["text"])
                in_table = False
                continue
            if SKIP_LINE_RE.search(rest if d else text):
                if amounts and re.search(r"balance", text, re.IGNORECASE):
                    prev_balance = _parse_money(amounts[-1]["text"])
                continue
            if columns and not in_table:
                continue  # between tables: summaries, notices, interest tables

            if d:
                last_date = d
            elif amounts and last_date and in_table:
                # Same day as the transaction above — the bank only printed
                # the date once.
                d, rest = last_date, text
            else:
                # Wrapped description: no date, no amounts.
                if rows and not amounts and rows[-1]["_open"]:
                    rows[-1]["description"] = f'{rows[-1]["description"]} {text}'.strip()[:200]
                    rows[-1]["_open"] = False
                continue
            if not amounts:
                continue

            description = " ".join(
                w["text"] for w in line if w not in amounts
            )
            _, description = _leading_date(description, end)
            _, description = _leading_date(description, end)

            amount = _line_amount(amounts, columns, prev_balance)
            balance = _balance_of(amounts, columns)
            if amount is not None and balance is not None and prev_balance is not None:
                change = balance - prev_balance
                if abs(abs(change) - abs(amount)) < Decimal("0.01"):
                    amount = change
            if balance is not None:
                prev_balance = balance
            if amount is None or amount == 0:
                continue
            rows.append({"date": d, "description": description.strip() or "Transaction", "amount": amount, "_open": True})

    for r in rows:
        r.pop("_open", None)
    if not rows:
        raise PdfStatementError("Couldn't find any transactions in this PDF. Please download a CSV instead, or let us know your bank so we can support its layout.")
    return rows


def _nearest_column(word, columns):
    centre = (word["x0"] + word["x1"]) / 2
    return min(columns.items(), key=lambda kv: abs(kv[1] - centre))[0]


def _balance_of(amounts, columns):
    if columns:
        if "balance" in columns:
            for w in amounts:
                if _nearest_column(w, columns) == "balance":
                    return _parse_money(w["text"])
        return None
    # No headers: with two numbers on a line, the last is the running balance.
    if len(amounts) >= 2:
        return _parse_money(amounts[-1]["text"])
    return None


def _line_amount(amounts, columns, prev_balance):
    """Signed amount for a transaction line (negative = money out)."""
    if columns:
        for w in amounts:
            kind = _nearest_column(w, columns)
            value = _parse_money(w["text"])
            if value is None:
                continue
            if kind == "out":
                return -abs(value)
            if kind == "in":
                return abs(value)
            if kind == "amount":
                # Single amount column = card statement: charges positive,
                # payments/credits negative or marked CR.
                return -value
        return None

    # No headers: last number is a running balance if there are two.
    values = [_parse_money(w["text"]) for w in amounts]
    values = [v for v in values if v is not None]
    if not values:
        return None
    if len(values) >= 2 and prev_balance is not None:
        change = values[-1] - prev_balance
        if abs(abs(change) - abs(values[-2])) < Decimal("0.01"):
            return change
    return -values[0]
