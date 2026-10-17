# expenses/models.py

from django.db   import models
from django.conf import settings


class Expense(models.Model):
    CATEGORIES = [
        ('Food & Dining',          'Food & Dining'),
        ('Transport',              'Transport'),
        ('Shopping',               'Shopping'),
        ('Entertainment',          'Entertainment'),
        ('Health',                 'Health'),
        ('Housing',                'Housing'),
        ('Education',              'Education'),
        ('Gym & Fitness',          'Gym & Fitness'),
        ('Subscriptions',          'Subscriptions'),
        ('Insurance',              'Insurance'),
        ('Clothing & Shopping',    'Clothing & Shopping'),
        ('Personal Care & Beauty', 'Personal Care & Beauty'),
        ('Gifts & Donations',      'Gifts & Donations'),
        ('Bills & Utilities',      'Bills & Utilities'),
        ('Medical & Health',       'Medical & Health'),
        ('Education & Books',      'Education & Books'),
        ('Abroad Expense',         'Abroad Expense'),
        ('Other',                  'Other'),
        ('Immigration',            'Immigration'),

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

# ─────────────────────────────────────────────
# Bank accounts and their statements
# Every uploaded statement is kept with all of its rows — including transfers
# and card payments that aren't saved as expenses — so the Accounts page can
# show each account's full history, which months are covered, and recurring
# charges like pre-authorized debits.
# ─────────────────────────────────────────────

class Account(models.Model):
    KINDS = [
        ('chequing',    'Chequing'),
        ('savings',     'Savings'),
        ('credit_card', 'Credit card'),
        ('other',       'Other'),
    ]
    user        = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='bank_accounts')
    name        = models.CharField(max_length=100)
    kind        = models.CharField(max_length=20, choices=KINDS, default='other')
    institution = models.CharField(max_length=50, blank=True, default='')
    last4       = models.CharField(max_length=4, blank=True, default='')
    # A card's Planner debt, kept up to date from its newest statement.
    debt        = models.ForeignKey('financial_planner.Debt', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='statement_accounts')
    debt_synced_through = models.DateField(null=True, blank=True)    # period end of the statement last applied
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['kind', 'name']
        constraints = [models.UniqueConstraint(fields=['user', 'name'], name='unique_account_name_per_user')]

    def __str__(self):
        return f"{self.user.email} — {self.name}"


class Statement(models.Model):
    user         = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='statements')
    account      = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='statements')
    filename     = models.CharField(max_length=255)
    file_hash    = models.CharField(max_length=64, blank=True, default='', db_index=True)
    period_start = models.DateField()
    period_end   = models.DateField()
    # Figures printed on the statement (credit cards mostly), when found.
    previous_balance = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    new_balance      = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    minimum_payment  = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    payment_due      = models.DateField(null=True, blank=True)
    credit_limit     = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    uploaded_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-period_end', '-id']

    def __str__(self):
        return f"{self.account.name} {self.period_start} – {self.period_end}"


class StatementTransaction(models.Model):
    KINDS = [
        ('expense',   'Saved as an expense'),
        ('income',    'Saved as income'),
        ('transfer',  'Transfer between own accounts'),
        ('duplicate', 'Already recorded'),
        ('skipped',   'Not saved'),
    ]
    user        = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='statement_transactions')
    statement   = models.ForeignKey(Statement, on_delete=models.CASCADE, related_name='transactions')
    account     = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='transactions')
    date        = models.DateField()
    description = models.CharField(max_length=255)          # as printed by the bank
    title       = models.CharField(max_length=255)          # cleaned up / renamed
    amount      = models.DecimalField(max_digits=12, decimal_places=2)   # negative = money out of this account
    kind        = models.CharField(max_length=20, choices=KINDS, default='skipped')
    category    = models.CharField(max_length=50, blank=True, default='')
    expense     = models.ForeignKey(Expense, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    income      = models.ForeignKey(Income, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-date', 'id']
        indexes = [models.Index(fields=['account', 'date'])]

    def __str__(self):
        return f"{self.date} {self.title} {self.amount}"
