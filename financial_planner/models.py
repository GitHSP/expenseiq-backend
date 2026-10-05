from django.db import models
from django.core.validators import MinValueValidator
from decimal import Decimal
from django.conf import settings

class Debt(models.Model):
    """
    Represents a single debt account (credit card, loan, etc.)
    """

    DEBT_TYPE_CHOICES = [
        ("credit_card", "Credit Card"),
        ("loan", "Loan"),
        ("line_of_credit", "Line of Credit"),
        ("other", "Other"),
    ]


    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="debts")
    name = models.CharField(max_length=100)  # e.g. "CIBC Visa"
    debt_type = models.CharField(max_length=20, choices=DEBT_TYPE_CHOICES, default="credit_card")
    current_balance = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))])
    annual_interest_rate = models.DecimalField(max_digits=5, decimal_places=2)  # e.g. 21.99
    minimum_payment = models.DecimalField(max_digits=8, decimal_places=2)
    credit_limit = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)  # for credit cards
    due_day = models.PositiveSmallIntegerField(null=True, blank=True)  # day of month payment is due
    avalanche_order = models.PositiveSmallIntegerField(default=1)  # 1 = highest priority
    is_active = models.BooleanField(default=True)
    paid_off_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["avalanche_order"]
        unique_together = ["user", "name"]

    def __str__(self):
        return f"{self.name} — ${self.current_balance}"

    @property
    def monthly_interest_rate(self):
        return self.annual_interest_rate / Decimal("12") / Decimal("100")

    @property
    def utilization_percent(self):
        if self.credit_limit and self.credit_limit > 0:
            return round((self.current_balance / self.credit_limit) * 100, 1)
        return None

    @property
    def monthly_interest_amount(self):
        """Interest charged this month before payment."""
        return round(self.current_balance * self.monthly_interest_rate, 2)


class EmergencyFund(models.Model):
    """
    Tracks the emergency fund balance over time.
    Target: $1,000. Once hit, all bonus income goes to CIBC (avalanche target).
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="emergency_fund")
    current_balance = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    target_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("1000.00"))
    monthly_contribution = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("100.00"))

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Emergency Fund — ${self.current_balance} / ${self.target_amount}"

    @property
    def is_funded(self):
        return self.current_balance >= self.target_amount

    @property
    def progress_percent(self):
        if self.target_amount > 0:
            return min(round((self.current_balance / self.target_amount) * 100, 1), 100)
        return 0


class MonthlyPlan(models.Model):
    """
    The master plan for a given month.
    Stores income, total expenses and surplus. Groups the monthly checklist.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="monthly_plans")
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()  # 1–12

    freelance_income = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    # Summary totals (editable — no longer auto-derived from a paycheck breakdown)
    total_income = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    total_fixed_expenses = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    total_debt_payments = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    surplus_to_avalanche = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    emergency_fund_contribution = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("100.00"))

    # Status
    is_finalized = models.BooleanField(default=False)  # locked once month is over
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["user", "year", "month"]
        ordering = ["year", "month"]

    def __str__(self):
        return f"{self.year}-{self.month:02d} Plan"

    @property
    def effective_income(self):
        return self.total_income + self.freelance_income


class RecurringItem(models.Model):
    """
    A payment (or income source) that repeats every month — rent, bills,
    transfers, salary. Each month's checklist is built from these plus the
    user's active debts, so edits here carry into every future month.
    """

    CATEGORY_CHOICES = [
        ("fixed_expense", "Fixed Expense"),
        ("temp_payment", "Temporary Payment"),
        ("auto_debit", "Auto Debit"),
        ("transfer", "Transfer"),
        ("income", "Income"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recurring_items")
    label = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))])
    due_day = models.PositiveSmallIntegerField(null=True, blank=True)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="fixed_expense")
    is_auto_debit = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["due_day", "label"]

    def __str__(self):
        return f"{self.label} ({self.amount})"


class ChecklistItem(models.Model):
    """
    Interactive monthly checklist item (☐ → ✅).
    Each item belongs to a MonthlyPlan and can optionally link to a Debt.
    """

    CATEGORY_CHOICES = [
        ("savings", "Savings"),
        ("debt_min", "Debt Minimum"),
        ("debt_extra", "Debt Extra Payment"),
        ("fixed_expense", "Fixed Expense"),
        ("temp_payment", "Temporary Payment"),
        ("auto_debit", "Auto Debit ⚠️"),
        ("transfer", "Transfer"),
        ("income", "Income Received"),
    ]

    monthly_plan = models.ForeignKey(MonthlyPlan, on_delete=models.CASCADE, related_name="checklist_items")

    label = models.CharField(max_length=200)  # e.g. "Pay CIBC minimum $71.03"
    amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    due_day = models.PositiveSmallIntegerField(null=True, blank=True)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    is_auto_debit = models.BooleanField(default=False)  # triggers ⚠️ warning
    is_completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    linked_debt = models.ForeignKey(
        Debt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checklist_items",
    )

    # Set when the item was generated from a RecurringItem, so the month's
    # checklist can be re-synced after the recurring list changes.
    recurring_item = models.ForeignKey(
        RecurringItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checklist_items",
    )

    class Meta:
        ordering = ["sort_order", "due_day"]

    def __str__(self):
        status = "✅" if self.is_completed else "☐"
        return f"{status} {self.label}"
