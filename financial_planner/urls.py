from django.urls import path
from . import views

urlpatterns = [
    # ── Debts ──
    path('debts/',              views.DebtListCreateView.as_view(),              name='fp-debt-list'),
    path('debts/<int:pk>/',     views.DebtDetailView.as_view(),                  name='fp-debt-detail'),

    # ── Recurring payments ──
    path('recurring/',          views.RecurringItemListCreateView.as_view(),     name='fp-recurring-list'),
    path('recurring/<int:pk>/', views.RecurringItemDetailView.as_view(),         name='fp-recurring-detail'),

    # ── Emergency Fund ──
    path('emergency-fund/',     views.EmergencyFundView.as_view(),               name='fp-emergency-fund'),

    # ── Monthly Plans ──
    path('plans/',                    views.MonthlyPlanListCreateView.as_view(),      name='fp-plan-list'),
    path('plans/current/',            views.CurrentMonthPlanView.as_view(),           name='fp-plan-current'),
    path('plans/next/',               views.NextMonthPlanView.as_view(),              name='fp-plan-next'),
    path('plans/rollover/',           views.MonthlyRolloverView.as_view(),            name='fp-plan-rollover'),
    path('plans/generate-checklist/', views.GenerateChecklistView.as_view(),          name='fp-generate-checklist'),
    path('plans/<int:pk>/',           views.MonthlyPlanDetailView.as_view(),          name='fp-plan-detail'),

    # ── Investments ──
    path('investments/',                    views.InvestmentListCreateView.as_view(), name='fp-investment-list'),
    path('investments/<int:pk>/',           views.InvestmentDetailView.as_view(),     name='fp-investment-detail'),
    path('investments/<int:pk>/payments/',  views.InvestmentPaymentView.as_view(),    name='fp-investment-payments'),
    path('investments/<int:pk>/payments/<int:payment_id>/', views.InvestmentPaymentView.as_view(),
         name='fp-investment-payment'),
    path('net-worth/',                      views.NetWorthView.as_view(),             name='fp-net-worth'),

    # ── Checklist ──
    path('plans/<int:plan_id>/checklist/', views.ChecklistItemListCreateView.as_view(),  name='fp-checklist-list'),
    path('checklist/<int:pk>/',            views.ChecklistItemDetailView.as_view(),      name='fp-checklist-detail'),
    path('checklist/<int:pk>/toggle/',     views.ChecklistItemToggleView.as_view(),      name='fp-checklist-toggle'),
]
