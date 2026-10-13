# expenses/bulk.py
#
# Deleting many expenses/income entries at once (e.g. a whole month before
# re-importing its statements). Used by the API and by Aurora's tool.
# Logs one Activity summary per kind instead of a line per entry.

from decimal import Decimal

from django.db import transaction

from activity.context import activity_context, log_activity

from .models import Expense, Income

MODELS = {"expense": Expense, "income": Income}


def summarise(qs):
    items = list(qs.values_list("amount", flat=True))
    return len(items), sum(items, Decimal("0.00"))


def bulk_delete(user, kind, qs, label=""):
    """Delete the entries in `qs` (already filtered to this user). Returns (count, total)."""
    count, total = summarise(qs)
    if not count:
        return 0, Decimal("0.00")
    with transaction.atomic(), activity_context(quiet={kind}):
        qs.delete()
    singular, plural = ("expense", "expenses") if kind == "expense" else ("income entry", "income entries")
    log_activity(
        user, kind, "deleted",
        f"{count} {singular if count == 1 else plural}" + (f" ({label})" if label else ""),
        changes=[{"field": "amount", "label": "Total", "before": str(total), "after": None, "money": True}],
        details={"count": count, "total": str(total)},
    )
    return count, total
