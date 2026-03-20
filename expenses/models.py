# expenses/models.py

from django.db   import models
from django.conf import settings


class Expense(models.Model):
    CATEGORIES = [
        ('Food & Dining',  'Food & Dining'),
        ('Transport',      'Transport'),
        ('Shopping',       'Shopping'),
        ('Entertainment',  'Entertainment'),
        ('Health',         'Health'),
        ('Housing',        'Housing'),
        ('Education',      'Education'),
        ('Other',          'Other'),
    ]
    user       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='expenses')
    title      = models.CharField(max_length=255)
    amount     = models.DecimalField(max_digits=10, decimal_places=2)
    category   = models.CharField(max_length=50, choices=CATEGORIES)
    date       = models.DateField()
    tags       = models.JSONField(default=list, blank=True)
    notes      = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.email} — {self.title} (${self.amount})"


class Budget(models.Model):
    user     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='budgets')
    category = models.CharField(max_length=50)
    amount   = models.DecimalField(max_digits=10, decimal_places=2, default=500)

    class Meta:
        unique_together = ['user', 'category']

    def __str__(self):
        return f"{self.user.email} — {self.category}: ${self.amount}"


class Income(models.Model):
    CATEGORIES = [
        ('Salary',     'Salary'),
        ('Freelance',  'Freelance'),
        ('Investment', 'Investment'),
        ('Business',   'Business'),
        ('Rental',     'Rental'),
        ('Gift',       'Gift'),
        ('Refund',     'Refund'),
        ('Other',      'Other'),
    ]
    user       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='incomes')
    title      = models.CharField(max_length=255)
    amount     = models.DecimalField(max_digits=10, decimal_places=2)
    category   = models.CharField(max_length=50, choices=CATEGORIES)
    date       = models.DateField()
    notes      = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.email} — {self.title} (${self.amount})"


class Debt(models.Model):
    DEBT_TYPES = [
        ('Credit Card',       'Credit Card'),
        ('Personal Loan',     'Personal Loan'),
        ('Car Loan',          'Car Loan'),
        ('Mortgage',          'Mortgage'),
        ('Student Loan',      'Student Loan'),
        ('Buy Now Pay Later', 'Buy Now Pay Later'),
        ('Friends & Family',  'Friends & Family'),
    ]
    user            = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='debts')
    name            = models.CharField(max_length=255)
    type            = models.CharField(max_length=50, choices=DEBT_TYPES)
    balance         = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    original_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    interest_rate   = models.DecimalField(max_digits=5,  decimal_places=2, default=0)
    monthly_payment = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    limit           = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    lender          = models.CharField(max_length=255, blank=True, default='')
    next_payment_date = models.DateField(null=True, blank=True)
    notes           = models.TextField(blank=True, default='')
    is_paid_off     = models.BooleanField(default=False)
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.email} — {self.name} (${self.balance})"


class PaymentRecord(models.Model):
    """
    Records each payment made against a debt
    """
    user       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='payment_records')
    debt       = models.ForeignKey(Debt, on_delete=models.CASCADE, related_name='payments')
    amount     = models.DecimalField(max_digits=10, decimal_places=2)
    date       = models.DateField()
    note       = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.email} — {self.debt.name} payment (${self.amount})"