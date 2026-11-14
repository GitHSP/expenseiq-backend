from django.urls import path
from . import views

urlpatterns = [
    # ── Debts ──
    path('debts/',              views.DebtListCreateView.as_view(),              name='fp-debt-list'),
    path('debts/history/',      views.DebtHistoryView.as_view(),                 name='fp-debt-history'),
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
    path('goals/',                          views.SavingsGoalListView.as_view(),      name='fp-goals'),
    path('goals/<int:pk>/',                 views.SavingsGoalDetailView.as_view(),    name='fp-goal-detail'),
    path('reminders/',                      views.RemindersView.as_view(),            name='fp-reminders'),
    path('recap/',                          views.RecapView.as_view(),                name='fp-recap'),
    path('budgets/',                        views.BudgetListView.as_view(),           name='fp-budgets'),
    path('budgets/<int:pk>/',               views.BudgetDetailView.as_view(),         name='fp-budget-detail'),
    path('cash-flow/',                      views.CashFlowView.as_view(),             name='fp-cash-flow'),
    path('outlook/',                        views.OutlookView.as_view(),              name='fp-outlook'),
    path('net-worth/',                      views.NetWorthView.as_view(),             name='fp-net-worth'),

    # ── Checklist ──
    path('plans/<int:plan_id>/checklist/', views.ChecklistItemListCreateView.as_view(),  name='fp-checklist-list'),
    path('checklist/<int:pk>/',            views.ChecklistItemDetailView.as_view(),      name='fp-checklist-detail'),
    path('checklist/<int:pk>/toggle/',     views.ChecklistItemToggleView.as_view(),      name='fp-checklist-toggle'),
]

from . import day_views

urlpatterns += [
    path("day/", day_views.DayView.as_view(), name="day"),
    path("day/profile/", day_views.ProfileView.as_view(), name="day-profile"),
    path("day/week/", day_views.WeekView.as_view(), name="day-week"),
    path("day/goals/", day_views.GoalListView.as_view(), name="day-goals"),
    path("day/goals/<int:pk>/", day_views.GoalDetailView.as_view(), name="day-goal"),
    path("day/tasks/", day_views.TaskListView.as_view(), name="day-tasks"),
    path("day/tasks/<int:pk>/", day_views.TaskDetailView.as_view(), name="day-task"),
    path("day/plan/", day_views.PlanView.as_view(), name="day-plan"),
    path("day/slots/", day_views.SlotView.as_view(), name="day-slots"),
    path("day/slots/<int:pk>/", day_views.SlotView.as_view(), name="day-slot"),
    path("day/cron/", day_views.CronView.as_view(), name="day-cron"),
]
