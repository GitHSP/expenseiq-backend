# expenses/views.py

from rest_framework             import generics, status
from rest_framework.response    import Response
from rest_framework.views       import APIView
from rest_framework.permissions import IsAuthenticated
from django.utils               import timezone

from .models      import Expense, Budget, Income, Debt, PaymentRecord
from .serializers import (
    ExpenseSerializer, BudgetSerializer,
    IncomeSerializer, DebtSerializer, PaymentRecordSerializer
)


class ExpenseListCreateView(generics.ListCreateAPIView):
    serializer_class   = ExpenseSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Expense.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class ExpenseDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class   = ExpenseSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Expense.objects.filter(user=self.request.user)


class BudgetListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        budgets    = Budget.objects.filter(user=request.user)
        serializer = BudgetSerializer(budgets, many=True)
        return Response(serializer.data)

    def post(self, request):
        category = request.data.get('category')
        amount   = request.data.get('amount')
        budget, created = Budget.objects.update_or_create(
            user=request.user, category=category,
            defaults={'amount': amount}
        )
        return Response(BudgetSerializer(budget).data)


class IncomeListCreateView(generics.ListCreateAPIView):
    serializer_class   = IncomeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Income.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class IncomeDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class   = IncomeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Income.objects.filter(user=self.request.user)


class DebtListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/debts/ → list all debts for logged in user
    POST /api/debts/ → create a new debt
    """
    serializer_class   = DebtSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Debt.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class DebtDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    GET    /api/debts/<id>/ → get one debt
    PUT    /api/debts/<id>/ → update one debt
    DELETE /api/debts/<id>/ → delete one debt
    """
    serializer_class   = DebtSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Debt.objects.filter(user=self.request.user)


class DebtMarkPaidOffView(APIView):
    """
    POST /api/debts/<id>/paid-off/ → mark debt as paid off
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            debt = Debt.objects.get(pk=pk, user=request.user)
            debt.is_paid_off = True
            debt.balance     = 0
            debt.save()
            return Response(DebtSerializer(debt).data)
        except Debt.DoesNotExist:
            return Response({'error': 'Debt not found'}, status=status.HTTP_404_NOT_FOUND)


class PaymentRecordListCreateView(APIView):
    """
    GET  /api/payments/ → list all payment records
    POST /api/payments/ → record a new payment against a debt
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        payments   = PaymentRecord.objects.filter(user=request.user)
        serializer = PaymentRecordSerializer(payments, many=True)
        return Response(serializer.data)

    def post(self, request):
        debt_id = request.data.get('debt')
        amount  = request.data.get('amount')
        note    = request.data.get('note', '')
        date    = request.data.get('date', timezone.now().date())

        try:
            debt = Debt.objects.get(pk=debt_id, user=request.user)
        except Debt.DoesNotExist:
            return Response({'error': 'Debt not found'}, status=status.HTTP_404_NOT_FOUND)

        # Record the payment
        payment = PaymentRecord.objects.create(
            user   = request.user,
            debt   = debt,
            amount = amount,
            date   = date,
            note   = note,
        )

        # Reduce debt balance
        from decimal import Decimal
        new_balance      = max(Decimal('0'), debt.balance - Decimal(str(amount)))
        debt.balance     = new_balance
        debt.is_paid_off = new_balance == 0
        debt.save()

        return Response({
            'payment': PaymentRecordSerializer(payment).data,
            'debt':    DebtSerializer(debt).data,
        }, status=status.HTTP_201_CREATED)