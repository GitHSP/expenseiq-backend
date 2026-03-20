# expenses/urls.py

from django.urls import path
from . import views

urlpatterns = [
    # Expenses
    path('expenses/',          views.ExpenseListCreateView.as_view(),  name='expense-list'),
    path('expenses/<int:pk>/', views.ExpenseDetailView.as_view(),      name='expense-detail'),

    # Budgets
    path('budgets/',           views.BudgetListView.as_view(),         name='budget-list'),

    # Income
    path('income/',            views.IncomeListCreateView.as_view(),   name='income-list'),
    path('income/<int:pk>/',   views.IncomeDetailView.as_view(),       name='income-detail'),

    # Debts
    path('debts/',             views.DebtListCreateView.as_view(),     name='debt-list'),
    path('debts/<int:pk>/',    views.DebtDetailView.as_view(),         name='debt-detail'),
    path('debts/<int:pk>/paid-off/', views.DebtMarkPaidOffView.as_view(), name='debt-paid-off'),

    # Payment Records
    path('payments/',          views.PaymentRecordListCreateView.as_view(), name='payment-list'),
]