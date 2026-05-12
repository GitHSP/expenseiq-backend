from django.urls import path
from . import views

urlpatterns = [
    # ── Debts ──
    path('debts/',              views.DebtListCreateView.as_view(),              name='fp-debt-list'),
    path('debts/<int:pk>/',     views.DebtDetailView.as_view(),                  name='fp-debt-detail'),

    # ── Emergency Fund ──
    path('emergency-fund/',     views.EmergencyFundView.as_view(),               name='fp-emergency-fund'),

    # ── Paycheck Config ──
    path('paycheck-config/',    views.PaycheckConfigView.as_view(),              name='fp-paycheck-config'),

    # ── Paycheck Calculate ──
    path('paychecks/calculate/',views.PaycheckAllocationCalculateView.as_view(), name='fp-paycheck-calculate'),

    # ── Monthly Plans ──
    path('plans/',                    views.MonthlyPlanListCreateView.as_view(),      name='fp-plan-list'),
    path('plans/current/',            views.CurrentMonthPlanView.as_view(),           name='fp-plan-current'),
    path('plans/rollover/',           views.MonthlyRolloverView.as_view(),            name='fp-plan-rollover'),
    path('plans/generate-checklist/', views.GenerateChecklistView.as_view(),          name='fp-generate-checklist'),
    path('plans/<int:pk>/',           views.MonthlyPlanDetailView.as_view(),          name='fp-plan-detail'),

    # ── Checklist ──
    path('plans/<int:plan_id>/checklist/', views.ChecklistItemListCreateView.as_view(),  name='fp-checklist-list'),
    path('checklist/<int:pk>/',            views.ChecklistItemDetailView.as_view(),      name='fp-checklist-detail'),
    path('checklist/<int:pk>/toggle/',     views.ChecklistItemToggleView.as_view(),      name='fp-checklist-toggle'),

    # ── Paycheck Allocations ──
    path('plans/<int:plan_id>/paychecks/', views.PaycheckAllocationListCreateView.as_view(), name='fp-paycheck-list'),
    path('paychecks/<int:pk>/',            views.PaycheckAllocationDetailView.as_view(),     name='fp-paycheck-detail'),

    # ── Debt Snapshots ──
    path('plans/<int:plan_id>/snapshots/', views.MonthlyDebtSnapshotListCreateView.as_view(), name='fp-snapshot-list'),
    path('snapshots/<int:pk>/',            views.MonthlyDebtSnapshotDetailView.as_view(),     name='fp-snapshot-detail'),
]