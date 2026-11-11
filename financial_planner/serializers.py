from decimal import Decimal
from rest_framework import serializers
from django.utils   import timezone
from expenses.serializers import CURRENCIES, clean_original
from .models import (
    Debt, EmergencyFund, MonthlyPlan, ChecklistItem, RecurringItem,
    Investment, InvestmentPayment,
)
from .investments import summarize
from .policy_plan import PlanError, build_plan, validate_plan


class DebtSerializer(serializers.ModelSerializer):
    # Read-only computed properties from model
    monthly_interest_rate  = serializers.DecimalField(max_digits=10, decimal_places=6, read_only=True)
    utilization_percent    = serializers.FloatField(read_only=True)
    monthly_interest_amount= serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    # The closing balance on the linked card's newest statement, beside the Planner's own.
    statement_balance      = serializers.SerializerMethodField()
    statement_date         = serializers.SerializerMethodField()

    def _statement(self, obj):
        if not hasattr(obj, "_latest_statement"):
            from expenses.models import Statement
            obj._latest_statement = (Statement.objects.filter(account__debt=obj, new_balance__isnull=False)
                                     .order_by("-period_end", "-id").first())
        return obj._latest_statement

    def get_statement_balance(self, obj):
        st = self._statement(obj)
        return str(max(st.new_balance, Decimal("0.00"))) if st else None

    def get_statement_date(self, obj):
        st = self._statement(obj)
        return st.period_end.isoformat() if st else None

    class Meta:
        model  = Debt
        fields = [
            'id', 'name', 'debt_type',
            'current_balance', 'annual_interest_rate',
            'minimum_payment', 'credit_limit',
            'due_day', 'avalanche_order',
            'is_active', 'paid_off_date', 'notes',
            'original_currency', 'original_current_balance', 'original_minimum_payment', 'original_credit_limit',
            'created_at', 'updated_at',
            # computed
            'monthly_interest_rate',
            'utilization_percent',
            'monthly_interest_amount',
            'statement_balance', 'statement_date',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    ORIGINALS = ("original_current_balance", "original_minimum_payment", "original_credit_limit")

    def validate(self, attrs):
        if "original_currency" in attrs or any(f in attrs for f in self.ORIGINALS):
            cur = str(attrs.get("original_currency") or "").upper()
            if cur and cur not in CURRENCIES:
                raise serializers.ValidationError({"original_currency": "Unsupported currency."})
            if not cur or cur == "CAD":
                attrs["original_currency"] = ""
                for f in self.ORIGINALS:
                    attrs[f] = None
            else:
                attrs["original_currency"] = cur
        # (CAD amounts edited on their own: the model moves the originals along.)
        return attrs

    def _save(self, instance, validated_data):
        instance._originals_given = "original_currency" in validated_data
        for k, v in validated_data.items():
            setattr(instance, k, v)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        return self._save(instance, validated_data)


class EmergencyFundSerializer(serializers.ModelSerializer):
    is_funded        = serializers.BooleanField(read_only=True)
    progress_percent = serializers.FloatField(read_only=True)

    class Meta:
        model  = EmergencyFund
        fields = [
            'id', 'current_balance', 'target_amount',
            'monthly_contribution', 'updated_at',
            # computed
            'is_funded', 'progress_percent',
        ]
        read_only_fields = ['id', 'updated_at']


class ChecklistItemSerializer(serializers.ModelSerializer):
    class Meta:
        model  = ChecklistItem
        fields = [
            'id', 'label',
            'amount', 'original_amount', 'original_currency', 'due_day', 'category',
            'is_auto_debit', 'is_completed',
            'completed_at', 'sort_order', 'linked_debt',
            'recurring_item', 'paid_amount',
        ]
        read_only_fields = ['id', 'completed_at', 'recurring_item', 'paid_amount']

    def validate(self, attrs):
        return clean_original(attrs, self.instance)

    def validate_linked_debt(self, debt):
        # Ticking an item pays down its linked debt, so it must be the
        # caller's own debt — never someone else's, even if they guess its id.
        request = self.context.get("request")
        if debt is not None and (request is None or debt.user_id != request.user.id):
            raise serializers.ValidationError("Debt not found.")
        return debt


class RecurringItemSerializer(serializers.ModelSerializer):
    class Meta:
        model  = RecurringItem
        fields = [
            'id', 'label', 'amount', 'original_amount', 'original_currency', 'due_day', 'category',
            'frequency', 'anchor_date', 'average_from', 'average_of', 'debt',
            'is_auto_debit', 'paid_by_card', 'is_active', 'notes',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'average_of', 'created_at', 'updated_at']

    def validate(self, attrs):
        frequency = attrs.get("frequency", getattr(self.instance, "frequency", "monthly"))
        anchor = attrs.get("anchor_date", getattr(self.instance, "anchor_date", None))
        if frequency in ("weekly", "biweekly") and not anchor:
            raise serializers.ValidationError({"anchor_date": "Give one recent payday so the paydays can be worked out."})
        return clean_original(attrs, self.instance)

    def validate_due_day(self, value):
        if value is not None and not 1 <= value <= 31:
            raise serializers.ValidationError("Due day must be between 1 and 31.")
        return value

    def validate_debt(self, value):
        request = self.context.get("request")
        if value is not None and request and value.user_id != request.user.id:
            raise serializers.ValidationError("Debt not found.")
        return value


class MonthlyPlanSerializer(serializers.ModelSerializer):
    checklist_items      = ChecklistItemSerializer(many=True, read_only=True)
    effective_income     = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model  = MonthlyPlan
        fields = [
            'id', 'year', 'month',
            'freelance_income',
            'total_income', 'total_fixed_expenses',
            'total_debt_payments', 'surplus_to_avalanche',
            'emergency_fund_contribution',
            'is_finalized', 'notes',
            'created_at', 'updated_at',
            # computed
            'effective_income',
            # nested
            'checklist_items',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class InvestmentPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model  = InvestmentPayment
        fields = ['id', 'date', 'amount', 'note']


class InvestmentSerializer(serializers.ModelSerializer):
    payments = InvestmentPaymentSerializer(many=True, read_only=True)
    summary  = serializers.SerializerMethodField()

    class Meta:
        model  = Investment
        fields = [
            'id', 'kind', 'name', 'provider', 'currency', 'start_date', 'recurring_item',
            'payment', 'payment_frequency', 'rate',
            'policy_type', 'paying_years', 'term_years', 'sum_assured',
            'current_value', 'current_value_date', 'maturity_value', 'yearly_cost', 'surrender_charge', 'plan',
            'months', 'chit_value', 'prized', 'prized_date', 'prize_amount',
            'deposit_where', 'deposit_amount', 'deposit_rate', 'deposit_start',
            'compounding', 'interest_payout', 'is_active', 'notes', 'payments', 'summary',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_summary(self, obj):
        s = summarize(obj)
        if obj.kind == "insurance" and obj.plan:
            s["plan"] = build_plan(obj)
        return s

    def validate_plan(self, value):
        import json
        if len(json.dumps(value)) > 200_000:
            raise serializers.ValidationError("That's too much data for one policy.")
        try:
            return validate_plan(value)
        except PlanError as e:
            raise serializers.ValidationError(str(e))

    def _apply_plan(self, validated):
        # The plan's newest statement value and surrender charge become the policy's
        # cash value, so the summary and net worth use them too.
        plan = validated.get("plan")
        if not plan or not plan.get("values"):
            return
        from datetime import date as _d
        from decimal import Decimal as _D
        from .investments import months_between
        from .policy_plan import surrender_charge
        last = plan["values"][-1]
        validated["current_value"] = _D(str(round(last["fund"], 2)))
        validated["current_value_date"] = _d.fromisoformat(last["date"])
        start = validated.get("start_date") or (self.instance.start_date if self.instance else None)
        if start:
            py = months_between(start, _d.fromisoformat(last["date"])) // 12 + 1
            validated["surrender_charge"] = _D(str(round(surrender_charge(plan, py), 2)))
        latest = plan["years"][-1] if plan.get("years") else None
        if latest and latest.get("charges") is not None:
            validated["yearly_cost"] = _D(str(round(latest["charges"] + (latest.get("tax") or 0), 2)))

    def create(self, validated):
        self._apply_plan(validated)
        return super().create(validated)

    def update(self, instance, validated):
        self._apply_plan(validated)
        return super().update(instance, validated)

    def validate_currency(self, value):
        value = (value or "").upper()
        if value not in CURRENCIES:
            raise serializers.ValidationError("Unsupported currency.")
        return value

    def validate_recurring_item(self, value):
        if value is not None and value.user_id != self.context["request"].user.id:
            raise serializers.ValidationError("Recurring payment not found.")
        return value

    def validate(self, attrs):
        def get(f):
            return attrs.get(f, getattr(self.instance, f, None))
        kind = get("kind")
        if kind in ("chit", "fd") and not get("months"):
            raise serializers.ValidationError({"months": "How many months does it run?"})
        if kind == "chit" and not get("chit_value"):
            raise serializers.ValidationError({"chit_value": "Enter the chit's total value."})
        if kind == "fd" and not get("payment"):
            raise serializers.ValidationError({"payment": "Enter the amount deposited."})
        if kind == "rd" and not get("payment"):
            raise serializers.ValidationError({"payment": "Enter the deposit amount."})
        allowed = {"rd": ("weekly", "biweekly", "monthly"),
                   "insurance": ("monthly", "quarterly", "half_yearly", "yearly")}.get(kind)
        if allowed and (get("payment_frequency") or "monthly") not in allowed:
            raise serializers.ValidationError({"payment_frequency": "That isn't an option for this investment."})
        if kind == "insurance" and not get("payment"):
            raise serializers.ValidationError({"payment": "Enter the premium."})
        paying, term = get("paying_years"), get("term_years")
        if paying and paying > 100:
            raise serializers.ValidationError({"paying_years": "That's more than 100 years — leave it empty if premiums have no set end."})
        if term and term > 100:
            raise serializers.ValidationError({"term_years": "That's more than 100 years."})
        if paying and term and paying > term:
            raise serializers.ValidationError({"paying_years": "Premiums can't run longer than the policy term."})
        if kind == "insurance" and get("yearly_cost") and get("payment"):
            per_year = Decimal(get("payment")) * {"monthly": 12, "quarterly": 4, "half_yearly": 2, "yearly": 1}.get(
                get("payment_frequency") or "monthly", 12)
            if Decimal(get("yearly_cost")) > per_year * 2:
                raise serializers.ValidationError({"yearly_cost": "That's more than twice your yearly premiums — check the statement."})
        if attrs.get("current_value") is not None and not get("current_value_date"):
            attrs["current_value_date"] = timezone.localdate()
        return attrs


class SavingsGoalSerializer(serializers.ModelSerializer):
    """A goal, with what's left and what to put aside each month to make the date."""
    left          = serializers.SerializerMethodField()
    percent       = serializers.SerializerMethodField()
    monthly       = serializers.SerializerMethodField()
    months_left   = serializers.SerializerMethodField()

    class Meta:
        from .models import SavingsGoal
        model  = SavingsGoal
        fields = ["id", "name", "target", "saved", "target_date", "left", "percent", "monthly", "months_left", "created_at"]
        read_only_fields = ["id", "created_at"]

    def _left(self, obj):
        return max(obj.target - obj.saved, Decimal("0.00"))

    def get_left(self, obj):
        return str(self._left(obj))

    def get_percent(self, obj):
        return round(float(min(obj.saved / obj.target, 1) * 100), 1) if obj.target else 0

    def get_months_left(self, obj):
        if not obj.target_date:
            return None
        today = timezone.localdate()
        return max((obj.target_date.year - today.year) * 12 + obj.target_date.month - today.month, 0)

    def get_monthly(self, obj):
        months = self.get_months_left(obj)
        if months is None or self._left(obj) <= 0:
            return None
        return str((self._left(obj) / max(months, 1)).quantize(Decimal("0.01")))

    def validate_name(self, value):
        value = " ".join(str(value).split())
        if len(value) < 2:
            raise serializers.ValidationError("Give the goal a name.")
        return value
