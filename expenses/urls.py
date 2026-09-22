from django.urls import path
from . import views

urlpatterns = [
    path('expenses/',          views.ExpenseListCreateView.as_view(),  name='expense-list'),
    path('expenses/<int:pk>/', views.ExpenseDetailView.as_view(),      name='expense-detail'),
    path('income/',            views.IncomeListCreateView.as_view(),   name='income-list'),
    path('income/<int:pk>/',   views.IncomeDetailView.as_view(),       name='income-detail'),
]