from rest_framework             import generics, status
from rest_framework.response    import Response
from rest_framework.views       import APIView
from rest_framework.permissions import IsAuthenticated
from django.shortcuts           import get_object_or_404
from django.utils               import timezone
from decimal                    import Decimal

from .models import (
    Debt, EmergencyFund, MonthlyPlan,
    PaycheckAllocation, ChecklistItem, MonthlyDebtSnapshot,
    PaycheckConfig,
)
from .serializers import (
    DebtSerializer, EmergencyFundSerializer, MonthlyPlanSerializer,
    PaycheckAllocationSerializer, ChecklistItemSerializer,
    MonthlyDebtSnapshotSerializer, PaycheckConfigSerializer,
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
        self._recalculate_avalanche_order(self.request.user)

    def _recalculate_avalanche_order(self, user):
        debts = Debt.objects.filter(
            user=user, is_active=True
        ).order_by('-annual_interest_rate')
        for i, debt in enumerate(debts, start=1):
            debt.avalanche_order = i
            debt.save(update_fields=['avalanche_order'])


class DebtDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class   = DebtSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Debt.objects.filter(user=self.request.user)

    def perform_update(self, serializer):
        serializer.save()
        self._recalculate_avalanche_order(self.request.user)

    def perform_destroy(self, instance):
        instance.delete()
        self._recalculate_avalanche_order(self.request.user)

    def _recalculate_avalanche_order(self, user):
        debts = Debt.objects.filter(
            user=user, is_active=True
        ).order_by('-annual_interest_rate')
        for i, debt in enumerate(debts, start=1):
            debt.avalanche_order = i
            debt.save(update_fields=['avalanche_order'])


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
    Toggles is_completed.
    If item has a linked_debt and category is debt_min/debt_extra,
    reduces the debt balance by the item amount.
    If item is savings, adds to emergency fund.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        from decimal import Decimal
        from datetime import date

        item = get_object_or_404(
            ChecklistItem,
            pk=pk,
            monthly_plan__user=request.user
        )

        was_completed  = item.is_completed
        item.is_completed = not item.is_completed
        item.completed_at = timezone.now() if item.is_completed else None
        item.save()

        debt_updated = None
        fund_updated = None

        # ── Only trigger on ticking ON (not unticking) ──
        if item.is_completed and not was_completed:

            # ── Auto reduce debt balance ──
            if (
                item.linked_debt and
                item.amount and
                item.category in ["debt_min", "debt_extra"]
            ):
                debt = item.linked_debt
                reduction        = Decimal(str(item.amount))
                debt.current_balance = max(
                    Decimal("0.00"),
                    debt.current_balance - reduction
                )
                if debt.current_balance == Decimal("0.00"):
                    debt.is_active     = False
                    debt.paid_off_date = date.today()
                debt.save()
                debt_updated = DebtSerializer(debt).data

            # ── Auto add to emergency fund ──
            if item.category == "savings" and item.amount:
                try:
                    fund = EmergencyFund.objects.get(user=request.user)
                    fund.current_balance = min(
                        fund.current_balance + Decimal(str(item.amount)),
                        fund.target_amount
                    )
                    fund.save()
                    fund_updated = EmergencyFundSerializer(fund).data
                except EmergencyFund.DoesNotExist:
                    pass

        return Response({
            "item":         ChecklistItemSerializer(item).data,
            "debt_updated": debt_updated,
            "fund_updated": fund_updated,
        })


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
    



#for rollover to next month
class MonthlyRolloverView(APIView):
    """
    POST /api/fp/plans/rollover/
    Applies interest to all debts and generates
    next month's checklist from current month's items.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from decimal import Decimal
        from datetime import date
        import calendar

        # ── Get current month plan ──
        now        = timezone.now()
        next_month = now.month + 1 if now.month < 12 else 1
        next_year  = now.year if now.month < 12 else now.year + 1

        # ── Check if next month plan already exists ──
        if MonthlyPlan.objects.filter(
            user=request.user, year=next_year, month=next_month
        ).exists():
            return Response(
                {"error": f"Plan for {next_year}-{next_month:02d} already exists"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ── Step 1: Apply monthly interest to all active debts ──
        debts = Debt.objects.filter(user=request.user, is_active=True)
        for debt in debts:
            monthly_interest = debt.current_balance * debt.monthly_interest_rate
            debt.current_balance += monthly_interest
            debt.save()

        # ── Step 2: Create next month's plan ──
        current_plan = MonthlyPlan.objects.filter(
            user=request.user
        ).order_by("-year", "-month").first()

        # Detect 3-paycheck month
        # A month has 3 biweekly paychecks if it has 5 weeks
        _, days_in_month = calendar.monthrange(next_year, next_month)
        is_three_paycheck = days_in_month >= 29 and next_month in [1, 4, 7, 10]

        new_plan = MonthlyPlan.objects.create(
            user                       = request.user,
            year                       = next_year,
            month                      = next_month,
            biweekly_income            = current_plan.biweekly_income if current_plan else Decimal("1100.00"),
            second_job_income          = current_plan.second_job_income if current_plan else Decimal("925.00"),
            freelance_income           = Decimal("0.00"),
            num_paychecks              = 3 if is_three_paycheck else 2,
            is_three_paycheck_month    = is_three_paycheck,
            total_income               = current_plan.total_income if current_plan else Decimal("0.00"),
            total_fixed_expenses       = current_plan.total_fixed_expenses if current_plan else Decimal("0.00"),
            total_debt_payments        = current_plan.total_debt_payments if current_plan else Decimal("0.00"),
            emergency_fund_contribution= current_plan.emergency_fund_contribution if current_plan else Decimal("100.00"),
        )

        # ── Step 3: Copy checklist items from current month ──
        # Reset completion status for new month
        if current_plan:
            current_items = ChecklistItem.objects.filter(
                monthly_plan=current_plan
            )
            for item in current_items:
                # Update debt payment amounts based on new balances
                new_amount = item.amount
                if item.linked_debt and item.category == "debt_min":
                    new_amount = item.linked_debt.minimum_payment

                ChecklistItem.objects.create(
                    monthly_plan        = new_plan,
                    label               = item.label,
                    amount              = new_amount,
                    due_day             = item.due_day,
                    category            = item.category,
                    is_auto_debit       = item.is_auto_debit,
                    is_completed        = False,  # reset!
                    sort_order          = item.sort_order,
                    linked_debt         = item.linked_debt,
                )

        return Response({
            "message":      f"Rolled over to {next_year}-{next_month:02d}",
            "plan":         MonthlyPlanSerializer(new_plan).data,
            "debts_updated": DebtSerializer(debts, many=True).data,
        }, status=status.HTTP_201_CREATED)
    


class GenerateChecklistView(APIView):
    """
    POST /api/fp/plans/generate-checklist/
    Auto-generates checklist items from user's debts.
    Clears existing debt-related items and rebuilds them.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from decimal import Decimal

        # ── Get or create current month plan ──
        from django.utils import timezone
        now  = timezone.now()
        plan, _ = MonthlyPlan.objects.get_or_create(
            user  = request.user,
            year  = now.year,
            month = now.month,
        )

        # ── Get all active debts sorted by avalanche order ──
        debts = Debt.objects.filter(
            user=request.user, is_active=True
        ).order_by('avalanche_order')

        if not debts.exists():
            return Response(
                {"error": "No active debts found. Add debts first!"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ── Delete existing debt-related checklist items ──
        ChecklistItem.objects.filter(
            monthly_plan=plan,
            category__in=["debt_min", "debt_extra", "savings"]
        ).delete()

        created_items = []
        sort_order    = 1

        # ── Emergency fund savings item ──
        try:
            fund = EmergencyFund.objects.get(user=request.user)
            if not fund.is_funded:
                item = ChecklistItem.objects.create(
                    monthly_plan  = plan,
                    label         = f"Emergency Savings 🏦 — transfer FIRST on pay day",
                    amount        = fund.monthly_contribution,
                    due_day       = 1,
                    category      = "savings",
                    is_auto_debit = False,
                    sort_order    = sort_order,
                )
                created_items.append(item)
                sort_order += 1
        except EmergencyFund.DoesNotExist:
            pass

        # ── Debt minimum payments ──
        for debt in debts:
            item = ChecklistItem.objects.create(
                monthly_plan  = plan,
                label         = f"{debt.name} minimum payment",
                amount        = debt.minimum_payment,
                due_day       = debt.due_day,
                category      = "debt_min",
                is_auto_debit = False,
                sort_order    = sort_order,
                linked_debt   = debt,
            )
            created_items.append(item)
            sort_order += 1

        # ── Extra avalanche payment for highest APR debt ──
        top_debt = debts.first()
        if top_debt:
            # Calculate total minimums
            total_minimums = sum(d.minimum_payment for d in debts)

            # Suggest extra payment label
            item = ChecklistItem.objects.create(
                monthly_plan  = plan,
                label         = f"🎯 {top_debt.name} — EXTRA avalanche payment (all surplus)",
                amount        = Decimal("0.00"),  # user fills this in
                due_day       = top_debt.due_day,
                category      = "debt_extra",
                is_auto_debit = False,
                sort_order    = sort_order,
                linked_debt   = top_debt,
            )
            created_items.append(item)

        return Response({
            "message":  f"Generated {len(created_items)} checklist items from {debts.count()} debts!",
            "plan_id":  plan.id,
            "items":    ChecklistItemSerializer(created_items, many=True).data,
            "avalanche_target": DebtSerializer(top_debt).data if top_debt else None,
        }, status=status.HTTP_201_CREATED)
    
class PaycheckConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from datetime import date
        config, _ = PaycheckConfig.objects.get_or_create(
            user=request.user,
            defaults={
                'first_pay_date':   date(2026, 5, 1),
                'biweekly_amount':  1100,
                'second_job_day':   20,
                'second_job_amount':625,
                'second_job_tips':  300,
                'extra_payment':    150,
            }
        )
        return Response(PaycheckConfigSerializer(config).data)

    def patch(self, request):
        from datetime import date
        config, _ = PaycheckConfig.objects.get_or_create(
            user=request.user,
            defaults={'first_pay_date': date(2026, 5, 1)}
        )
        serializer = PaycheckConfigSerializer(
            config, data=request.data, partial=True
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PaycheckAllocationCalculateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from decimal import Decimal
        from datetime import date as date_type
        from django.utils import timezone

        now   = timezone.now()
        year  = int(request.query_params.get('year',  now.year))
        month = int(request.query_params.get('month', now.month))

        try:
            config = PaycheckConfig.objects.get(user=request.user)
        except PaycheckConfig.DoesNotExist:
            return Response(
                {"error": "Please set up your paycheck configuration first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            plan = MonthlyPlan.objects.get(
                user=request.user, year=year, month=month
            )
        except MonthlyPlan.DoesNotExist:
            return Response(
                {"error": f"No plan found for {year}-{month:02d}. Please generate a plan first."},
                status=status.HTTP_400_BAD_REQUEST
            )

        items = ChecklistItem.objects.filter(
            monthly_plan=plan
        ).order_by('due_day', 'sort_order')

        biweekly_dates  = config.get_pay_dates_for_month(year, month)
        second_job_date = config.get_second_job_date_for_month(year, month)

        paychecks = []
        for i, pay_date in enumerate(biweekly_dates, start=1):
            paychecks.append({
                'number':        i,
                'date':          pay_date.isoformat(),
                'source':        'biweekly',
                'gross':         float(config.biweekly_amount),
                'label':         f"Paycheck #{i} — {pay_date.strftime('%b %d')}",
                'items':         [],
                'final_balance': float(config.biweekly_amount),
                'total_bills':   0,
                'is_negative':   False,
            })

        paychecks.append({
            'number':        len(biweekly_dates) + 1,
            'date':          second_job_date.isoformat(),
            'source':        'second_job',
            'gross':         float(config.second_job_amount + config.second_job_tips),
            'label':         f"2nd Job + Tips — {second_job_date.strftime('%b %d')}",
            'items':         [],
            'final_balance': float(config.second_job_amount + config.second_job_tips),
            'total_bills':   0,
            'is_negative':   False,
        })

        paychecks.sort(key=lambda p: p['date'])

        unassigned = []
        for item in items:
            if not item.amount:
                continue

            due_day  = item.due_day or 28
            import calendar
            last_day = calendar.monthrange(year, month)[1]
            item_date= date_type(year, month, min(due_day, last_day))

            suitable = [
                p for p in paychecks
                if p['date'] <= item_date.isoformat()
            ]

            if suitable:
                target = suitable[-1]
                running = target['gross'] - target['total_bills'] - float(item.amount)
                target['items'].append({
                    'id':           item.id,
                    'label':        item.label,
                    'amount':       float(item.amount),
                    'due_day':      item.due_day,
                    'category':     item.category,
                    'is_auto_debit':item.is_auto_debit,
                    'is_completed': item.is_completed,
                    'linked_debt':  item.linked_debt_id,
                    'running_after':round(running, 2),
                })
                target['total_bills']   += float(item.amount)
                target['final_balance']  = round(target['gross'] - target['total_bills'], 2)
                target['is_negative']    = target['final_balance'] < 0
            else:
                unassigned.append({
                    'id':       item.id,
                    'label':    item.label,
                    'amount':   float(item.amount) if item.amount else 0,
                    'due_day':  item.due_day,
                    'category': item.category,
                })

        total_income  = sum(p['gross'] for p in paychecks)
        total_bills   = sum(p['total_bills'] for p in paychecks)
        total_surplus = total_income - total_bills

        return Response({
            'year':          year,
            'month':         month,
            'paychecks':     paychecks,
            'unassigned':    unassigned,
            'total_income':  round(total_income, 2),
            'total_bills':   round(total_bills, 2),
            'total_surplus': round(total_surplus, 2),
            'extra_payment': float(config.extra_payment),
            'paycheck_count':len(paychecks),
        })


class GenerateChecklistView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from decimal import Decimal
        from django.utils import timezone

        now  = timezone.now()
        plan, _ = MonthlyPlan.objects.get_or_create(
            user=request.user, year=now.year, month=now.month,
        )

        debts = Debt.objects.filter(
            user=request.user, is_active=True
        ).order_by('avalanche_order')

        if not debts.exists():
            return Response(
                {"error": "No active debts found. Add debts first!"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Delete existing debt-related items
        ChecklistItem.objects.filter(
            monthly_plan=plan,
            category__in=["debt_min", "debt_extra", "savings"]
        ).delete()

        created_items = []
        sort_order    = 1

        # Emergency fund savings item
        try:
            fund = EmergencyFund.objects.get(user=request.user)
            if not fund.is_funded:
                item = ChecklistItem.objects.create(
                    monthly_plan  = plan,
                    label         = "Emergency Savings 🏦 — transfer FIRST on pay day",
                    amount        = fund.monthly_contribution,
                    due_day       = 1,
                    category      = "savings",
                    is_auto_debit = False,
                    sort_order    = sort_order,
                )
                created_items.append(item)
                sort_order += 1
        except EmergencyFund.DoesNotExist:
            pass

        # Minimum payments
        for debt in debts:
            item = ChecklistItem.objects.create(
                monthly_plan  = plan,
                label         = f"{debt.name} minimum payment",
                amount        = debt.minimum_payment,
                due_day       = debt.due_day,
                category      = "debt_min",
                is_auto_debit = False,
                sort_order    = sort_order,
                linked_debt   = debt,
            )
            created_items.append(item)
            sort_order += 1

        # Extra avalanche payment for top debt
        top_debt = debts.first()
        if top_debt:
            item = ChecklistItem.objects.create(
                monthly_plan  = plan,
                label         = f"🎯 {top_debt.name} — EXTRA avalanche payment (all surplus)",
                amount        = Decimal("0.00"),
                due_day       = top_debt.due_day,
                category      = "debt_extra",
                is_auto_debit = False,
                sort_order    = sort_order,
                linked_debt   = top_debt,
            )
            created_items.append(item)

        return Response({
            "message":          f"Generated {len(created_items)} checklist items from {debts.count()} debts!",
            "plan_id":          plan.id,
            "items":            ChecklistItemSerializer(created_items, many=True).data,
            "avalanche_target": DebtSerializer(top_debt).data if top_debt else None,
        }, status=status.HTTP_201_CREATED)