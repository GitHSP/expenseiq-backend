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