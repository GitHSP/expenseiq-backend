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

    # A debt the user pays in another currency (e.g. an education loan in
    # INR): the amounts as they typed them. The CAD fields above stay the
    # ones totals and interest use; when those change (interest, a payment,
    # a statement) the originals change by the same proportion.
    original_currency        = models.CharField(max_length=3, blank=True, default="")
    original_current_balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    original_minimum_payment = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    original_credit_limit    = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["avalanche_order"]
        unique_together = ["user", "name"]

    def __str__(self):
        return f"{self.name} — ${self.current_balance}"

    ORIGINAL_FIELDS = ("current_balance", "minimum_payment", "credit_limit")

    def save(self, *args, **kwargs):
        # CAD amounts changed without new originals (interest, a ticked
        # payment, a statement): move the originals by the same proportion.
        if self.pk and self.original_currency and not getattr(self, "_originals_given", False):
            old = type(self).objects.filter(pk=self.pk).values(
                *self.ORIGINAL_FIELDS, *(f"original_{f}" for f in self.ORIGINAL_FIELDS)).first()
            if old:
                rate = (old["original_current_balance"] / old["current_balance"]
                        if old["current_balance"] and old["original_current_balance"] is not None else None)
                touched = []
                for f in self.ORIGINAL_FIELDS:
                    new, was, orig = getattr(self, f), old[f], old[f"original_{f}"]
                    if new == was:
                        continue
                    if new is None:
                        value = None
                    elif was and orig is not None:
                        value = (orig * Decimal(new) / Decimal(was)).quantize(Decimal("0.01"))
                    elif rate is not None:
                        value = (Decimal(new) * rate).quantize(Decimal("0.01"))
                    else:
                        continue
                    setattr(self, f"original_{f}", value)
                    touched.append(f"original_{f}")
                if touched and kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = list(set(kwargs["update_fields"]) | set(touched))
        super().save(*args, **kwargs)

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
    # What the user typed when it wasn"t CAD (e.g. ₹7,000): shown as-is when
    # the app displays that currency. `amount` is always the CAD value.
    original_amount   = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    original_currency = models.CharField(max_length=3, blank=True, default="")
    # How often it comes: monthly on due_day, every 1 or 2 weeks counted
    # from anchor_date (a known payday), or a monthly estimate that varies
    # (tips). The amount is per occurrence (per pay for weekly/biweekly).
    FREQUENCY_CHOICES = [
        ("monthly",  "Monthly"),
        ("biweekly", "Every 2 weeks"),
        ("weekly",   "Weekly"),
        ("varies",   "Varies (monthly estimate)"),
    ]
    frequency   = models.CharField(max_length=10, choices=FREQUENCY_CHOICES, default="monthly")
    anchor_date = models.DateField(null=True, blank=True)
    # Pay that changes every time (hourly work): words from the deposit as the
    # bank prints it. When set, `amount` is kept at the average of the last
    # few matching deposits in the uploaded statements.
    average_from = models.CharField(max_length=100, blank=True, default="")
    average_of   = models.PositiveSmallIntegerField(default=0)    # how many deposits the average used
    # The debt this pays (an education loan's EMI): the checklist item becomes
    # that debt's payment — counted once, and ticking it lowers the balance.
    debt = models.ForeignKey("Debt", on_delete=models.SET_NULL, null=True, blank=True, related_name="recurring_payments")
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
    # What the user typed when it wasn"t CAD (e.g. ₹7,000): shown as-is when
    # the app displays that currency. `amount` is always the CAD value.
    original_amount   = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    original_currency = models.CharField(max_length=3, blank=True, default="")
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


class Investment(models.Model):
    """
    Savings that grow over years: an insurance policy with a cash value, a
    chit, or a recurring deposit. Amounts are kept in the investment's own
    currency (an Indian RD stays in rupees); the app converts for totals.
    The figures it works out (paid so far, maturity, value) are in
    financial_planner/investments.py.
    """

    KIND_CHOICES = [("insurance", "Insurance"), ("chit", "Chit"), ("rd", "Recurring deposit"),
                    ("fd", "Fixed deposit")]
    PAYOUT_CHOICES = [("", "Reinvested (paid at maturity)"), ("monthly", "Paid out monthly"),
                      ("quarterly", "Paid out quarterly"), ("yearly", "Paid out yearly")]
    FREQUENCY_CHOICES = [("monthly", "Monthly"), ("quarterly", "Quarterly"),
                         ("half_yearly", "Half-yearly"), ("yearly", "Yearly")]
    COMPOUNDING_CHOICES = [("quarterly", "Quarterly"), ("monthly", "Monthly"), ("yearly", "Yearly")]
    DEPOSIT_CHOICES = [("", "Not deposited"), ("chit_company", "With the chit company"), ("bank", "Bank FD")]

    user       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="investments")
    kind       = models.CharField(max_length=10, choices=KIND_CHOICES)
    name       = models.CharField(max_length=100)
    provider   = models.CharField(max_length=100, blank=True, default="")    # iA, Equitable, KSFE, SBI...
    currency   = models.CharField(max_length=3, default="CAD")
    start_date = models.DateField()
    # Planner's recurring payment for it (premium, instalment), when there is one.
    recurring_item = models.ForeignKey(RecurringItem, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name="investments")

    # Premium (insurance) or monthly instalment (chit, RD), and how often it's paid.
    payment           = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    payment_frequency = models.CharField(max_length=12, choices=FREQUENCY_CHOICES, default="monthly")
    rate              = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("0.000"))  # % a year

    # Insurance
    policy_type    = models.CharField(max_length=50, blank=True, default="")    # universal life, endowment...
    paying_years   = models.PositiveSmallIntegerField(null=True, blank=True)   # years premiums are paid
    term_years     = models.PositiveSmallIntegerField(null=True, blank=True)   # years until it matures
    sum_assured    = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    current_value  = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)  # cash value
    current_value_date = models.DateField(null=True, blank=True)
    maturity_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)  # as quoted
    # From the yearly statement: what comes out of the fund each year (cost of
    # insurance + policy fee + premium tax), and the charge for cancelling now.
    yearly_cost      = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    surrender_charge = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    # Chit (`months` is also an RD's tenure)
    months       = models.PositiveSmallIntegerField(null=True, blank=True)
    chit_value   = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    prized       = models.BooleanField(default=False)
    prized_date  = models.DateField(null=True, blank=True)
    prize_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    deposit_where  = models.CharField(max_length=12, choices=DEPOSIT_CHOICES, blank=True, default="")
    deposit_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    deposit_rate   = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    deposit_start  = models.DateField(null=True, blank=True)

    # RD and FD (an FD's amount deposited is `payment`, its tenure `months`)
    compounding = models.CharField(max_length=10, choices=COMPOUNDING_CHOICES, default="quarterly")
    interest_payout = models.CharField(max_length=10, choices=PAYOUT_CHOICES, blank=True, default="")

    is_active  = models.BooleanField(default=True)
    notes      = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind", "start_date", "name"]

    def __str__(self):
        return f"{self.name} ({self.get_kind_display()})"


class InvestmentPayment(models.Model):
    """One instalment actually paid — a chit's, which changes every month."""

    investment = models.ForeignKey(Investment, on_delete=models.CASCADE, related_name="payments")
    date       = models.DateField()
    amount     = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))])
    note       = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["date", "id"]
