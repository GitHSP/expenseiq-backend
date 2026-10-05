from rest_framework             import generics, status
from rest_framework.response    import Response
from rest_framework.views       import APIView
from rest_framework.permissions import IsAuthenticated
from django.shortcuts           import get_object_or_404
from django.utils               import timezone
from decimal                    import Decimal

from .models import (
    Debt, EmergencyFund, MonthlyPlan, ChecklistItem, RecurringItem,
)
from .serializers import (
    DebtSerializer, EmergencyFundSerializer, MonthlyPlanSerializer,
    ChecklistItemSerializer, RecurringItemSerializer,
)
from .services import (
    toggle_checklist_item, recalculate_avalanche_order,
    generate_checklist, rollover_month,
    get_or_create_current_plan, sync_current_month, delete_recurring_item,
    delete_debt,
)


# ─────────────────────────────────────────────
# DEBT VIEWS
# ─────────────────────────────────────────────

class DebtListCreateView(generics.ListCreateAPIView):
    serializer_class   = DebtSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Debt.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
        recalculate_avalanche_order(self.request.user)
        sync_current_month(self.request.user)


class DebtDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class   = DebtSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Debt.objects.filter(user=self.request.user)

    def perform_update(self, serializer):
        serializer.save()
        recalculate_avalanche_order(self.request.user)
        sync_current_month(self.request.user)

    def perform_destroy(self, instance):
        delete_debt(instance)


# ─────────────────────────────────────────────
# RECURRING ITEM VIEWS
# Every change re-syncs this month's checklist so it reflects the
# recurring list straight away (ticked items are left alone).
# ─────────────────────────────────────────────

class RecurringItemListCreateView(generics.ListCreateAPIView):
    serializer_class   = RecurringItemSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return RecurringItem.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
        sync_current_month(self.request.user)


class RecurringItemDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class   = RecurringItemSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return RecurringItem.objects.filter(user=self.request.user)

    def perform_update(self, serializer):
        serializer.save()
        sync_current_month(self.request.user)

    def perform_destroy(self, instance):
        delete_recurring_item(instance)


# ─────────────────────────────────────────────
# EMERGENCY FUND VIEWS
# ─────────────────────────────────────────────

class EmergencyFundView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Get or create singleton
        fund, _ = EmergencyFund.objects.get_or_create(user=request.user)
        return Response(EmergencyFundSerializer(fund).data)

    def patch(self, request):
        fund, _ = EmergencyFund.objects.get_or_create(user=request.user)
        serializer = EmergencyFundSerializer(fund, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ─────────────────────────────────────────────
# MONTHLY PLAN VIEWS
# ─────────────────────────────────────────────

class MonthlyPlanListCreateView(generics.ListCreateAPIView):
    serializer_class   = MonthlyPlanSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return MonthlyPlan.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class MonthlyPlanDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class   = MonthlyPlanSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return MonthlyPlan.objects.filter(user=self.request.user)


class CurrentMonthPlanView(APIView):
    """
    GET /api/fp/plans/current/
    Returns this month's plan. The first request of a new month creates it:
    interest is applied to debts and the checklist is built from the
    user's recurring items and debts. `rolled_over` is true on that request.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        plan, rolled_over = get_or_create_current_plan(request.user)
        data = MonthlyPlanSerializer(plan).data
        data["rolled_over"] = rolled_over
        return Response(data)


# ─────────────────────────────────────────────
# CHECKLIST VIEWS
# ─────────────────────────────────────────────

class ChecklistItemListCreateView(generics.ListCreateAPIView):
    serializer_class   = ChecklistItemSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        plan_id = self.kwargs.get('plan_id')
        return ChecklistItem.objects.filter(
            monthly_plan__user=self.request.user,
            monthly_plan__id=plan_id,
        )

    def perform_create(self, serializer):
        plan = get_object_or_404(
            MonthlyPlan,
            id=self.kwargs.get('plan_id'),
            user=self.request.user
        )
        serializer.save(monthly_plan=plan)


class ChecklistItemDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class   = ChecklistItemSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return ChecklistItem.objects.filter(
            monthly_plan__user=self.request.user
        )


class ChecklistItemToggleView(APIView):
    """
    POST /api/fp/checklist/<pk>/toggle/
    Toggles is_completed.
    If item has a linked_debt and category is debt_min/debt_extra,
    reduces the debt balance by the item amount.
    If item is savings, adds to emergency fund.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        item = get_object_or_404(
            ChecklistItem,
            pk=pk,
            monthly_plan__user=request.user
        )

        item, debt, fund = toggle_checklist_item(item)

        return Response({
            "item":         ChecklistItemSerializer(item).data,
            "debt_updated": DebtSerializer(debt).data if debt else None,
            "fund_updated": EmergencyFundSerializer(fund).data if fund else None,
        })


# ─────────────────────────────────────────────
# MONTHLY ROLLOVER
# ─────────────────────────────────────────────

class MonthlyRolloverView(APIView):
    """
    POST /api/fp/plans/rollover/
    Applies interest to all debts and generates
    next month's checklist from current month's items.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            new_plan, debts = rollover_month(request.user)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "message":      f"Rolled over to {new_plan.year}-{new_plan.month:02d}",
            "plan":         MonthlyPlanSerializer(new_plan).data,
            "debts_updated": DebtSerializer(debts, many=True).data,
        }, status=status.HTTP_201_CREATED)


class GenerateChecklistView(APIView):
    """
    POST /api/fp/plans/generate-checklist/
    Re-syncs this month's checklist with the user's recurring items and
    debts. Manual and already-ticked items are kept.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            plan, created_items, top_debt = generate_checklist(request.user)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "message":  f"Checklist synced — {len(created_items)} planned items.",
            "plan_id":  plan.id,
            "items":    ChecklistItemSerializer(created_items, many=True).data,
            "avalanche_target": DebtSerializer(top_debt).data if top_debt else None,
        }, status=status.HTTP_201_CREATED)
