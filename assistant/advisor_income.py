# assistant/advisor_income.py
#
# Planned income for a month straight from the recurring list (income isn't
# on the checklist): a biweekly pay counts once per payday that month.

from decimal import Decimal


def planned_income_rows(user, year, month):
    from financial_planner.models import RecurringItem
    from financial_planner.services import paydays
    rows = []
    for rec in RecurringItem.objects.filter(user=user, is_active=True, category="income"):
        times = len(paydays(rec, year, month)) if rec.frequency in ("weekly", "biweekly") else 1
        rows.append({"label": rec.label, "amount": rec.amount * times, "category": "income",
                     "due_day": rec.due_day, "debt": None, "done": None})
    return rows
