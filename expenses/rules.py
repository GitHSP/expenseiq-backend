# expenses/rules.py
#
# The user's tidy-up rules ("anything mentioning Tim Hortons -> Coffee",
# "Wealthsimple is my own account"): applied to what's already imported and
# to statement previews, so future imports follow them too.

import re
from decimal import Decimal

from django.db import transaction
from django.db.models import Q

from activity.context import activity_context

from .models import Expense, Income, MerchantRule, StatementTransaction


# A payment onto a credit card (money from the user's bank account).
CARD_PAYMENT_RE = re.compile(r"\bPAYMENT\b|\bPAIEMENT\b", re.I)


def shared_matchers(user, treat_as="shared"):
    """Names whose money is their share of bills the user pays (not income);
    with treat_as="bill", the bills themselves (rent, utilities)."""
    return [r.match.lower() for r in MerchantRule.objects.filter(user=user, treat_as=treat_as)]


def is_shared(text, matchers):
    text = str(text or "").lower()
    return any(m in text for m in matchers)


def shared_name(text, matchers):
    """Which shared rule a row belongs to, as a display name ("alex" -> "Alex")."""
    text = str(text or "").lower()
    hits = [m for m in matchers if m in text]
    return max(hits, key=len).title() if hits else "Other"


def parse_share(text):
    """'625' -> (625, None); '25%' -> (None, 25); '' -> (None, None). ValueError if bad."""
    text = str(text or "").strip().replace(",", "").replace("$", "")
    if not text:
        return None, None
    if text.endswith("%"):
        pct = Decimal(text[:-1].strip())
        if not 0 < pct <= 100:
            raise ValueError("A percent share must be between 0 and 100.")
        return None, pct
    amount = Decimal(text)
    if amount <= 0:
        raise ValueError("A share must be more than 0.")
    return amount, None


def share_label(rule):
    if rule.share_amount is not None:
        return f"{rule.share_amount.normalize():f}"
    if rule.share_percent is not None:
        return f"{rule.share_percent.normalize():f}%"
    return ""


def bill_share(rules, text, paid):
    """The user's part of a shared bill they paid in full (None: no share set)."""
    rule = rule_for([r for r in rules if r.treat_as == "bill"], text)
    if rule is None:
        return None
    if rule.share_amount is not None:
        return min(rule.share_amount, paid)
    if rule.share_percent is not None:
        return (paid * rule.share_percent / 100).quantize(Decimal("0.01"))
    return None


def shares_set(user):
    """Shared bills are saved at the user's share, so roommates' money just pays them back."""
    return MerchantRule.objects.filter(user=user, treat_as="bill").filter(
        Q(share_amount__isnull=False) | Q(share_percent__isnull=False)).exists()


def rule_for(rules, *texts):
    """The rule that applies to a transaction (longest match wins)."""
    hay = " ".join(str(t or "") for t in texts).lower()
    hits = [r for r in rules if r.match.lower() in hay]
    return max(hits, key=lambda r: len(r.match)) if hits else None


def apply_to_preview(user, preview, file_kinds=None):
    """Statement preview rows follow the user's rules. On bank accounts, money
    coming in is only ticked as income when it's pay, a government benefit or
    an ATM deposit (file_kinds: file index -> account kind)."""
    from .income_policy import deposit_class, income_words
    rules = list(MerchantRule.objects.filter(user=user))
    words = income_words(user)
    from .income_policy import is_charge
    for row in preview:
        if Decimal(str(row.get("signed_amount") or 0)) < 0 and is_charge(f'{row["raw_description"]} {row["title"]}'):
            row["not_expense"] = "charge"              # interest or a fee: not an expense
        kind = (file_kinds or {}).get(row.get("file"))
        if (kind and Decimal(str(row.get("signed_amount") or 0)) > 0
                and not row.get("is_transfer") and not row.get("transfer_match")):
            text = f'{row["raw_description"]} {row["title"]}'
            if kind == "credit_card":
                # Money onto a card is a payment (own money moving) or a refund — never income.
                if CARD_PAYMENT_RE.search(text):
                    row["is_transfer"] = True
                else:
                    row["not_income"] = "refund"
                continue
            cls = deposit_class(text, words)
            if cls != "income":
                row["not_income"] = cls or "other"     # "refund" or "other"
    if not rules:
        return preview
    for row in preview:
        rule = rule_for(rules, row["raw_description"], row["title"])
        if not rule:
            continue
        row["rule"] = rule.match
        if rule.rename_to:
            row["title"] = rule.rename_to
        if rule.category:
            row["expense_category"] = rule.category
        if rule.treat_as == "transfer":
            row["is_transfer"] = True
        elif rule.treat_as in ("spending", "bill"):
            row["is_transfer"] = False
            row["transfer_match"] = None
            row.pop("not_income", None)            # the user said it's real income/spending
        elif rule.treat_as == "shared" and Decimal(str(row.get("signed_amount") or 0)) > 0:
            # Someone's share of the bills: not income, so not imported as income.
            row["is_transfer"] = True
            row["shared"] = True
            row.pop("not_income", None)
    return preview


def apply_rule(user, rule):
    """Apply one rule to everything already imported. Returns counts.
    'Own money' removes expenses/income that were saved from matching
    statement rows (they weren't really spending or income)."""
    text = Q(description__icontains=rule.match) | Q(title__icontains=rule.match)
    rows = StatementTransaction.objects.filter(user=user).filter(text).select_related("expense", "income")
    counts = {"statement_rows": 0, "entries_updated": 0, "entries_removed": 0}
    with transaction.atomic(), activity_context(source="assistant", quiet={"expense", "income"}):
        for tx in rows:
            changes = {}
            if rule.rename_to and tx.title != rule.rename_to:
                changes["title"] = rule.rename_to
            if rule.category and tx.category != rule.category:
                changes["category"] = rule.category
            if rule.treat_as == "transfer":
                for entry in (tx.expense, tx.income):
                    if entry is not None:
                        entry.delete()
                        counts["entries_removed"] += 1
                changes.update(kind="transfer", expense=None, income=None)
            elif rule.treat_as in ("spending", "shared", "bill") and tx.kind == "transfer":
                changes["kind"] = "skipped"
            if rule.treat_as == "shared" and tx.amount > 0:
                # Their share of the bills isn't income: drop income entries saved from it.
                if tx.income is not None:
                    tx.income.delete()
                    counts["entries_removed"] += 1
                    changes["income"] = None
                if tx.category != "Shared bills":
                    changes["category"] = "Shared bills"
                if tx.kind == "income":
                    changes["kind"] = "skipped"
            if rule.treat_as == "bill" and tx.amount < 0 and tx.expense is not None:
                # Saved at the user's share (rent 625 of 2,550), or the full bill without one.
                full = -tx.amount
                share = bill_share([rule], f"{tx.title} {tx.description}", full)
                want = share if share is not None else full
                if tx.expense.amount != want:
                    tx.expense.amount = want
                    tx.expense.notes = (f"Your share of {full:,.2f} (shared bill)" if share is not None
                                        else tx.expense.notes.replace("(shared bill)", "").strip())
                    tx.expense.save(update_fields=["amount", "notes"])
                    counts["entries_updated"] += 1
            if rule.treat_as != "transfer":
                for entry in (tx.expense, None if "income" in changes else tx.income):
                    if entry is None:
                        continue
                    fields = []
                    if rule.rename_to and entry.title != rule.rename_to:
                        entry.title = rule.rename_to
                        fields.append("title")
                    if rule.category in [c[0] for c in type(entry).CATEGORIES] and entry.category != rule.category:
                        entry.category = rule.category
                        fields.append("category")
                    if fields:
                        entry.save(update_fields=fields)
                        counts["entries_updated"] += 1
            if changes:
                for field, value in changes.items():
                    setattr(tx, field, value)
                tx.save(update_fields=list(changes))
                counts["statement_rows"] += 1
        # Entries (from statements or typed in) with matching titles.
        if rule.treat_as != "transfer" and (rule.rename_to or rule.category):
            for model in (Expense, Income):
                for entry in model.objects.filter(user=user, title__icontains=rule.match):
                    fields = []
                    if rule.rename_to and entry.title != rule.rename_to:
                        entry.title = rule.rename_to
                        fields.append("title")
                    valid = [c[0] for c in model.CATEGORIES]
                    if rule.category in valid and entry.category != rule.category:
                        entry.category = rule.category
                        fields.append("category")
                    if fields:
                        entry.save(update_fields=fields)
                        counts["entries_updated"] += 1
    return counts


def apply_all_rules(user):
    """Re-apply every rule (after an import or re-upload, so rows saved as
    income/expenses that a rule says otherwise get fixed). Returns totals."""
    totals = {"statement_rows": 0, "entries_updated": 0, "entries_removed": 0}
    for rule in MerchantRule.objects.filter(user=user):
        for k, v in apply_rule(user, rule).items():
            totals[k] += v
    return totals
