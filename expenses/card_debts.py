# expenses/card_debts.py
#
# Credit-card accounts and their Planner debts: each card is linked to a
# debt (matched by name, or chosen by the user), and the debt's balance,
# minimum payment, limit and due day follow the card's newest statement.
# Also: payments made to a card from the user's bank accounts since that
# statement, so the balance stays current between statements.

from decimal import Decimal

from activity.context import activity_context

from .accounts import match_debt
from .models import Account, StatementTransaction


def link_debt(account):
    """Link a card to its debt if it isn't linked yet and the match is clear."""
    from financial_planner.models import Debt
    if account.kind != "credit_card" or account.debt_id:
        return account.debt
    taken = Account.objects.filter(user=account.user, debt__isnull=False).exclude(pk=account.pk).values_list("debt_id", flat=True)
    debt = match_debt(account, list(Debt.objects.filter(user=account.user, is_active=True).exclude(id__in=taken)))
    if debt:
        account.debt = debt
        account.save(update_fields=["debt"])
    return debt


def sync_debt(account, force=False):
    """Bring the linked debt up to date with the card's newest statement.
    Returns a short description of what changed, or None."""
    debt = link_debt(account)
    if not debt:
        return None
    latest = account.statements.exclude(new_balance=None).order_by("-period_end", "-id").first()
    if not latest:
        return None
    if not force and account.debt_synced_through and latest.period_end < account.debt_synced_through:
        return None          # an older statement: the debt already reflects a newer one
    changes = {"current_balance": max(latest.new_balance, Decimal("0.00"))}
    if latest.minimum_payment is not None:
        changes["minimum_payment"] = latest.minimum_payment
    if latest.credit_limit is not None:
        changes["credit_limit"] = latest.credit_limit
    if latest.payment_due is not None:
        changes["due_day"] = latest.payment_due.day
    changed = {f: v for f, v in changes.items() if getattr(debt, f) != v}
    if changed:
        reason = f"From the {account.name} statement ending {latest.period_end:%b} {latest.period_end.day}, {latest.period_end.year}"
        with activity_context(source="import", reason=reason):
            for field, value in changed.items():
                setattr(debt, field, value)
            debt.save(update_fields=[*changed, "updated_at"])
    if account.debt_synced_through != latest.period_end:
        account.debt_synced_through = latest.period_end
        account.save(update_fields=["debt_synced_through"])
    return {"account": account.name, "debt": debt.name, "balance": str(debt.current_balance)} if changed else None


def _card_words(account):
    """How a card shows up in a bank account's transactions."""
    words = []
    if account.last4:
        words.append(account.last4)
    name = account.name.split("•")[0].strip().upper()
    if name:
        words.append(name)                                       # "CIBC VISA"
    inst = (account.institution or "").upper()
    if inst and inst not in ("CIBC", "TD", "RBC", "BMO"):        # big banks' names appear on other payments too
        words.append(inst)                                       # "MBNA", "SCOTIABANK"
    if inst == "SCOTIABANK":
        words.append("SCOTIA")
    return words


def payments_since(account, after):
    """Payments to this card from the user's other accounts dated after
    `after` (the card's last statement), newest first."""
    if not after:
        return []
    words = _card_words(account)
    if not words:
        return []
    rows = StatementTransaction.objects.filter(
        user=account.user, amount__lt=0, date__gt=after,
    ).exclude(account=account).exclude(account__kind="credit_card").select_related("account").order_by("-date")
    found = []
    for tx in rows:
        text = f"{tx.description} {tx.title}".upper()
        if any(w in text for w in words) and (tx.kind == "transfer" or "PAYMENT" in text or "CARD" in text):
            found.append(tx)
    return found


def card_history(account):
    """Per statement: what was owed, paid, spent, charged in interest/fees."""
    out = []
    for st in account.statements.order_by("period_start", "id"):
        paid = spent = interest = Decimal("0")
        for tx in st.transactions.all():
            text = tx.description.upper()
            if tx.amount > 0:
                paid += tx.amount
            elif "INTEREST" in text and "REVERSAL" not in text:
                interest -= tx.amount
            else:
                spent -= tx.amount
        out.append({
            "statement_id": st.id,
            "period_start": st.period_start.isoformat(), "period_end": st.period_end.isoformat(),
            "previous_balance": None if st.previous_balance is None else str(st.previous_balance),
            "new_balance": None if st.new_balance is None else str(st.new_balance),
            "paid": str(paid), "spent": str(spent), "interest": str(interest),
            "minimum_payment": None if st.minimum_payment is None else str(st.minimum_payment),
            "payment_due": st.payment_due.isoformat() if st.payment_due else None,
            "credit_limit": None if st.credit_limit is None else str(st.credit_limit),
        })
    return out
