from rest_framework             import generics, status
from rest_framework.response    import Response
from rest_framework.views       import APIView
from rest_framework.permissions import IsAuthenticated
from django.shortcuts           import get_object_or_404
from django.utils               import timezone
from decimal                    import Decimal

from .models import (
    Debt, EmergencyFund, MonthlyPlan,
    PaycheckAllocation, ChecklistItem, MonthlyDebtSnapshot
)
from .serializers import (
    DebtSerializer, EmergencyFundSerializer, MonthlyPlanSerializer,
    PaycheckAllocationSerializer, ChecklistItemSerializer,
    MonthlyDebtSnapshotSerializer
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


class DebtDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class   = DebtSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Debt.objects.filter(user=self.request.user)


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
    Returns this month's plan — creates it if it doesn't exist yet.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        now   = timezone.now()
        plan, created = MonthlyPlan.objects.get_or_create(
            user  = request.user,
            year  = now.year,
            month = now.month,
        )
        return Response(MonthlyPlanSerializer(plan).data)


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
    Toggles is_completed on a checklist item.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        item = get_object_or_404(
            ChecklistItem,
            pk=pk,
            monthly_plan__user=request.user
        )
        item.is_completed = not item.is_completed
        item.completed_at = timezone.now() if item.is_completed else None
        item.save()
        return Response(ChecklistItemSerializer(item).data)


# ─────────────────────────────────────────────
# PAYCHECK ALLOCATION VIEWS
# ─────────────────────────────────────────────

class PaycheckAllocationListCreateView(generics.ListCreateAPIView):
    serializer_class   = PaycheckAllocationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        plan_id = self.kwargs.get('plan_id')
        return PaycheckAllocation.objects.filter(
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


class PaycheckAllocationDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class   = PaycheckAllocationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return PaycheckAllocation.objects.filter(
            monthly_plan__user=self.request.user
        )


# ─────────────────────────────────────────────
# MONTHLY DEBT SNAPSHOT VIEWS
# ─────────────────────────────────────────────

class MonthlyDebtSnapshotListCreateView(generics.ListCreateAPIView):
    serializer_class   = MonthlyDebtSnapshotSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        plan_id = self.kwargs.get('plan_id')
        return MonthlyDebtSnapshot.objects.filter(
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


class MonthlyDebtSnapshotDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class   = MonthlyDebtSnapshotSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return MonthlyDebtSnapshot.objects.filter(
            monthly_plan__user=self.request.user
        )