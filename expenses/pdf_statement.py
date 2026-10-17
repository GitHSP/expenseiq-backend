# expenses/pdf_statement.py
#
# Reads transactions out of a text-based PDF bank or credit-card statement.
#
# A PDF only has positioned words, so this rebuilds the transaction table:
#   - a table starts at a header row ("Date | Description | Withdrawals |
#     Deposits | Balance", "Trans date | Post date | Description | Amount",
#     "REF.# | TRANS. DATE | ... | AMOUNT", "DESCRIPTION | ... | DATE | ...")
#     and each header phrase becomes a column with an x-position
#   - every word in a row is assigned to a column: text columns are
#     left-aligned (date, description, category, reference), money columns
#     right-aligned (withdrawals, deposits, amount, balance)
#   - the date comes from the date column wherever it sits; many banks print
#     it only on a day's first row, so undated rows reuse the last date
#   - rows without a date or amount continue the previous description
#   - which money column an amount is under says money in vs out; a running
#     balance, when present, double-checks it
#   - anything outside the table (summary boxes, notices, totals, interest
#     disclosures) is ignored; a "Your payments" table holds money coming in
# Statements usually omit the year, so it's taken from the latest full date
# printed on the statement (the period end). PDFs without a usable header
# fall back to reading date-led lines.

import io
import re
from datetime import date, timedelta
from decimal import Decimal

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"

# Date at the start of a piece of text; (month-name, day) in either order
# ("Oct 03", "03 Oct", "SEP01"), ISO dates, and numeric month/day(/year).
DATE_PATTERNS = [
    ("md",  re.compile(rf"^{MONTH_RE}\s*(\d{{1,2}})\b", re.IGNORECASE)),
    ("dm",  re.compile(rf"^(\d{{1,2}})\s*{MONTH_RE}(?![a-z])", re.IGNORECASE)),
    ("iso", re.compile(r"^(\d{4})-(\d{2})-(\d{2})\b")),
    ("num", re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")),
]

# Full dates anywhere on the statement, used to work out the year.
FULL_DATE_RES = [
    ("mdy", re.compile(rf"\b{MONTH_RE}\s+(\d{{1,2}}),?\s+(20\d\d)\b", re.IGNORECASE)),   # October 14, 2026
    ("ymd", re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b")),                                  # 2026-10-14
    ("mdy2", re.compile(rf"\b{MONTH_RE}\s+(\d{{1,2}})/(\d{{2}})\b", re.IGNORECASE)),       # SEP 30/26 (TD)
    ("num", re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b")),                               # 10/14/2026
]

AMOUNT_RE = re.compile(
    # Optional trailing footnote stars: CIBC prints "INTEREST REVERSAL -0.27***".
    r"^\(?-?\$?\d{1,3}(?:,\d{3})*\.\d{2}\)?(?:CR|DR|OD|-)?\**$|^\(?-?\$?\d+\.\d{2}\)?(?:CR|DR|OD|-)?\**$",
    re.IGNORECASE,
)

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
    r"page \d|^\d+ of \d+$|statement period|statement date|account number",
    re.IGNORECASE,
)

# Summary wording that can sit on a dated line (e.g. "Sep 1 Opening balance").
# Narrower than SKIP_LINE_RE: a dated row in a table is normally a real
# transaction, e.g. CIBC's interest table lists "Jun 15 CASH ADVANCES 22.99% 4.40".
SKIP_DATED_RE = re.compile(
    r"opening balance|closing balance|balance forward|previous balance|new balance|"
    r"statement balance|minimum payment|payment due",
    re.IGNORECASE,
)
MAX_HEADER_WORDS = 14     # longer lines are sentences in the fine print, not headings
# Payment-slip wording: "NEW BALANCE  MINIMUM PAYMENT  PAYMENT DUE DATE  AMOUNT PAID"
# looks like a heading but heads the tear-off slip, not transactions.
SLIP_HEADER_RE = re.compile(r"new balance|minimum payment|payment due|amount paid|amount due", re.IGNORECASE)
RATE_RE = re.compile(r"\s*\d{1,2}\.\d{1,3}\s*%")

# An undated line starting with one of these closes the transaction table;
# anything after it is ignored until the next table header.
TABLE_END_RE = re.compile(
    r"^(total|sub-?total|closing balance|new balance|statement balance|ending balance)(?![a-z])|"
    r"^(subtotal|newbalance|closingbalance)",
    re.IGNORECASE,
)

# Header phrase -> column kind. Order matters: the first match wins.
COLUMN_KINDS = [
    ("balance",  re.compile(r"balance", re.IGNORECASE)),
    ("date",     re.compile(r"\bdate\b", re.IGNORECASE)),
    ("out",      re.compile(r"withdraw|debits?\b|charges?\b|purchases?\b|cheques?\b|paid out|funds out", re.IGNORECASE)),
    ("in",       re.compile(r"deposit|credits?\b|payments?\b|paid in|funds in", re.IGNORECASE)),
    ("amount",   re.compile(r"amount|^\$$|^\(\$\)$|^cad\b", re.IGNORECASE)),
    ("ref",      re.compile(r"\bref\b|ref\.|reference", re.IGNORECASE)),
    ("category", re.compile(r"categor", re.IGNORECASE)),
    ("desc",     re.compile(r"description|details|transaction|payee|particulars|merchant|activity|name", re.IGNORECASE)),
]
MONEY_KINDS = ("out", "in", "amount", "balance")
MONEY_TOLERANCE = 40     # how far (pt) a number may sit from its column's edge
PHRASE_GAP = 6           # header words closer than this form one phrase

# Notes printed under a transaction that aren't part of its name.
NOTE_LINE_RE = re.compile(r"foreign currency|exchange rate|@\s*\d+\.\d+|conversion", re.IGNORECASE)

# A table titled like this lists money coming in (e.g. CIBC "Your payments").
CREDIT_SECTION_RE = re.compile(r"payments|credits|refunds", re.IGNORECASE)
DEBIT_SECTION_RE = re.compile(r"charges|purchases|debits", re.IGNORECASE)


def _glue_tolerant(pattern):
    """Same pattern, but a space may be missing ("Previousstatementbalance")."""
    return re.compile(pattern.pattern.replace(" ", r"\s?"), pattern.flags)


SKIP_LINE_RE = _glue_tolerant(SKIP_LINE_RE)
SKIP_DATED_RE = _glue_tolerant(SKIP_DATED_RE)
SLIP_HEADER_RE = _glue_tolerant(SLIP_HEADER_RE)


class PdfStatementError(ValueError):
    pass


def _parse_money(token):
    t = token.strip().rstrip("*")
    negative = False
    if t.startswith("(") and t.endswith(")"):
        negative, t = True, t[1:-1]
    upper = t.upper()
    if upper.endswith("CR"):
        negative, t = True, t[:-2]          # credit on a card statement = money in
    elif upper.endswith("OD"):
        negative, t = True, t[:-2]          # overdrawn balance
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
    for kind, pattern in FULL_DATE_RES:
        for m in pattern.finditer(text):
            try:
                if kind == "mdy":
                    found.append(date(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2))))
                elif kind == "ymd":
                    found.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
                elif kind == "mdy2":
                    found.append(date(2000 + int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2))))
                else:
                    found.append(date(int(m.group(3)), int(m.group(1)), int(m.group(2))))
            except (ValueError, KeyError):
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


def _is_money(word):
    return bool(AMOUNT_RE.match(word["text"]))


def _phrases(words):
    """Group words that sit close together into phrases (x0, x1, text)."""
    phrases = []
    for w in words:
        if phrases and w["x0"] - phrases[-1]["x1"] < PHRASE_GAP and not _is_money(w):
            p = phrases[-1]
            p["x1"], p["text"] = w["x1"], f'{p["text"]} {w["text"]}'
        else:
            phrases.append({"x0": w["x0"], "x1": w["x1"], "text": w["text"], "money": _is_money(w)})
    return phrases


class Table:
    """Column layout of one transaction table, built from its header row."""

    def __init__(self, columns, credit_section, interest=False):
        self.columns = columns                      # [{"kind", "x0", "x1"}] left to right
        self.credit_section = credit_section
        self.interest = interest                    # rows are interest charged (CIBC "Your interest")
        self.right = max(c["x1"] for c in columns) + 12
        self.left = min(c["x0"] for c in columns) - 12
        self.text_cols = [c for c in columns if c["kind"] not in MONEY_KINDS]
        self.money_cols = [c for c in columns if c["kind"] in MONEY_KINDS]

    @classmethod
    def from_header(cls, line, section_title):
        if len(line) > MAX_HEADER_WORDS:
            return None
        # A label immediately followed by an amount is a "label: value" pair
        # from a summary box sharing the row (e.g. "Payments -$500.00"), not a
        # column heading.
        phrases = _phrases(line)
        heads = []
        for i, p in enumerate(phrases):
            if p["money"]:
                continue
            nxt = i + 1
            while nxt < len(phrases) and phrases[nxt]["text"] in ("=", ":", "+", "-"):
                nxt += 1                              # "Account Balance = $2,318.27"
            if nxt < len(phrases) and phrases[nxt]["money"]:
                continue
            heads.append(p)
        columns = []
        for p in heads:
            kind = next((k for k, rx in COLUMN_KINDS if rx.search(p["text"])), None)
            if kind:
                columns.append({"kind": kind, "x0": p["x0"], "x1": p["x1"], "text": p["text"]})
        kinds = [c["kind"] for c in columns]
        if not any(k in kinds for k in ("out", "in", "amount")):
            return None
        if "date" not in kinds:
            return None
        money_right = max(c["x1"] for c in columns if c["kind"] in MONEY_KINDS) + 12
        if SLIP_HEADER_RE.search(" ".join(w["text"] for w in line if w["x0"] <= money_right)):
            return None
        # Summary boxes and reports (e.g. a spend-by-category chart) have no
        # date column, so a real header needs a money column and a date
        # column. Keep only the first date column (transaction date, not
        # posting date).
        seen_date = False
        for c in columns:
            if c["kind"] == "date":
                if seen_date:
                    c["kind"] = "date2"
                seen_date = True
        credit = bool(section_title) and bool(CREDIT_SECTION_RE.search(section_title)) \
            and not DEBIT_SECTION_RE.search(section_title)
        interest = any(re.search(r"interest", p["text"], re.IGNORECASE) for p in heads)
        return cls(columns, credit, interest)

    def split(self, line):
        """Return (cells, money) for a row: text per text column, and
        [(kind, word)] for amounts under money columns."""
        cells, money, stray = {}, [], []
        self.stray_used = False
        for w in line:
            if w["x0"] > self.right:
                continue  # beside the table (e.g. a summary box)
            if w["x1"] < self.left:
                continue  # printer marks in the margin (MBNA's "-IRH")
            if _is_money(w):
                col = min(self.money_cols, key=lambda c: min(abs(w["x1"] - c["x1"]),
                                                             abs((w["x0"] + w["x1"]) / 2 - (c["x0"] + c["x1"]) / 2)))
                if min(abs(w["x1"] - col["x1"]), abs((w["x0"] + w["x1"]) / 2 - (col["x0"] + col["x1"]) / 2)) <= MONEY_TOLERANCE:
                    money.append((col["kind"], w))
                    continue
                stray.append(w)
            # Text is left-aligned: it belongs to the last text column that
            # starts at or before it.
            col = None
            for c in self.text_cols:
                if c["x0"] <= w["x0"] + 6:
                    col = c
            kind = col["kind"] if col else "desc"
            if w in stray:
                continue
            cells[kind] = f'{cells.get(kind, "")} {w["text"]}'.strip()
        # One amount printed out of line with its column (CIBC's
        # "INTEREST REVERSAL -0.27***" sits under the rate column): if it's
        # the row's only amount and right of the description, it's the amount.
        if not money and len(stray) == 1 and self.text_cols and stray[0]["x0"] > max(c["x1"] for c in self.text_cols):
            spendable = [c for c in self.money_cols if c["kind"] != "balance"] or self.money_cols
            col = min(spendable, key=lambda c: abs(stray[0]["x1"] - c["x1"]))
            money.append((col["kind"], stray[0]))
            self.stray_used = True
        else:
            for w in stray:
                cells["desc"] = f'{cells.get("desc", "")} {w["text"]}'.strip()
        return cells, money

    def amount(self, money, prev_balance):
        """(signed amount, balance) for a row. Negative = money out."""
        amount = balance = None
        for kind, w in money:
            value = _parse_money(w["text"])
            if value is None:
                continue
            if kind == "balance":
                balance = value
            elif amount is None:
                if kind == "out":
                    amount = -abs(value)
                elif kind == "in":
                    amount = abs(value)
                elif self.credit_section:
                    amount = abs(value)
                else:
                    # Card statements show charges positive and credits
                    # negative (or with CR / a trailing minus).
                    amount = -value
        if amount is not None and balance is not None and prev_balance is not None:
            change = balance - prev_balance
            if abs(abs(change) - abs(amount)) < Decimal("0.01"):
                amount = change
        return amount, balance


def parse_pdf(raw, details=None):
    """Transaction rows. If `details` is a dict, it also gets the first pages' text
    and the statement end date, for working out the account and period."""
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
    if details is not None:
        details["text"] = " ".join(w["text"] for page in pages[:3] for w in page)
        details["end"] = end

    lines = [line for words in pages for line in _group_lines(words)]
    if any(_find_header(lines, i, "") for i in range(len(lines))):
        rows = _parse_tables(lines, end)
    else:
        rows = _parse_without_header(lines, end)

    if not rows:
        raise PdfStatementError("Couldn't find any transactions in this PDF. Please download a CSV instead, or let us know your bank so we can support its layout.")
    return rows


# Words that can make up the extra lines of a split heading.
HEADING_WORD_RE = re.compile(
    r"^(date|trans\.?|transaction|posting|post|activity|description|details|amount|\(\$\)|\$|ref\.?#?|"
    r"balance|withdrawals?|deposits?|debits?|credits?|reference|number|no\.?)$",
    re.IGNORECASE,
)


def _stacked_header(lines, i, section_title):
    """A heading split over two or three lines, e.g. RBC's
    "TRANSACTION  POSTING" / "ACTIVITY DESCRIPTION  AMOUNT ($)" / "DATE  DATE".
    Words far to the right (a summary box beside the table) are left out."""
    base = lines[i]
    if any(_is_money(w) for w in base):
        return None
    right = max(w["x1"] for w in base) + 12
    for picks in ((i - 1, i + 1), (i + 1,), (i - 1,)):
        words = list(base)
        for j in picks:
            if not (0 <= j < len(lines) and abs(lines[j][0]["top"] - base[0]["top"]) <= 14):
                continue
            extra = [w for w in lines[j] if w["x0"] <= right]
            # Only heading words ("TRANSACTION POSTING", "DATE DATE") — not
            # e.g. "This month  Year-to-date" above a spend report.
            if extra and all(HEADING_WORD_RE.match(w["text"]) for w in extra):
                words += extra
        if len(words) > len(base):
            header = Table.from_header(sorted(words, key=lambda w: w["x0"]), section_title)
            if header:
                return header
    return None


def _looks_like_row(table, line, end):
    """A dated line with an amount under the last table's columns: the table
    goes on after a section total (MBNA lists payments, purchases and
    interest as sections of one table, each with its own "Total")."""
    cells, money = table.split(line)
    if not cells.get("desc"):
        return False        # (the amount may be on the next line — see `pending`)
    d, _ = _leading_date(cells.get("date", ""), end) if cells.get("date") else (None, "")
    return d is not None


def _find_header(lines, i, section_title):
    return Table.from_header(lines[i], section_title) or _stacked_header(lines, i, section_title)


def _parse_tables(lines, end):
    rows = []
    table = None
    in_table = False
    recent_titles = []      # plain lines seen since the last table ended
    prev_balance = None
    last_date = None
    pending = None          # a dated line whose amount is on the next line

    for i, line in enumerate(lines):
        section_title = " | ".join(recent_titles[-2:])
        header = _find_header(lines, i, section_title)
        if header:
            # A header can repeat on every page; keep reading the same table.
            table, in_table = header, True
            recent_titles = []
            if rows:
                rows[-1]["_open"] = 0      # lines under a new header aren't part of the last row
            continue

        text = " ".join(w["text"] for w in line)
        if not in_table and table is not None and _looks_like_row(table, line, end):
            in_table = True
        if not in_table or table is None:
            # Between tables, remember headings like "Your payments" for the
            # next table, and pick up an opening balance.
            if not any(_is_money(w) for w in line):
                recent_titles.append(text)
            elif re.search(r"balance", text, re.IGNORECASE):
                money = [w for w in line if _is_money(w)]
                prev_balance = _parse_money(money[-1]["text"])
            continue

        cells, money = table.split(line)
        row_text = " ".join(v for k, v in cells.items() if k in ("date", "desc", "date2", "ref"))
        date_text = cells.get("date", "")
        d, _ = _leading_date(date_text, end) if date_text else (None, "")
        if d is None and re.match(r"^\d{1,4}\s+\S", date_text):
            # Scotia prints "REF.#" and "DATE" so close together they read
            # as one heading: "001 Jun 11" is reference 001, dated Jun 11.
            d, _ = _leading_date(re.sub(r"^\d{1,4}\s+", "", date_text), end)
        if d is None and "date" not in [c["kind"] for c in table.columns]:
            d, _ = _leading_date(row_text, end)
        description = cells.get("desc", "")
        if "date" not in [c["kind"] for c in table.columns]:
            _, description = _leading_date(description, end)
        # Text after the posting date belongs to the description (RBC's
        # descriptions start left of their heading: "AUG 20 FABRIKAM*STREAMING...").
        if cells.get("date2"):
            _, spill = _leading_date(cells["date2"], end)
            if spill and spill != cells["date2"]:
                description = f"{spill} {description}".strip()

        # "Total ..." can start in the date column (CIBC: "Total for 4505 XXXX
        # XXXX 0042 $101.10"), so check the whole width of the table too — but
        # not a summary box beside it (RBC's "NEW BALANCE $..." next to the rows).
        table_text = " ".join(w["text"] for w in line if table.left <= w["x1"] and w["x0"] <= table.right)
        if not d and (TABLE_END_RE.search(description or row_text) or TABLE_END_RE.search(table_text)):
            balance = next((_parse_money(w["text"]) for k, w in money if k == "balance"), None)
            if balance is not None:
                prev_balance = balance
            in_table = False
            recent_titles = []
            continue
        if (SKIP_DATED_RE if d else SKIP_LINE_RE).search(description or row_text):
            balance = next((_parse_money(w["text"]) for k, w in money if k == "balance"), None)
            if balance is None and money and re.search(r"balance", description or row_text, re.IGNORECASE):
                balance = _parse_money(money[-1][1]["text"])
            if balance is not None:
                prev_balance = balance
            continue

        if table.stray_used and not d:
            money = []                 # e.g. "AMT 20.99 USD" under a foreign purchase
        has_amount = any(k != "balance" for k, _ in money)
        if not has_amount and d and description:
            # MBNA foreign purchases: "08/12/26 08/13/26 WL*VUE*MICROSOFT ..."
            # with "59.99 USD 0101 $85.10" on the line below.
            pending = {"date": d, "description": description}
            continue
        if has_amount and not d and pending:
            d, description = pending["date"], f'{pending["description"]} {description}'.strip()
        pending = None
        if not has_amount:
            # A wrapped description line (or a note like a foreign-currency
            # rate) belongs to the transaction above it.
            if rows and rows[-1]["_open"] and description and not d and not NOTE_LINE_RE.search(description):
                rows[-1]["description"] = f'{rows[-1]["description"]} {description}'.strip()[:200]
                rows[-1]["_open"] -= 1
            continue

        if d:
            last_date = d
        elif last_date:
            d = last_date  # the bank printed the date only once for the day
        else:
            continue

        amount, balance = table.amount(money, prev_balance)
        if balance is not None:
            prev_balance = balance
        if amount is None or amount == 0:
            continue
        description = description or row_text or "Transaction"
        if table.interest and amount < 0:
            description = f"Interest charged - {RATE_RE.sub('', description).strip()}"
        rows.append({"date": d, "description": description, "amount": amount, "_open": 2})

    for r in rows:
        r.pop("_open", None)
    return rows


def _parse_without_header(lines, end):
    """Statements with no recognisable table header: read lines that start
    with a date, using a running balance (when there is one) for direction."""
    rows = []
    prev_balance = None
    for line in lines:
        text = " ".join(w["text"] for w in line)
        d, rest = _leading_date(text, end)
        if d:
            _, rest = _leading_date(rest, end)
        money = [w for w in line if _is_money(w)]
        if not d and TABLE_END_RE.search(text):
            continue
        if SKIP_LINE_RE.search(rest if d else text):
            if money and re.search(r"balance", text, re.IGNORECASE):
                prev_balance = _parse_money(money[-1]["text"])
            continue
        if not d:
            if rows and not money and rows[-1]["_open"]:
                rows[-1]["description"] = f'{rows[-1]["description"]} {text}'.strip()[:200]
                rows[-1]["_open"] = 0
            continue
        if not money:
            continue
        values = [_parse_money(w["text"]) for w in money]
        values = [v for v in values if v is not None]
        description = " ".join(w["text"] for w in line if w not in money)
        _, description = _leading_date(description, end)
        _, description = _leading_date(description, end)

        amount = -values[0]
        if len(values) >= 2:
            if prev_balance is not None:
                change = values[-1] - prev_balance
                if abs(abs(change) - abs(values[-2])) < Decimal("0.01"):
                    amount = change
            prev_balance = values[-1]
        if amount == 0:
            continue
        rows.append({"date": d, "description": description.strip() or "Transaction", "amount": amount, "_open": 1})

    for r in rows:
        r.pop("_open", None)
    return rows
