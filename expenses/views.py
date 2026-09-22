from rest_framework             import generics
from rest_framework.permissions import IsAuthenticated

from .models      import Expense, Income
from .serializers import ExpenseSerializer, IncomeSerializer


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