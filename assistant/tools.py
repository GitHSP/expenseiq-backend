# assistant/tools.py
#
# Tool schemas (Claude tool-use format) + the Python handlers that execute
# them. Handlers reuse the same models/serializers/services the rest of the
# app uses, so an action taken via chat behaves identically to the same
# action taken through the UI (same validation, same side effects).
#
# Since chat never references numeric IDs, lookups (a debt, an expense, a
# checklist item) are done by case-insensitive substring match on name/
# title/label. Zero or multiple matches return a plain-language message
# instead of guessing, so Claude can ask the user to be more specific.

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from expenses.models import Expense, Income
from expenses.serializers import ExpenseSerializer, IncomeSerializer
from financial_planner.models import Debt, EmergencyFund, MonthlyPlan, ChecklistItem
from financial_planner.serializers import (
    DebtSerializer, EmergencyFundSerializer, ChecklistItemSerializer, MonthlyPlanSerializer,
)
from financial_planner.services import (
    toggle_checklist_item as _toggle_checklist_item,
    recalculate_avalanche_order,
    generate_checklist as _generate_checklist,
    rollover_month as _rollover_month,
)

EXPENSE_CATEGORIES = [c[0] for c in Expense.CATEGORIES]
INCOME_CATEGORIES = [c[0] for c in Income.CATEGORIES]
DEBT_TYPES = [c[0] for c in Debt.DEBT_TYPE_CHOICES]
CHECKLIST_CATEGORIES = [c[0] for c in ChecklistItem.CATEGORY_CHOICES]


# ─────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────

def _to_decimal(value, field="amount"):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"'{value}' is not a valid number for {field}.")


def _parse_date(value):
    if not value:
        return date.today()
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return date.today()


def _find_one(queryset, field_name, query_value, kind_label):
    """Case-insensitive substring lookup. Returns (instance, None) on a
    single match, or (None, message) on zero/multiple matches."""
    matches = list(queryset.filter(**{f"{field_name}__icontains": query_value}))
    if not matches:
        return None, f"No {kind_label} found matching '{query_value}'."
    if len(matches) > 1:
        names = ", ".join(str(getattr(m, field_name)) for m in matches[:8])
        return None, (
            f"Found {len(matches)} {kind_label}s matching '{query_value}': {names}. "
            "Please be more specific."
        )
    return matches[0], None


# ─────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────

def get_upcoming_payments(user, **kwargs):
    today = date.today()
    plan = MonthlyPlan.objects.filter(user=user, year=today.year, month=today.month).first()
    if not plan:
        return {"ok": False, "message": "No monthly plan found for this month."}

    upcoming_items = plan.checklist_items.filter(
        is_completed=False,
        due_day__gte=today.day
    ).order_by("due_day")

    items_data = [
        {
            "label": item.label,
            "amount": str(item.amount) if item.amount is not None else None,
            "category": item.category,
            "due_day": item.due_day,
            "linked_debt": item.linked_debt.name if item.linked_debt else None,
        }
        for item in upcoming_items
    ]

    return {
        "ok": True,
        "month": f"{today.year}-{today.month:02d}",
        "upcoming_payments": items_data,
    }

def get_financial_summary(user, **kwargs):
    today = date.today()
    month_expenses = Expense.objects.filter(user=user, date__year=today.year, date__month=today.month)
    month_income = Income.objects.filter(user=user, date__year=today.year, date__month=today.month)
    total_expenses = sum((e.amount for e in month_expenses), Decimal("0.00"))
    total_income = sum((i.amount for i in month_income), Decimal("0.00"))

    debts = Debt.objects.filter(user=user, is_active=True).order_by("avalanche_order")
    debts_data = [
        {
            "name": d.name,
            "balance": str(d.current_balance),
            "apr": str(d.annual_interest_rate),
            "minimum_payment": str(d.minimum_payment),
            "avalanche_order": d.avalanche_order,
        }
        for d in debts
    ]
    total_debt = sum((d.current_balance for d in debts), Decimal("0.00"))

    try:
        fund = EmergencyFund.objects.get(user=user)
        fund_data = {
            "current_balance": str(fund.current_balance),
            "target_amount": str(fund.target_amount),
            "is_funded": fund.is_funded,
        }
    except EmergencyFund.DoesNotExist:
        fund_data = None

    plan = MonthlyPlan.objects.filter(user=user, year=today.year, month=today.month).first()
    if plan:
        items = plan.checklist_items.all()
        checklist_data = {
            "total_items": items.count(),
            "completed": items.filter(is_completed=True).count(),
        }
    else:
        checklist_data = None

    return {
        "ok": True,
        "month": f"{today.year}-{today.month:02d}",
        "total_expenses_this_month": str(total_expenses),
        "total_income_this_month": str(total_income),
        "active_debts": debts_data,
        "total_debt_balance": str(total_debt),
        "emergency_fund": fund_data,
        "current_month_checklist": checklist_data,
    }


def list_expenses(user, category=None, date_from=None, date_to=None, limit=10, **kwargs):
    qs = Expense.objects.filter(user=user)
    if category:
        qs = qs.filter(category__icontains=category)
    if date_from:
        qs = qs.filter(date__gte=_parse_date(date_from))
    if date_to:
        qs = qs.filter(date__lte=_parse_date(date_to))

    try:
        limit = min(int(limit or 10), 50)
    except (TypeError, ValueError):
        limit = 10

    items = list(qs[:limit])
    return {
        "ok": True,
        "count": len(items),
        "expenses": ExpenseSerializer(items, many=True).data,
    }


def add_expense(user, title, amount, category=None, date=None, notes="", **kwargs):
    try:
        amt = _to_decimal(amount)
    except ValueError as e:
        return {"ok": False, "message": str(e)}

    cat = category if category in EXPENSE_CATEGORIES else "Other"
    exp = Expense.objects.create(
        user=user, title=title, amount=amt, category=cat,
        date=_parse_date(date), notes=notes or "",
    )
    return {
        "ok": True,
        "message": f"Added expense '{exp.title}' — ${exp.amount} ({exp.category}) on {exp.date}.",
        "expense": ExpenseSerializer(exp).data,
    }


def update_expense(user, title, amount=None, category=None, date=None, notes=None, **kwargs):
    expense, error = _find_one(Expense.objects.filter(user=user), "title", title, "expense")
    if error:
        return {"ok": False, "message": error}

    if amount is not None:
        try:
            expense.amount = _to_decimal(amount)
        except ValueError as e:
            return {"ok": False, "message": str(e)}
    if category:
        expense.category = category if category in EXPENSE_CATEGORIES else expense.category
    if date:
        expense.date = _parse_date(date)
    if notes is not None:
        expense.notes = notes
    expense.save()

    return {
        "ok": True,
        "message": f"Updated expense '{expense.title}' — ${expense.amount} ({expense.category}) on {expense.date}.",
        "expense": ExpenseSerializer(expense).data,
    }


def delete_expense(user, title, **kwargs):
    expense, error = _find_one(Expense.objects.filter(user=user), "title", title, "expense")
    if error:
        return {"ok": False, "message": error}
    label = f"{expense.title} (${expense.amount})"
    expense.delete()
    return {"ok": True, "message": f"Deleted expense '{label}'."}


def add_income(user, title, amount, category=None, date=None, notes="", **kwargs):
    try:
        amt = _to_decimal(amount)
    except ValueError as e:
        return {"ok": False, "message": str(e)}

    cat = category if category in INCOME_CATEGORIES else "Other"
    inc = Income.objects.create(
        user=user, title=title, amount=amt, category=cat,
        date=_parse_date(date), notes=notes or "",
    )
    return {
        "ok": True,
        "message": f"Added income '{inc.title}' — ${inc.amount} ({inc.category}) on {inc.date}.",
        "income": IncomeSerializer(inc).data,
    }


def add_debt(user, name, debt_type=None, current_balance=0, annual_interest_rate=0,
             minimum_payment=0, credit_limit=None, due_day=None, notes="", **kwargs):
    try:
        balance = _to_decimal(current_balance, "current_balance")
        apr = _to_decimal(annual_interest_rate, "annual_interest_rate")
        min_pay = _to_decimal(minimum_payment, "minimum_payment")
        limit = _to_decimal(credit_limit, "credit_limit") if credit_limit not in (None, "") else None
    except ValueError as e:
        return {"ok": False, "message": str(e)}

    dtype = debt_type if debt_type in DEBT_TYPES else "credit_card"

    if Debt.objects.filter(user=user, name__iexact=name).exists():
        return {"ok": False, "message": f"A debt named '{name}' already exists. Use update_debt instead."}

    debt = Debt.objects.create(
        user=user, name=name, debt_type=dtype,
        current_balance=balance, annual_interest_rate=apr,
        minimum_payment=min_pay, credit_limit=limit,
        due_day=due_day, notes=notes or "",
    )
    recalculate_avalanche_order(user)
    debt.refresh_from_db()

    return {
        "ok": True,
        "message": f"Added debt '{debt.name}' — ${debt.current_balance} at {debt.annual_interest_rate}% APR.",
        "debt": DebtSerializer(debt).data,
    }


def update_debt(user, name, current_balance=None, annual_interest_rate=None,
                 minimum_payment=None, credit_limit=None, due_day=None,
                 is_active=None, notes=None, **kwargs):
    debt, error = _find_one(Debt.objects.filter(user=user), "name", name, "debt")
    if error:
        return {"ok": False, "message": error}

    try:
        if current_balance is not None:
            debt.current_balance = _to_decimal(current_balance, "current_balance")
        if annual_interest_rate is not None:
            debt.annual_interest_rate = _to_decimal(annual_interest_rate, "annual_interest_rate")
        if minimum_payment is not None:
            debt.minimum_payment = _to_decimal(minimum_payment, "minimum_payment")
        if credit_limit is not None:
            debt.credit_limit = _to_decimal(credit_limit, "credit_limit")
    except ValueError as e:
        return {"ok": False, "message": str(e)}

    if due_day is not None:
        debt.due_day = due_day
    if is_active is not None:
        debt.is_active = bool(is_active)
        if debt.is_active:
            debt.paid_off_date = None
    if notes is not None:
        debt.notes = notes

    debt.save()
    recalculate_avalanche_order(user)
    debt.refresh_from_db()

    return {
        "ok": True,
        "message": f"Updated debt '{debt.name}' — balance now ${debt.current_balance}.",
        "debt": DebtSerializer(debt).data,
    }


def delete_debt(user, name, **kwargs):
    debt, error = _find_one(Debt.objects.filter(user=user), "name", name, "debt")
    if error:
        return {"ok": False, "message": error}
    label = debt.name
    debt.delete()
    recalculate_avalanche_order(user)
    return {"ok": True, "message": f"Deleted debt '{label}'."}


def update_emergency_fund(user, current_balance=None, add_amount=None,
                           target_amount=None, monthly_contribution=None, **kwargs):
    fund, _ = EmergencyFund.objects.get_or_create(user=user)

    try:
        if add_amount is not None:
            fund.current_balance = max(
                Decimal("0.00"), fund.current_balance + _to_decimal(add_amount, "add_amount")
            )
        elif current_balance is not None:
            fund.current_balance = _to_decimal(current_balance, "current_balance")
        if target_amount is not None:
            fund.target_amount = _to_decimal(target_amount, "target_amount")
        if monthly_contribution is not None:
            fund.monthly_contribution = _to_decimal(monthly_contribution, "monthly_contribution")
    except ValueError as e:
        return {"ok": False, "message": str(e)}

    fund.save()

    return {
        "ok": True,
        "message": f"Emergency fund is now ${fund.current_balance} of ${fund.target_amount}.",
        "emergency_fund": EmergencyFundSerializer(fund).data,
    }


def add_checklist_item(user, label, amount=None, category="fixed_expense",
                        due_day=None, debt_name=None, **kwargs):
    today = date.today()
    plan, _ = MonthlyPlan.objects.get_or_create(user=user, year=today.year, month=today.month)

    cat = category if category in CHECKLIST_CATEGORIES else "fixed_expense"

    linked_debt = None
    if debt_name:
        linked_debt, error = _find_one(Debt.objects.filter(user=user), "name", debt_name, "debt")
        if error:
            return {"ok": False, "message": error}

    try:
        amt = _to_decimal(amount, "amount") if amount not in (None, "") else None
    except ValueError as e:
        return {"ok": False, "message": str(e)}

    item = ChecklistItem.objects.create(
        monthly_plan=plan, label=label, amount=amt, due_day=due_day,
        category=cat, linked_debt=linked_debt,
        sort_order=plan.checklist_items.count() + 1,
    )

    return {
        "ok": True,
        "message": f"Added checklist item '{item.label}'.",
        "item": ChecklistItemSerializer(item).data,
    }


def toggle_checklist_item(user, label, **kwargs):
    today = date.today()
    plan = MonthlyPlan.objects.filter(user=user, year=today.year, month=today.month).first()
    if not plan:
        return {"ok": False, "message": "No checklist exists for this month yet. Try generate_checklist first."}

    item, error = _find_one(plan.checklist_items.all(), "label", label, "checklist item")
    if error:
        return {"ok": False, "message": error}

    item, debt, fund = _toggle_checklist_item(item)
    state = "completed" if item.is_completed else "un-completed"

    return {
        "ok": True,
        "message": f"Marked '{item.label}' as {state}.",
        "item": ChecklistItemSerializer(item).data,
        "debt_updated": DebtSerializer(debt).data if debt else None,
        "fund_updated": EmergencyFundSerializer(fund).data if fund else None,
    }


def delete_checklist_item(user, label, **kwargs):
    today = date.today()
    plan = MonthlyPlan.objects.filter(user=user, year=today.year, month=today.month).first()
    if not plan:
        return {"ok": False, "message": "No checklist exists for this month yet."}

    item, error = _find_one(plan.checklist_items.all(), "label", label, "checklist item")
    if error:
        return {"ok": False, "message": error}

    item_label = item.label
    item.delete()
    return {"ok": True, "message": f"Deleted checklist item '{item_label}'."}


def generate_checklist(user, **kwargs):
    try:
        plan, created_items, top_debt = _generate_checklist(user)
    except ValueError as e:
        return {"ok": False, "message": str(e)}

    return {
        "ok": True,
        "message": f"Generated {len(created_items)} checklist items for {plan.year}-{plan.month:02d}.",
        "items": ChecklistItemSerializer(created_items, many=True).data,
    }


def rollover_month(user, **kwargs):
    try:
        new_plan, debts = _rollover_month(user)
    except ValueError as e:
        return {"ok": False, "message": str(e)}

    return {
        "ok": True,
        "message": f"Rolled over to {new_plan.year}-{new_plan.month:02d}.",
        "plan": MonthlyPlanSerializer(new_plan).data,
    }


TOOL_HANDLERS = {
    "get_upcoming_payments": get_upcoming_payments,
    "get_financial_summary": get_financial_summary,
    "list_expenses": list_expenses,
    "add_expense": add_expense,
    "update_expense": update_expense,
    "delete_expense": delete_expense,
    "add_income": add_income,
    "add_debt": add_debt,
    "update_debt": update_debt,
    "delete_debt": delete_debt,
    "update_emergency_fund": update_emergency_fund,
    "add_checklist_item": add_checklist_item,
    "toggle_checklist_item": toggle_checklist_item,
    "delete_checklist_item": delete_checklist_item,
    "generate_checklist": generate_checklist,
    "rollover_month": rollover_month,
}

# Tools whose successful result should be surfaced to the frontend as an
# "action" (so the UI can show a toast / refresh the relevant screen).
MUTATING_TOOLS = {
    "add_expense", "update_expense", "delete_expense",
    "add_income",
    "add_debt", "update_debt", "delete_debt",
    "update_emergency_fund",
    "add_checklist_item", "toggle_checklist_item", "delete_checklist_item",
    "generate_checklist", "rollover_month",
}


# ─────────────────────────────────────────────
# Tool schemas (Claude tool-use format)
# ─────────────────────────────────────────────

TOOL_SCHEMAS = [

    {
        "name": "get_upcoming_payments",
        "description": (
            "Get a list of the user's upcoming payments for this month, including "
            "checklist items that are not yet completed. Returns label, amount, category, "
            "due day, and linked debt (if any)."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    
    {
        "name": "get_financial_summary",
        "description": (
            "Get a snapshot of the user's current finances: this month's total expenses and "
            "income, all active debts with balances/APR, total debt, emergency fund status, "
            "and this month's checklist completion. Use this whenever you need context before "
            "answering a question or before deciding how to act."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_expenses",
        "description": "List the user's recent expenses, optionally filtered by category or date range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Filter by category (partial match ok)."},
                "date_from": {"type": "string", "description": "ISO date (YYYY-MM-DD), inclusive."},
                "date_to": {"type": "string", "description": "ISO date (YYYY-MM-DD), inclusive."},
                "limit": {"type": "integer", "description": "Max results, default 10, max 50."},
            },
        },
    },
    {
        "name": "add_expense",
        "description": (
            "Add a new expense. Use this whenever the user mentions spending money or something "
            "they bought/paid for, even casually phrased (\"I spent $40 on groceries today\", "
            "\"had lunch for 12 bucks\")."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short description, e.g. 'Groceries'."},
                "amount": {"type": "number", "description": "Amount spent, positive number."},
                "category": {
                    "type": "string",
                    "enum": EXPENSE_CATEGORIES,
                    "description": "Closest matching category. Defaults to 'Other' if unsure.",
                },
                "date": {"type": "string", "description": "ISO date (YYYY-MM-DD). Defaults to today."},
                "notes": {"type": "string", "description": "Optional extra detail."},
            },
            "required": ["title", "amount"],
        },
    },
    {
        "name": "update_expense",
        "description": "Update an existing expense, found by matching its title.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Title (or part of it) to find the expense."},
                "amount": {"type": "number"},
                "category": {"type": "string", "enum": EXPENSE_CATEGORIES},
                "date": {"type": "string", "description": "ISO date (YYYY-MM-DD)."},
                "notes": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "delete_expense",
        "description": "Delete an expense, found by matching its title.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Title (or part of it) to find the expense."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "add_income",
        "description": "Add a new income entry (a paycheck, freelance payment, gift, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short description, e.g. 'Paycheck'."},
                "amount": {"type": "number"},
                "category": {"type": "string", "enum": INCOME_CATEGORIES},
                "date": {"type": "string", "description": "ISO date (YYYY-MM-DD). Defaults to today."},
                "notes": {"type": "string"},
            },
            "required": ["title", "amount"],
        },
    },
    {
        "name": "add_debt",
        "description": (
            "Add a new debt (credit card, loan, line of credit). Use this when the user says "
            "something like \"add my Visa, $2000 at 22% interest, minimum $60/month\"."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "e.g. 'CIBC Visa'."},
                "debt_type": {"type": "string", "enum": DEBT_TYPES, "description": "Defaults to 'credit_card'."},
                "current_balance": {"type": "number"},
                "annual_interest_rate": {"type": "number", "description": "APR as a percent, e.g. 21.99."},
                "minimum_payment": {"type": "number", "description": "Minimum monthly payment."},
                "credit_limit": {"type": "number", "description": "Optional, for credit cards."},
                "due_day": {"type": "integer", "description": "Optional day of month payment is due."},
                "notes": {"type": "string"},
            },
            "required": ["name", "current_balance", "annual_interest_rate", "minimum_payment"],
        },
    },
    {
        "name": "update_debt",
        "description": (
            "Update an existing debt, found by matching its name. Use this to update a balance "
            "(\"update my Visa balance to $1800\"), interest rate, minimum payment, or mark it "
            "paid off/active."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name (or part of it) to find the debt."},
                "current_balance": {"type": "number"},
                "annual_interest_rate": {"type": "number"},
                "minimum_payment": {"type": "number"},
                "credit_limit": {"type": "number"},
                "due_day": {"type": "integer"},
                "is_active": {"type": "boolean", "description": "Set false to mark it paid off/closed."},
                "notes": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "delete_debt",
        "description": "Delete a debt entirely, found by matching its name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name (or part of it) to find the debt."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "update_emergency_fund",
        "description": (
            "Update the user's emergency fund. Use add_amount for a relative change "
            "(\"I put $50 into savings\"), or current_balance to set it outright."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "add_amount": {"type": "number", "description": "Amount to add (can be negative to subtract)."},
                "current_balance": {"type": "number", "description": "Set the balance directly."},
                "target_amount": {"type": "number", "description": "Update the savings goal."},
                "monthly_contribution": {"type": "number"},
            },
        },
    },
    {
        "name": "add_checklist_item",
        "description": "Add a new item to this month's checklist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "e.g. 'Pay rent'."},
                "amount": {"type": "number"},
                "category": {"type": "string", "enum": CHECKLIST_CATEGORIES, "description": "Defaults to 'fixed_expense'."},
                "due_day": {"type": "integer"},
                "debt_name": {"type": "string", "description": "If this item relates to a specific debt, its name."},
            },
            "required": ["label"],
        },
    },
    {
        "name": "toggle_checklist_item",
        "description": (
            "Mark a checklist item done (or undo it), found by matching its label. Ticking a "
            "debt payment reduces that debt's balance; ticking a savings item adds to the "
            "emergency fund — this happens automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Label (or part of it) to find the checklist item."},
            },
            "required": ["label"],
        },
    },
    {
        "name": "delete_checklist_item",
        "description": "Delete a checklist item, found by matching its label.",
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Label (or part of it) to find the checklist item."},
            },
            "required": ["label"],
        },
    },
    {
        "name": "generate_checklist",
        "description": (
            "Auto-generate this month's checklist from the user's active debts (avalanche order) "
            "plus an emergency-fund savings item if needed. Replaces any existing debt/savings "
            "items for the current month. Requires at least one active debt."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "rollover_month",
        "description": (
            "Apply this month's interest to all active debts and create next month's plan/"
            "checklist. Use when the user asks to roll over, start a new month, or close out "
            "the current month."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]
