"""
The Sunday summary: what was spent this week against a usual week, the
biggest categories, what's due next week, regular charges that changed, and
where the debt-free date stands.
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from .reminders import build_reminders

USUAL_WEEKS = 8


def build_weekly(user, today=None):
    from expenses.models import Expense
    from .outlook import build_outlook
    today = today or timezone.localdate()
    start = today - timedelta(days=7)
    week = Expense.objects.filter(user=user, date__gte=start, date__lt=today)
    spent = week.aggregate(s=Sum("amount"))["s"] or Decimal("0")
    before = Expense.objects.filter(user=user, date__gte=start - timedelta(weeks=USUAL_WEEKS), date__lt=start)
    usual = (before.aggregate(s=Sum("amount"))["s"] or Decimal("0")) / USUAL_WEEKS
    top = list(week.values("category").annotate(total=Sum("amount")).order_by("-total")[:3])
    r = build_reminders(user, today=today, days=7)
    plan = build_outlook(user, today=today)
    planner = plan["scenarios"]["planner"] if plan["has_debts"] else None
    return {
        "start": start, "end": today - timedelta(days=1),
        "spent": spent.quantize(Decimal("0.01")), "usual": usual.quantize(Decimal("0.01")),
        "top": [{"category": t["category"], "total": t["total"]} for t in top],
        "due": r["due"], "closing": r["closing"], "changes": r["changes"],
        "debt_free": planner["date"] if planner and planner["months"] else None,
        "total_debt": Decimal(str(plan["total_debt"])) if plan["has_debts"] else None,
    }
