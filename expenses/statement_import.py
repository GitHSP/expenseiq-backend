# expenses/statement_import.py
#
# Turns a bank/credit-card statement export (CSV or OFX/QFX) into rows the
# user can review before importing as expenses/income. Nothing here writes
# to the database.
#
# Amounts are kept signed from the account holder's view: negative = money
# out. Banks disagree on this for credit-card CSVs (some show purchases as
# positive), so the preview suggests flipping when most rows are positive and
# the user can flip it themselves.

import csv
import io
import re
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

MAX_ROWS = 5000


class StatementError(ValueError):
    """Raised with a user-facing message when a file can't be read."""


# ─────────────────────────────────────────────
# Low-level parsing helpers
# ─────────────────────────────────────────────

DATE_FORMATS = [
    "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%m/%d/%y", "%d/%m/%y",
    "%b %d, %Y", "%d %b %Y", "%b %d %Y", "%d-%b-%Y", "%d-%b-%y", "%Y%m%d",
]


def _decode(raw):
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise StatementError("Couldn't read the file's text encoding.")


def _parse_amount(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s or s in {"-", "--"}:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    upper = s.upper()
    if upper.endswith("CR"):
        s = s[:-2]
    elif upper.endswith("DR"):
        negative, s = True, s[:-2]
    s = s.replace("$", "").replace(",", "").replace("CAD", "").replace("USD", "").strip()
    if s.startswith("-"):
        negative, s = not negative, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", s):
        return None
    try:
        amount = Decimal(s)
    except InvalidOperation:
        return None
    return -amount if negative else amount


def _try_date(value, fmt):
    try:
        return datetime.strptime(str(value).strip(), fmt).date()
    except (ValueError, TypeError):
        return None


def _best_date_format(values):
    """Pick the format that parses the most values. Ties (e.g. every day
    is <= 12) go to the earlier format in DATE_FORMATS, i.e. month-first,
    which is what Canadian bank exports use."""
    values = [v for v in values if str(v).strip()]
    if not values:
        return None
    best, best_count = None, 0
    for fmt in DATE_FORMATS:
        count = sum(1 for v in values if _try_date(v, fmt))
        if count > best_count:
            best, best_count = fmt, count
    return best if best_count else None


# Transaction-type wording banks put before the merchant name.
GENERIC_PREFIX = re.compile(
    r"^(visa debit (retail )?purchase|visa debit|interac (retail )?purchase|retail purchase|"
    r"point of sale( purchase)?|pos (purchase|retail)|pre-?authori[sz]ed (debit|payment)|"
    r"bill payment|electronic funds transfer|eft (debit|credit)|debit memo|credit memo|"
    r"misc(ellaneous)? payment|internet banking|online banking)\b\s*",
    re.IGNORECASE,
)


def clean_title(text):
    """'VISA DEBIT RETAIL PURCHASE TIM HORTONS #24 624637090333' -> 'Tim Hortons'."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    stripped = GENERIC_PREFIX.sub("", t)
    if len(stripped) >= 3:
        t = stripped
    t = re.sub(r"#\s*\d+", "", t)                 # store numbers
    t = re.sub(r"\b[\dX*]{6,}\b", "", t)          # card/reference numbers
    t = re.sub(r"\s+", " ", t).strip(" -*")
    # Title-case words the bank shouted, leave the rest ('Realstar Corp').
    t = " ".join(w.title() if len(w) > 1 and w == w.upper() and re.search(r"[A-Z]", w) else w for w in t.split())
    return t[:120] or "Imported transaction"


# ─────────────────────────────────────────────
# OFX / QFX
# ─────────────────────────────────────────────

def _ofx_field(block, tag):
    m = re.search(rf"<{tag}>([^<\r\n]*)", block, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def parse_ofx(text):
    rows = []
    for block in re.findall(r"<STMTTRN>(.*?)</STMTTRN>", text, re.IGNORECASE | re.DOTALL):
        posted = _ofx_field(block, "DTPOSTED")[:8]
        d = _try_date(posted, "%Y%m%d")
        amount = _parse_amount(_ofx_field(block, "TRNAMT"))
        if d is None or amount is None:
            continue
        name, memo = _ofx_field(block, "NAME"), _ofx_field(block, "MEMO")
        description = name if not memo or memo.lower() in name.lower() else f"{name} {memo}"
        rows.append({"date": d, "description": description, "amount": amount})
    if not rows and "<OFX>" not in text.upper():
        raise StatementError("That doesn't look like an OFX/QFX file.")
    return rows, {"format": "ofx", "signed_amount": True}


# ─────────────────────────────────────────────
# CSV
# ─────────────────────────────────────────────

HEADER_KEYS = {
    "date":        ["transaction date", "trans. date", "trans date", "date", "posted date", "posting date"],
    "description": ["description 1", "description", "payee", "merchant", "name", "details", "transaction details", "transaction", "memo"],
    "description2": ["description 2", "sub-description", "sub description"],
    "amount":      ["amount", "cad$", "cad", "transaction amount"],
    "debit":       ["debit", "withdrawal", "withdrawals", "funds out", "money out", "paid out"],
    "credit":      ["credit", "deposit", "deposits", "funds in", "money in", "paid in"],
}


def _find_header(rows):
    """Return (index, column map) of a header row in the first lines, if any."""
    for i, row in enumerate(rows[:15]):
        cells = [c.strip().lower() for c in row]
        mapping = {}
        # Exact header names first, then headers that merely contain one
        # (e.g. "Withdrawals ($)", "Amount (CAD)").
        for exact in (True, False):
            for key, names in HEADER_KEYS.items():
                if key in mapping:
                    continue
                for name in names:
                    found = next(
                        (j for j, c in enumerate(cells)
                         if (c == name if exact else name in c) and j not in mapping.values()),
                        None,
                    )
                    if found is not None:
                        mapping[key] = found
                        break
        if "date" in mapping and ("amount" in mapping or "debit" in mapping or "credit" in mapping):
            return i, mapping
    return None, None


def _infer_columns(rows):
    """Work out the columns of a header-less CSV from its contents."""
    width = max(len(r) for r in rows)
    sample = rows[:200]

    def col(i):
        return [r[i] if i < len(r) else "" for r in sample]

    date_col, date_hits = None, 0
    for i in range(width):
        fmt = _best_date_format(col(i))
        hits = sum(1 for v in col(i) if fmt and _try_date(v, fmt))
        if hits > date_hits:
            date_col, date_hits = i, hits
    if date_col is None or date_hits < len(sample) * 0.6:
        raise StatementError("Couldn't find a date column in this CSV.")

    numeric, text = [], []
    for i in range(width):
        if i == date_col:
            continue
        values = [v for v in col(i) if v.strip()]
        if not values:
            continue
        numeric_share = sum(1 for v in values if _parse_amount(v) is not None) / len(values)
        if numeric_share >= 0.9:
            numeric.append(i)
        else:
            text.append((sum(len(v) for v in values) / len(values), i))

    if not numeric:
        raise StatementError("Couldn't find an amount column in this CSV.")
    if not text:
        raise StatementError("Couldn't find a description column in this CSV.")
    description_col = max(text)[1]

    mapping = {"date": date_col, "description": description_col}
    after = [i for i in numeric if i > description_col] or numeric
    if len(after) >= 2:
        # Debit, credit (and possibly a running balance after them).
        mapping["debit"], mapping["credit"] = after[0], after[1]
    else:
        mapping["amount"] = after[0]
    return mapping


def parse_csv(text):
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        raise StatementError("The file is empty.")

    header_index, mapping = _find_header(rows)
    data = rows[header_index + 1:] if header_index is not None else rows
    if header_index is None:
        mapping = _infer_columns(data)
    if not data:
        raise StatementError("No transactions found in the file.")

    def cell(row, key):
        i = mapping.get(key)
        return row[i] if i is not None and i < len(row) else ""

    fmt = _best_date_format([cell(r, "date") for r in data])
    if not fmt:
        raise StatementError("Couldn't understand the dates in this CSV.")

    out = []
    for row in data:
        d = _try_date(cell(row, "date"), fmt)
        if d is None:
            continue
        if "amount" in mapping:
            amount = _parse_amount(cell(row, "amount"))
        else:
            debit, credit = _parse_amount(cell(row, "debit")), _parse_amount(cell(row, "credit"))
            if debit is None and credit is None:
                continue
            amount = (abs(credit) if credit else Decimal("0")) - (abs(debit) if debit else Decimal("0"))
        if amount is None or amount == 0:
            continue
        description = " ".join(p for p in (cell(row, "description").strip(), cell(row, "description2").strip()) if p)
        out.append({"date": d, "description": description, "amount": amount})

    if not out:
        raise StatementError("No transactions found in the file.")
    return out, {"format": "csv", "signed_amount": "amount" in mapping}


def parse_statement(filename, raw):
    name = (filename or "").lower()
    if raw[:5] == b"%PDF-" or name.endswith(".pdf"):
        from .pdf_statement import PdfStatementError, parse_pdf
        try:
            rows = parse_pdf(raw)
        except PdfStatementError as e:
            raise StatementError(str(e))
        if len(rows) > MAX_ROWS:
            raise StatementError(f"That file has {len(rows)} transactions — please import at most {MAX_ROWS} at a time.")
        return rows, {"format": "pdf", "signed_amount": True}

    text = _decode(raw)
    if name.endswith((".ofx", ".qfx")) or "<OFX>" in text[:5000].upper():
        rows, info = parse_ofx(text)
    elif name.endswith((".csv", ".txt")) or "," in text[:2000]:
        try:
            rows, info = parse_csv(text)
        except StatementError:
            if name.endswith(".csv"):
                raise
            # Not a .csv and not readable as one: probably not a statement.
            raise StatementError("This doesn't look like a bank statement. Upload the PDF, CSV, OFX or QFX file from your bank.")
    else:
        raise StatementError("Please upload a PDF, CSV, OFX or QFX file.")

    if not rows:
        raise StatementError("No transactions found in the file.")
    if len(rows) > MAX_ROWS:
        raise StatementError(f"That file has {len(rows)} transactions — please import at most {MAX_ROWS} at a time.")
    return rows, info


# ─────────────────────────────────────────────
# Categorising
# ─────────────────────────────────────────────

TRANSFER_PATTERNS = re.compile(
    r"payment.{0,10}thank you|paiement.{0,10}merci|payment received|"
    r"^tfr\b|(?<!e-)(?<!e)\btransfer (to|from)\b|online transfer|internet transfer|"
    r"credit card payment|mb-credit card|"
    r"(payment|pmt|pymt).{0,20}(visa|mastercard|mbna|amex)|"
    r"(visa|mastercard|mbna|amex).{0,20}(payment|pmt|pymt)",
    re.IGNORECASE,
)

EXPENSE_RULES = [
    ("Subscriptions",          r"netflix|spotify|disney\s?\+|disney plus|apple\.com/bill|itunes|youtube premium|google \*|prime video|amazon prime|crave|icloud|adobe|openai|chatgpt|patreon|audible"),
    ("Food & Dining",          r"tim hortons|starbucks|mcdonald|subway|a&w|wendy|burger|kfc|popeyes|pizza|domino|sushi|restaurant|cafe|coffee|bakery|grill|\bpub\b|uber\s?eats|doordash|skip ?the ?dishes|chipotle|freshii|harvey|mary brown|loblaws|no ?frills|sobeys|\bmetro\b|food basics|freshco|superstore|safeway|save-on|farm boy|t&t|longo|walmart|costco|grocery|fortinos|zehrs|iga\b|foodland"),
    ("Transport",              r"\buber\b(?!\s?eats)|lyft|presto|\bttc\b|go transit|translink|compass card|oc transpo|\bstm\b|via rail|petro|esso|shell|chevron|husky|pioneer|ultramar|\bgas\b|parking|impark|green ?p|indigo park|mto|serviceontario"),
    ("Bills & Utilities",      r"rogers|\bbell\b|telus|fido|koodo|virgin (plus|mobile)|freedom mobile|chatr|public mobile|lucky mobile|hydro|enbridge|fortis|alectra|utilit|internet|monthly fee|service charge|account fee|nsf fee|overdraft"),
    ("Insurance",              r"insurance|intact|manulife|sun life|belair|aviva|desjardins ins|wawanesa|sonnet|square one"),
    ("Housing",                r"\brent\b|landlord|property management|mortgage|condo fee|realstar|capreit|minto|greenwin|killam|"
                               r"boardwalk|mainstreet|timbercreek|homestead|sterling karamar|medallion|hazelview|starlight|metcap|"
                               r"osgoode|bentall|skyline living|residences"),
    ("Gym & Fitness",          r"goodlife|fit4less|planet fitness|la fitness|anytime fitness|orangetheory|\bgym\b|ymca|crossfit"),
    ("Health",                 r"pharma|shoppers drug|rexall|london drugs|jean coutu|drug mart"),
    ("Medical & Health",       r"clinic|dental|dentist|physio|optometr|medical|hospital|chiro|massage therap"),
    ("Personal Care & Beauty", r"salon|barber|\bspa\b|sephora|nail"),
    ("Clothing & Shopping",    r"h&m|\bzara\b|uniqlo|old navy|\bgap\b|lululemon|sport chek|aritzia|\bnike\b|adidas|winners|marshalls|simons|reitmans"),
    ("Shopping",               r"amazon|amzn|best buy|canadian tire|dollarama|ikea|home depot|staples|the bay|hbc|ebay|etsy|rona|lowe|apple store|temu|shein|aliexpress"),
    ("Entertainment",          r"cineplex|ticketmaster|steam|playstation|xbox|nintendo|landmark cinema|eventbrite|bowling|theatre"),
    ("Education & Books",      r"indigo|chapters|coursera|udemy|tuition|university|college|school"),
    ("Gifts & Donations",      r"donation|charity|gofundme|red cross|unicef"),
    ("Abroad Expense",         r"\bwise\b|transferwise|western union|remitly|moneygram|xoom|remit"),
]

INCOME_RULES = [
    ("Salary",     r"payroll|pay ?roll|salary|direct dep|\bpay\b|wages"),
    ("Refund",     r"refund|return|reversal|rebate|cashback|cash back"),
    ("Investment", r"interest|dividend"),
]


def _history_key(text):
    words = re.findall(r"[a-z0-9&']+", str(text).lower())
    return " ".join(words[:3])


def _user_history(user):
    """Map of normalised title -> the category this user most often gives it."""
    from .models import Expense, Income

    def build(qs):
        votes = {}
        for title, category in qs.values_list("title", "category")[:5000]:
            votes.setdefault(_history_key(title), Counter())[category] += 1
        return {k: c.most_common(1)[0][0] for k, c in votes.items() if k}

    return (
        build(Expense.objects.filter(user=user).order_by("-created_at")),
        build(Income.objects.filter(user=user).order_by("-created_at")),
    )


def _match(rules, text, default):
    for category, pattern in rules:
        if re.search(pattern, text, re.IGNORECASE):
            return category
    return default


MONEY_MOVEMENT = re.compile(
    r"transfer|\btrf\b|\btfr\b|interac|e-?trf|deposit|payment|\bpmt\b|\bpymt\b|\beft\b|withdrawal|\batm\b",
    re.IGNORECASE,
)

TRANSFER_WINDOW_DAYS = 3


def _close(d1, d2):
    return abs((d1 - d2).days) <= TRANSFER_WINDOW_DAYS


def build_preview(user, files):
    """
    Add category guesses and transfer/duplicate flags to parsed statements.

    `files` is a list of {"rows": [...], "flip": bool}; `flip` is the
    suggested sign flip for that file (see suggest_flip) and is used to
    tell money in from money out when pairing transfers between files.
    Returns one flat list of preview rows, each tagged with its file index.
    """
    from .models import Expense, Income

    expense_history, income_history = _user_history(user)

    flat = []
    for file_index, f in enumerate(files):
        for r in f["rows"]:
            out = (r["amount"] < 0) != f["flip"]
            flat.append({**r, "file": file_index, "out": out, "abs": abs(r["amount"])})

    dates = [r["date"] for r in flat]
    window = (min(dates) - timedelta(days=TRANSFER_WINDOW_DAYS), max(dates) + timedelta(days=TRANSFER_WINDOW_DAYS))
    existing = {"expense": [], "income": []}
    for kind, model in (("expense", Expense), ("income", Income)):
        qs = model.objects.filter(user=user, date__range=window).values_list("amount", "date", "title", "notes")
        existing[kind] = [
            {"amount": abs(a), "date": d, "title": t, "imported": (n or "").startswith("Imported from")}
            for a, d, t, n in qs
        ]

    def is_duplicate(kind, row):
        return any(e["amount"] == row["abs"] and _close(e["date"], row["date"]) for e in existing[kind])

    # ── Transfers between the user's own accounts in this batch ──
    # Same amount, opposite direction, different statements, a few days
    # apart, and at least one side reads like money moving.
    incoming_by_amount = {}
    for j, b in enumerate(flat):
        if not b["out"]:
            incoming_by_amount.setdefault(b["abs"], []).append(j)

    options = {}
    for i, a in enumerate(flat):
        if not a["out"]:
            continue
        options[i] = [
            (abs((a["date"] - flat[j]["date"]).days), j)
            for j in incoming_by_amount.get(a["abs"], [])
            if flat[j]["file"] != a["file"]
            and _close(a["date"], flat[j]["date"])
            and (MONEY_MOVEMENT.search(a["description"]) or MONEY_MOVEMENT.search(flat[j]["description"]))
        ]

    # Money often hops through accounts with the same amount — Simplii →
    # CIBC, then CIBC → Visa. Pair the rows with the fewest possible partners
    # first so CIBC's payment takes the Visa credit, leaving CIBC's deposit
    # for Simplii, instead of Simplii grabbing the Visa credit.
    pairs = {}
    for i in sorted(options, key=lambda i: (len(options[i]), i)):
        candidates = [c for c in options[i] if c[1] not in pairs]
        if candidates:
            j = min(candidates)[1]
            pairs[i], pairs[j] = j, i

    seen = Counter()
    preview = []
    for i, r in enumerate(flat):
        title = clean_title(r["description"])
        key = _history_key(title)
        # Identical rows (same day, amount, text) are almost always the same
        # transaction listed twice, e.g. overlapping statements.
        row_key = (r["date"], r["abs"], title.lower())
        seen[row_key] += 1

        transfer_match = None
        if i in pairs:
            other = flat[pairs[i]]
            transfer_match = {
                "type":  "pair",
                "row":   pairs[i],
                "file":  other["file"],
                "date":  other["date"].isoformat(),
                "title": clean_title(other["description"]),
            }
        elif MONEY_MOVEMENT.search(r["description"]):
            # The other half may have been imported earlier as a normal
            # expense/income — e.g. last week's Simplii "e-transfer sent".
            opposite = "income" if r["out"] else "expense"
            match = next(
                (e for e in existing[opposite]
                 if e["imported"] and e["amount"] == r["abs"] and _close(e["date"], r["date"])),
                None,
            )
            if match:
                transfer_match = {
                    "type":  "existing",
                    "kind":  opposite,
                    "date":  match["date"].isoformat(),
                    "title": match["title"],
                }

        preview.append({
            "id":                i,
            "file":              r["file"],
            "date":              r["date"].isoformat(),
            "title":             title,
            "raw_description":   r["description"],
            "amount":            str(r["abs"]),
            "signed_amount":     str(r["amount"]),
            "is_transfer":       bool(TRANSFER_PATTERNS.search(r["description"])),
            "transfer_match":    transfer_match,
            "expense_category":  expense_history.get(key) or _match(EXPENSE_RULES, r["description"], "Other"),
            "income_category":   income_history.get(key) or _match(INCOME_RULES, r["description"], "Other"),
            "duplicate_expense": is_duplicate("expense", r) or seen[row_key] > 1,
            "duplicate_income":  is_duplicate("income", r) or seen[row_key] > 1,
        })
    return preview


def suggest_flip(rows, info):
    """Credit-card CSVs often list purchases as positive numbers. If most
    rows in a single-amount CSV are positive, suggest treating them as
    money out."""
    if not info.get("signed_amount") or info.get("format") != "csv":
        return False
    positives = sum(1 for r in rows if r["amount"] > 0)
    return positives / len(rows) > 0.7


# ─────────────────────────────────────────────
# Cross-checking against the Financial Planner checklist
# ─────────────────────────────────────────────

PLANNER_STOPWORDS = {
    "payment", "payments", "minimum", "min", "pmt", "pymt", "the", "and", "for",
    "from", "with", "online", "bill", "pay", "extra", "avalanche", "surplus",
    "all", "regular", "account", "main", "day", "first", "transfer", "monthly",
    "csv", "pdf", "qfx", "ofx", "statement", "visa", "mastercard",
}
PLANNER_DUE_WINDOW = 5
DEBT_CATEGORIES = ("debt_min", "debt_extra")


def _words(text):
    return {w for w in re.findall(r"[a-z][a-z0-9&']{2,}", str(text).lower()) if w not in PLANNER_STOPWORDS}


def match_checklist(user, preview, files):
    """
    Suggest which unticked Financial Planner checklist items each statement
    row pays, so importing can tick them off. A match needs the same amount
    plus either overlapping names (the statement's file name counts, so a
    "PAYMENT THANK YOU" in cibc-visa.pdf can match "CIBC Visa minimum") or a
    date within a few days of the item's due day. Each item matches once.
    Sets row["checklist_match"] = {"id", "label", "month"} or None.
    """
    from financial_planner.models import ChecklistItem

    months = {(int(r["date"][:4]), int(r["date"][5:7])) for r in preview}
    by_month = {}
    items = (
        ChecklistItem.objects
        .filter(monthly_plan__user=user, is_completed=False, amount__gt=0)
        .select_related("monthly_plan", "linked_debt")
    )
    for item in items:
        key = (item.monthly_plan.year, item.monthly_plan.month)
        if key in months:
            by_month.setdefault(key, []).append(item)

    used = set()
    for row in preview:
        row["checklist_match"] = None
        d = date.fromisoformat(row["date"])
        amount = Decimal(row["amount"])
        money_out = (Decimal(row["signed_amount"]) < 0) != files[row["file"]]["flip"]
        row_words = _words(f'{row["raw_description"]} {row["title"]} {files[row["file"]]["filename"]}')

        best, best_score = None, 0
        for item in by_month.get((d.year, d.month), []):
            if item.id in used or abs(item.amount - amount) > Decimal("0.01"):
                continue
            # Debt payments show up as money out at the bank and money in on
            # the card itself — either side is proof of payment.
            if item.category not in DEBT_CATEGORIES and money_out == (item.category == "income"):
                continue
            item_words = _words(item.label)
            if item.linked_debt:
                item_words |= _words(item.linked_debt.name)
            overlap = len(row_words & item_words)
            near_due = item.due_day is not None and abs(d.day - item.due_day) <= PLANNER_DUE_WINDOW
            score = overlap * 2 + (1 if near_due else 0)
            if (overlap or near_due) and score > best_score:
                best, best_score = item, score

        if best:
            used.add(best.id)
            # Paying a planned bill (rent, insurance, family transfer...) is
            # real spending even if the bank words it like a transfer.
            if best.category not in DEBT_CATEGORIES and best.category != "savings":
                row["is_transfer"] = False
            row["checklist_match"] = {
                "id":    best.id,
                "label": best.label,
                "month": f"{best.monthly_plan.year}-{best.monthly_plan.month:02d}",
            }
    return preview
