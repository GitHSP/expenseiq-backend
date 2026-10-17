from django.urls import path
from . import account_views, views

urlpatterns = [
    path('expenses/',          views.ExpenseListCreateView.as_view(),  name='expense-list'),
    path('expenses/<int:pk>/', views.ExpenseDetailView.as_view(),      name='expense-detail'),
    path('income/',            views.IncomeListCreateView.as_view(),   name='income-list'),
    path('income/<int:pk>/',   views.IncomeDetailView.as_view(),       name='income-detail'),
    path('expenses/bulk-delete/', views.BulkDeleteView.as_view(kind="expense"), name='expense-bulk-delete'),
    path('income/bulk-delete/',   views.BulkDeleteView.as_view(kind="income"),  name='income-bulk-delete'),
    path('statements/preview/', views.StatementPreviewView.as_view(),  name='statement-preview'),
    path('statements/import/',  views.StatementImportView.as_view(),   name='statement-import'),
    path('statements/accounts/',          account_views.AccountListView.as_view(),         name='account-list'),
    path('statements/accounts/<int:pk>/', account_views.AccountDetailView.as_view(),       name='account-detail'),
    path('statements/transactions/',      account_views.AccountTransactionsView.as_view(), name='account-transactions'),
    path('statements/recurring/',         account_views.RecurringChargesView.as_view(),    name='recurring-charges'),
    path('statements/<int:pk>/',          account_views.StatementDetailView.as_view(),     name='statement-detail'),
]