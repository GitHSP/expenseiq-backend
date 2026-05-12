from django.db import models
from django.core.validators import MinValueValidator
from decimal import Decimal
from django.conf import settings

class Debt(models.Model):
    """
    Represents a single debt account (credit card, loan, etc.)
    Tracks live balance — updated each month via MonthlyDebtSnapshot.
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
    Stores income, total expenses, surplus, and freelance income.
    Generated automatically by the avalanche simulation engine.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="monthly_plans")
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()  # 1–12

    # Income
    biweekly_income = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("1100.00"))
    second_job_income = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("925.00"))
    freelance_income = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    num_paychecks = models.PositiveSmallIntegerField(default=2)  # 2 or 3
    is_three_paycheck_month = models.BooleanField(default=False)

    # Computed totals (filled by plan generator)
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


class PaycheckAllocation(models.Model):
    """
    Breaks down each paycheck within a month — what gets paid from which paycheck.
    Linked to MonthlyPlan. Supports the paycheck-by-paycheck view.
    """

    PAYCHECK_SOURCE_CHOICES = [
        ("biweekly", "Biweekly Job"),
        ("second_job", "2nd Job + Tips"),
        ("freelance", "Freelance"),
    ]

    monthly_plan = models.ForeignKey(MonthlyPlan, on_delete=models.CASCADE, related_name="paycheck_allocations")
    paycheck_number = models.PositiveSmallIntegerField()  # 1, 2, or 3 within the month
    paycheck_date = models.DateField()
    source = models.CharField(max_length=20, choices=PAYCHECK_SOURCE_CHOICES)
    gross_amount = models.DecimalField(max_digits=10, decimal_places=2)

    # Allocations from this paycheck
    allocated_to_savings = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    allocated_to_debts = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    allocated_to_expenses = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    allocated_to_avalanche = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    remaining = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["paycheck_date"]
        unique_together = ["monthly_plan", "paycheck_number", "source"]

    def __str__(self):
        return f"{self.monthly_plan} — Paycheck #{self.paycheck_number} ({self.source})"


class ChecklistItem(models.Model):
    """
    Interactive monthly checklist item (☐ → ✅).
    Each item belongs to a MonthlyPlan and optionally ties to a PaycheckAllocation.
    Grouped by paycheck for the UI.
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
    paycheck_allocation = models.ForeignKey(
        PaycheckAllocation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checklist_items",
    )

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

    class Meta:
        ordering = ["sort_order", "due_day"]

    def __str__(self):
        status = "✅" if self.is_completed else "☐"
        return f"{status} {self.label}"


class MonthlyDebtSnapshot(models.Model):
    """
    Records the state of each debt at the start and end of each month.
    Used for: debt tracker table (opening → interest → payment → closing),
    avalanche projection, and July audit mode.
    """

    monthly_plan = models.ForeignKey(MonthlyPlan, on_delete=models.CASCADE, related_name="debt_snapshots")
    debt = models.ForeignKey(Debt, on_delete=models.CASCADE, related_name="snapshots")

    opening_balance = models.DecimalField(max_digits=10, decimal_places=2)
    interest_charged = models.DecimalField(max_digits=10, decimal_places=2)  # opening × (rate/12)
    balance_after_interest = models.DecimalField(max_digits=10, decimal_places=2)
    payment_made = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    extra_payment = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))  # avalanche top-up
    closing_balance = models.DecimalField(max_digits=10, decimal_places=2)

    # Audit fields
    planned_payment = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    actual_payment = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)  # filled in July audit
    is_paid_off_this_month = models.BooleanField(default=False)

    class Meta:
        unique_together = ["monthly_plan", "debt"]
        ordering = ["debt__avalanche_order"]

    def __str__(self):
        return f"{self.monthly_plan} — {self.debt.name}: ${self.opening_balance} → ${self.closing_balance}"

    def save(self, *args, **kwargs):
        # Auto-compute derived fields before saving
        self.balance_after_interest = self.opening_balance + self.interest_charged
        self.closing_balance = max(
            self.balance_after_interest - self.payment_made - self.extra_payment,
            Decimal("0.00"),
        )
        self.is_paid_off_this_month = self.closing_balance == Decimal("0.00") and self.opening_balance > 0
        super().save(*args, **kwargs)

class PaycheckConfig(models.Model):
    """
    Stores user's paycheck configuration.
    Used to auto-calculate pay dates each month.
    """
    user               = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paycheck_config"
    )
    first_pay_date     = models.DateField()
    biweekly_amount    = models.DecimalField(max_digits=10, decimal_places=2, default=1100)
    second_job_day     = models.PositiveSmallIntegerField(default=20)
    second_job_amount  = models.DecimalField(max_digits=10, decimal_places=2, default=625)
    second_job_tips    = models.DecimalField(max_digits=10, decimal_places=2, default=300)
    extra_payment      = models.DecimalField(max_digits=10, decimal_places=2, default=150)
    created_at         = models.DateTimeField(auto_now_add=True)
    updated_at         = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.email} — Pay config"

    def get_pay_dates_for_month(self, year, month):
        from datetime import timedelta
        import calendar

        last_day   = calendar.monthrange(year, month)[1]
        month_end  = self.first_pay_date.replace(year=year, month=month, day=last_day)
        month_start= self.first_pay_date.replace(year=year, month=month, day=1)

        pay_dates = []

        # Go forward from anchor
        current = self.first_pay_date
        while current <= month_end:
            if current.year == year and current.month == month:
                pay_dates.append(current)
            current = current + timedelta(days=14)

        # Go backward from anchor
        current = self.first_pay_date - timedelta(days=14)
        while current >= month_start:
            if current.year == year and current.month == month:
                pay_dates.append(current)
            current = current - timedelta(days=14)

        return sorted(set(pay_dates))

    def get_second_job_date_for_month(self, year, month):
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        day      = min(self.second_job_day, last_day)
        return self.first_pay_date.replace(year=year, month=month, day=day)