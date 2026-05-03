"""
Seed script for Financial Planner — Sarath's data
Run: python manage.py shell < seed_financial_planner.py

Place this file in:S:/Freelance/ExpenseIQ/backend/
"""
import os
import django
from decimal import Decimal
from datetime import date

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model
from financial_planner.models import (
    Debt, EmergencyFund, MonthlyPlan, ChecklistItem
)

User = get_user_model()

# ─────────────────────────────────────────────
# CONFIG — change email to your login email
# ─────────────────────────────────────────────
USER_EMAIL = input("Enter your login email: ").strip()

try:
    user = User.objects.get(email=USER_EMAIL)
    print(f"✅ Found user: {user.email}")
except User.DoesNotExist:
    print(f"❌ No user found with email: {USER_EMAIL}")
    print("Available users:", list(User.objects.values_list("email", flat=True)))
    exit()

# ─────────────────────────────────────────────
# STEP 1 — Clear existing data for this user
# ─────────────────────────────────────────────
print("\n🗑  Clearing existing financial planner data...")
Debt.objects.filter(user=user).delete()
MonthlyPlan.objects.filter(user=user).delete()
EmergencyFund.objects.filter(user=user).delete()
print("✅ Cleared!")

# ─────────────────────────────────────────────
# STEP 2 — Create Debts (Avalanche order)
# Sorted by: highest APR first
# RBC#1 (21.99%) → CIBC (21.99%) → RBC#2 (21.99%) → Scotia (13.99%) → MBNA (12.99%) → Edu (9.9%)
# ─────────────────────────────────────────────
print("\n💳 Creating debts...")

DEBTS = [
    {
        "name":                "RBC Visa #1",
        "debt_type":           "credit_card",
        "current_balance":     Decimal("1773.69"),   # closing balance after May payment
        "annual_interest_rate":Decimal("21.99"),
        "minimum_payment":     Decimal("40.00"),
        "credit_limit":        Decimal("2000.00"),
        "due_day":             12,
        "avalanche_order":     1,
        "notes":               "🎯 Avalanche target — pay min + all surplus here",
    },
    {
        "name":                "CIBC Visa",
        "debt_type":           "credit_card",
        "current_balance":     Decimal("3494.14"),
        "annual_interest_rate":Decimal("21.99"),
        "minimum_payment":     Decimal("70.00"),
        "credit_limit":        Decimal("3500.00"),
        "due_day":             13,
        "avalanche_order":     2,
        "notes":               "Pay 5 days early → by 8th",
    },
    {
        "name":                "RBC Visa #2",
        "debt_type":           "credit_card",
        "current_balance":     Decimal("7487.44"),
        "annual_interest_rate":Decimal("21.99"),
        "minimum_payment":     Decimal("150.00"),
        "credit_limit":        Decimal("7500.00"),
        "due_day":             13,
        "avalanche_order":     3,
        "notes":               "Pay 5 days early → by 8th",
    },
    {
        "name":                "Scotia Visa",
        "debt_type":           "credit_card",
        "current_balance":     Decimal("4958.29"),
        "annual_interest_rate":Decimal("13.99"),
        "minimum_payment":     Decimal("100.00"),
        "credit_limit":        Decimal("5000.00"),
        "due_day":             6,
        "avalanche_order":     4,
        "notes":               "Pay 5 days early → by 1st",
    },
    {
        "name":                "MBNA Mastercard",
        "debt_type":           "credit_card",
        "current_balance":     Decimal("985.82"),
        "annual_interest_rate":Decimal("12.99"),
        "minimum_payment":     Decimal("25.00"),
        "credit_limit":        Decimal("1000.00"),
        "due_day":             8,
        "avalanche_order":     5,
        "notes":               "Pay 5 days early → by 3rd",
    },
    {
        "name":                "Education Loan",
        "debt_type":           "loan",
        "current_balance":     Decimal("24706.25"),
        "annual_interest_rate":Decimal("9.90"),
        "minimum_payment":     Decimal("500.00"),
        "credit_limit":        None,
        "due_day":             25,
        "avalanche_order":     6,
        "notes":               "🎓 Set aside in parts from each paycheck",
    },
]

created_debts = {}
for d in DEBTS:
    debt, created = Debt.objects.get_or_create(
        user=user,
        name=d["name"],
        defaults=d,
    )
    created_debts[d["name"]] = debt
    status = "created" if created else "already exists"
    print(f"  {'✅' if created else '⚠️ '} {d['name']} — ${d['current_balance']} @ {d['annual_interest_rate']}% ({status})")

# ─────────────────────────────────────────────
# STEP 3 — Emergency Fund
# ─────────────────────────────────────────────
print("\n🛡️  Creating emergency fund...")

fund, created = EmergencyFund.objects.get_or_create(
    user=user,
    defaults={
        "current_balance":     Decimal("0.00"),
        "target_amount":       Decimal("1000.00"),
        "monthly_contribution":Decimal("100.00"),
    }
)
print(f"  {'✅ Created' if created else '⚠️  Already exists'} — Target: $1,000")

# ─────────────────────────────────────────────
# STEP 4 — Monthly Plan (May 2026)
# ─────────────────────────────────────────────
print("\n📅 Creating May 2026 monthly plan...")

plan, created = MonthlyPlan.objects.get_or_create(
    user=user,
    year=2026,
    month=5,
    defaults={
        "biweekly_income":           Decimal("1100.00"),
        "second_job_income":         Decimal("925.00"),  # $625 + ~$300 tips
        "freelance_income":          Decimal("0.00"),
        "num_paychecks":             3,                  # May is a 3-paycheck month
        "is_three_paycheck_month":   True,
        "total_income":              Decimal("4225.00"), # 3 × $1100 + $925
        "total_fixed_expenses":      Decimal("2845.00"),
        "total_debt_payments":       Decimal("885.00"),  # all minimums
        "surplus_to_avalanche":      Decimal("222.96"),  # goes to RBC#1
        "emergency_fund_contribution":Decimal("100.00"),
        "notes":                     "3-paycheck month. Bonus: 20% savings, 80% to RBC#1 avalanche.",
    }
)
print(f"  {'✅ Created' if created else '⚠️  Already exists'} — May 2026 (3-paycheck month)")

# ─────────────────────────────────────────────
# STEP 5 — Checklist Items
# Mapped to correct categories
# ─────────────────────────────────────────────
print("\n☑️  Creating checklist items...")

CHECKLIST = [
    # ── Savings ──
    {
        "label":         "Emergency Savings 🏦 — transfer FIRST on pay day",
        "amount":        Decimal("100.00"),
        "due_day":       1,
        "category":      "savings",
        "is_auto_debit": False,
        "sort_order":    1,
        "linked_debt":   None,
    },

    # ── Auto Debits ⚠️ ──
    {
        "label":         "⚠️ Insurance — AUTO DEBIT (funds must be ready!)",
        "amount":        Decimal("85.00"),
        "due_day":       10,
        "category":      "auto_debit",
        "is_auto_debit": True,
        "sort_order":    2,
        "linked_debt":   None,
    },
    {
        "label":         "⚠️ Gym Fee — AUTO DEBIT",
        "amount":        Decimal("18.00"),
        "due_day":       17,
        "category":      "auto_debit",
        "is_auto_debit": True,
        "sort_order":    3,
        "linked_debt":   None,
    },
    {
        "label":         "⚠️ Bank Charge (2nd account) — AUTO DEBIT",
        "amount":        Decimal("5.00"),
        "due_day":       20,
        "category":      "auto_debit",
        "is_auto_debit": True,
        "sort_order":    4,
        "linked_debt":   None,
    },
    {
        "label":         "⚠️ Bank Charge (main account) — AUTO DEBIT last day",
        "amount":        Decimal("17.00"),
        "due_day":       31,
        "category":      "auto_debit",
        "is_auto_debit": True,
        "sort_order":    5,
        "linked_debt":   None,
    },

    # ── Debt Minimums ──
    {
        "label":         "Scotia Visa minimum payment (pay by 1st)",
        "amount":        Decimal("100.00"),
        "due_day":       6,
        "category":      "debt_min",
        "is_auto_debit": False,
        "sort_order":    6,
        "linked_debt":   created_debts.get("Scotia Visa"),
    },
    {
        "label":         "MBNA Mastercard minimum (pay by 3rd)",
        "amount":        Decimal("25.00"),
        "due_day":       8,
        "category":      "debt_min",
        "is_auto_debit": False,
        "sort_order":    7,
        "linked_debt":   created_debts.get("MBNA Mastercard"),
    },
    {
        "label":         "RBC Visa #1 minimum payment",
        "amount":        Decimal("40.00"),
        "due_day":       12,
        "category":      "debt_min",
        "is_auto_debit": False,
        "sort_order":    8,
        "linked_debt":   created_debts.get("RBC Visa #1"),
    },
    {
        "label":         "CIBC Visa minimum (pay by 8th)",
        "amount":        Decimal("70.00"),
        "due_day":       13,
        "category":      "debt_min",
        "is_auto_debit": False,
        "sort_order":    9,
        "linked_debt":   created_debts.get("CIBC Visa"),
    },
    {
        "label":         "RBC Visa #2 minimum (pay by 8th)",
        "amount":        Decimal("150.00"),
        "due_day":       13,
        "category":      "debt_min",
        "is_auto_debit": False,
        "sort_order":    10,
        "linked_debt":   created_debts.get("RBC Visa #2"),
    },
    {
        "label":         "Education Loan payment 🎓",
        "amount":        Decimal("500.00"),
        "due_day":       25,
        "category":      "debt_min",
        "is_auto_debit": False,
        "sort_order":    11,
        "linked_debt":   created_debts.get("Education Loan"),
    },

    # ── Avalanche Extra Payment ──
    {
        "label":         "🎯 RBC Visa #1 — EXTRA avalanche payment (all surplus)",
        "amount":        Decimal("222.96"),
        "due_day":       12,
        "category":      "debt_extra",
        "is_auto_debit": False,
        "sort_order":    12,
        "linked_debt":   created_debts.get("RBC Visa #1"),
    },

    # ── Temp Payments ──
    {
        "label":         "Laptop EMI ⏳ (ends July 2026 — month 7/7)",
        "amount":        Decimal("110.00"),
        "due_day":       8,
        "category":      "temp_payment",
        "is_auto_debit": False,
        "sort_order":    13,
        "linked_debt":   None,
    },
    {
        "label":         "Friend's Flight ✈️ (9 months from May 2026)",
        "amount":        Decimal("122.00"),
        "due_day":       10,
        "category":      "temp_payment",
        "is_auto_debit": False,
        "sort_order":    14,
        "linked_debt":   None,
    },
    {
        "label":         "Mobile EMI 📱 (20 months from May 2026)",
        "amount":        Decimal("47.04"),
        "due_day":       28,
        "category":      "temp_payment",
        "is_auto_debit": False,
        "sort_order":    15,
        "linked_debt":   None,
    },

    # ── Fixed Expenses ──
    {
        "label":         "Rent 🏠 (pay by 26th)",
        "amount":        Decimal("650.00"),
        "due_day":       31,
        "category":      "fixed_expense",
        "is_auto_debit": False,
        "sort_order":    16,
        "linked_debt":   None,
    },
    {
        "label":         "Transit Pass 🚌 (buy by 29th)",
        "amount":        Decimal("158.00"),
        "due_day":       29,
        "category":      "fixed_expense",
        "is_auto_debit": False,
        "sort_order":    17,
        "linked_debt":   None,
    },
    {
        "label":         "Mobile Bill (pay by 21st)",
        "amount":        Decimal("30.00"),
        "due_day":       26,
        "category":      "fixed_expense",
        "is_auto_debit": False,
        "sort_order":    18,
        "linked_debt":   None,
    },
    {
        "label":         "Groceries 🛒 (budget for full month)",
        "amount":        Decimal("200.00"),
        "due_day":       15,
        "category":      "fixed_expense",
        "is_auto_debit": False,
        "sort_order":    19,
        "linked_debt":   None,
    },
    {
        "label":         "Utilities 💡",
        "amount":        Decimal("40.00"),
        "due_day":       15,
        "category":      "fixed_expense",
        "is_auto_debit": False,
        "sort_order":    20,
        "linked_debt":   None,
    },

    # ── Transfers ──
    {
        "label":         "Extra India Transfer 🇮🇳 ₹11k = ~$157 CAD (before 15th)",
        "amount":        Decimal("157.00"),
        "due_day":       14,
        "category":      "transfer",
        "is_auto_debit": False,
        "sort_order":    21,
        "linked_debt":   None,
    },
    {
        "label":         "Abroad Transfer 🌏 Regular monthly family transfer",
        "amount":        Decimal("500.00"),
        "due_day":       28,
        "category":      "transfer",
        "is_auto_debit": False,
        "sort_order":    22,
        "linked_debt":   None,
    },
]

# Delete existing checklist for this plan
ChecklistItem.objects.filter(monthly_plan=plan).delete()

for item in CHECKLIST:
    ChecklistItem.objects.create(
        monthly_plan=plan,
        **item,
    )
    auto = "⚠️ AUTO" if item["is_auto_debit"] else "   "
    print(f"  ✅ {auto} [{item['category']:14}] {item['label'][:50]} — ${item['amount']}")

# ─────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────
print("\n" + "="*60)
print("🎉 SEED COMPLETE!")
print("="*60)
print(f"  💳 Debts created:          {Debt.objects.filter(user=user).count()}")
print(f"  🛡️  Emergency fund:         ready")
print(f"  📅 Monthly plan:           May 2026")
print(f"  ☑️  Checklist items:        {ChecklistItem.objects.filter(monthly_plan=plan).count()}")
print(f"\n  Total debt:                $44,000.00")
print(f"  Monthly minimums:          $885.00")
print(f"  Avalanche extra (RBC#1):   $222.96")
print(f"  Auto debits to watch:      Insurance, Gym, Bank×2")
print("="*60)
print("\n✅ Now open the Financial Planner tab in ExpenseIQ!")
