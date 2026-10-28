# expenses/account_views.py
#
# The Accounts page: the user's bank accounts and cards, the statements
# uploaded for each (and which months they cover), every transaction on an
# account, and recurring charges found across all of them.

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Max, Min, Q, Sum
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from activity.context import activity_context, log_activity

from .accounts import coverage, find_recurring, missing_periods
from .card_debts import card_history, payments_since, sync_debt
from .models import Account, Expense, Income, Statement, StatementTransaction
from .statement_import import EXPENSE_RULES, _match


def _money(v):
    return str((v or Decimal("0")).quantize(Decimal("0.01")))


def _month_range(value):
    """'2026-05' -> (date(2026, 5, 1), date(2026, 5, 31)), or None."""
    try:
        y, m = (int(p) for p in str(value).split("-"))
        first = date(y, m, 1)
    except (TypeError, ValueError):
        return None
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return first, date.fromordinal(nxt.toordinal() - 1)


class AccountListView(APIView):
    """GET /api/statements/accounts/ — accounts, their statements and month coverage."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        accounts = list(Account.objects.filter(user=request.user).select_related("debt"))
        statements = list(
            Statement.objects.filter(user=request.user)
            .annotate(
                count=Count("transactions"),
                money_in=Sum("transactions__amount", filter=Q(transactions__amount__gt=0)),
                money_out=Sum("transactions__amount", filter=Q(transactions__amount__lt=0)),
            )
            .order_by("-period_end", "-id")
        )
        tx_stats = {
            row["account"]: row for row in
            StatementTransaction.objects.filter(user=request.user)
            .values("account").annotate(count=Count("id"), first=Min("date"), last=Max("date"))
        }
        by_account = {}
        for s in statements:
            by_account.setdefault(s.account_id, []).append({
                "id": s.id, "filename": s.filename,
                "period_start": s.period_start.isoformat(), "period_end": s.period_end.isoformat(),
                "uploaded_at": s.uploaded_at.isoformat(), "count": s.count,
                "money_in": _money(s.money_in), "money_out": _money(-(s.money_out or 0)),
            })
        cov = coverage(accounts, statements)
        return Response({
            "accounts": [{
                "id": a.id, "name": a.name, "kind": a.kind, "institution": a.institution, "last4": a.last4,
                "transaction_count": tx_stats.get(a.id, {}).get("count", 0),
                "first_date": tx_stats[a.id]["first"].isoformat() if a.id in tx_stats else None,
                "last_date": tx_stats[a.id]["last"].isoformat() if a.id in tx_stats else None,
                "statements": by_account.get(a.id, []),
                "missing": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in missing_periods(
                    (s.period_start, s.period_end) for s in statements if s.account_id == a.id)],
                "debt": {"id": a.debt.id, "name": a.debt.name, "current_balance": _money(a.debt.current_balance)} if a.debt else None,
                "card": _card(a) if a.kind == "credit_card" else None,
                "coverage": cov["accounts"].get(a.id, {}),
            } for a in accounts],
            "months": cov["months"],
        })


def _card(account):
    """A card's statement-by-statement history and what's been paid to it since."""
    history = card_history(account)
    last = next((h for h in reversed(history) if h["new_balance"] is not None), None)
    since = payments_since(account, date.fromisoformat(last["period_end"])) if last else []
    paid_since = sum((-t.amount for t in since), Decimal("0"))
    return {
        "history": history,
        "statement_balance": last["new_balance"] if last else None,
        "statement_end": last["period_end"] if last else None,
        "minimum_payment": last["minimum_payment"] if last else None,
        "payment_due": last["payment_due"] if last else None,
        "credit_limit": last["credit_limit"] if last else None,
        "paid_since": _money(paid_since),
        "payments_since": [{"date": t.date.isoformat(), "amount": _money(-t.amount), "from": t.account.name,
                            "title": t.title} for t in since],
        # What's owed now, as far as the uploaded statements show: the last
        # statement's balance less payments made since (new purchases since
        # then only appear on the next statement).
        "estimated_balance": _money(Decimal(last["new_balance"]) - paid_since) if last else None,
    }


class MonthlySummaryView(APIView):
    """GET /api/statements/summary/ — income and spending per month across all
    accounts, from the uploaded statements. Transfers between the user's own
    accounts (incl. card payments) are left out; card refunds reduce spending;
    what roommates pay toward shared bills reduces spending and isn't income."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .rules import is_shared, shared_matchers
        shared = shared_matchers(request.user)
        months = {}
        rows = (StatementTransaction.objects.filter(user=request.user)
                .values_list("date", "amount", "account__kind", "kind", "title", "description"))
        for d, amount, acct_kind, kind, title, desc in rows:
            m = months.setdefault(f"{d.year}-{d.month:02d}",
                                  {"income": Decimal("0"), "expenses": Decimal("0"), "shared": Decimal("0")})
            if amount > 0 and acct_kind != "credit_card" and shared and is_shared(f"{title} {desc}", shared):
                m["shared"] += amount            # a roommate's share of the bills: lowers spending
            elif kind == "transfer":
                continue
            elif amount > 0 and acct_kind != "credit_card":
                m["income"] += amount
            else:
                m["expenses"] -= amount          # card refunds (positive) lower spending
        out = [{"month": k, "income": _money(v["income"]), "expenses": _money(v["expenses"] - v["shared"]),
                "net": _money(v["income"] - v["expenses"] + v["shared"]),
                "shared_by_others": _money(v["shared"])} for k, v in sorted(months.items())]
        total_in = sum((Decimal(m["income"]) for m in out), Decimal("0"))
        total_out = sum((Decimal(m["expenses"]) for m in out), Decimal("0"))
        return Response({"months": out, "total_income": _money(total_in), "total_expenses": _money(total_out),
                         "total_net": _money(total_in - total_out)})


class AccountDetailView(APIView):
    """PATCH /api/statements/accounts/<id>/ {name?, kind?} · DELETE removes the account
    and its statements (expenses and income already saved from them stay)."""
    permission_classes = [IsAuthenticated]

    def _get(self, request, pk):
        return Account.objects.filter(user=request.user, pk=pk).first()

    def patch(self, request, pk):
        account = self._get(request, pk)
        if not account:
            return Response({"error": "Account not found."}, status=status.HTTP_404_NOT_FOUND)
        name = request.data.get("name")
        kind = request.data.get("kind")
        if name is not None:
            name = str(name).strip()[:100]
            if not name:
                return Response({"error": "Name can't be empty."}, status=status.HTTP_400_BAD_REQUEST)
            if Account.objects.filter(user=request.user, name__iexact=name).exclude(pk=pk).exists():
                return Response({"error": "You already have an account with that name."}, status=status.HTTP_400_BAD_REQUEST)
            account.name = name
        if kind is not None:
            if kind not in dict(Account.KINDS):
                return Response({"error": "Unknown account type."}, status=status.HTTP_400_BAD_REQUEST)
            account.kind = kind
        relink = "debt" in request.data
        if relink:
            from financial_planner.models import Debt
            debt_id = request.data.get("debt")
            debt = Debt.objects.filter(user=request.user, pk=debt_id).first() if debt_id else None
            if debt_id and not debt:
                return Response({"error": "Debt not found."}, status=status.HTTP_404_NOT_FOUND)
            if debt:      # a debt belongs to one card
                Account.objects.filter(user=request.user, debt=debt).exclude(pk=pk).update(debt=None)
            account.debt, account.debt_synced_through = debt, None
        account.save()
        synced = sync_debt(account, force=True) if relink and account.debt else None
        return Response({"id": account.id, "name": account.name, "kind": account.kind,
                         "debt": account.debt_id, "debt_updated": synced})

    def delete(self, request, pk):
        account = self._get(request, pk)
        if not account:
            return Response({"error": "Account not found."}, status=status.HTTP_404_NOT_FOUND)
        name, statements = account.name, account.statements.count()
        account.delete()
        log_activity(request.user, "import", "deleted", f"Account {name}",
                     details={"statements": statements}, source="app", reason="")
        return Response(status=status.HTTP_204_NO_CONTENT)


class AccountMergeView(APIView):
    """POST /api/statements/accounts/<id>/merge/ {"into": <id>} — move every
    statement and transaction of this account into another one and remove it
    (for when one account was detected as two)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        source = Account.objects.filter(user=request.user, pk=pk).first()
        target = Account.objects.filter(user=request.user, pk=request.data.get("into")).first()
        if not source or not target:
            return Response({"error": "Account not found."}, status=status.HTTP_404_NOT_FOUND)
        if source.pk == target.pk:
            return Response({"error": "Choose a different account to merge into."}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            moved = source.statements.update(account=target)
            StatementTransaction.objects.filter(account=source).update(account=target)
            name = source.name
            source.delete()
        log_activity(request.user, "import", "updated", f"Merged {name} into {target.name}",
                     details={"statements": moved}, source="app", reason="")
        return Response({"id": target.id, "name": target.name, "statements_moved": moved})


class StatementDetailView(APIView):
    """PATCH /api/statements/<id>/ {"account": <id>} — move a statement (and its
    rows) to another account. DELETE /api/statements/<id>/?delete_entries=1 —
    remove a statement; with delete_entries, also the expenses and income
    that were saved from it."""
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        statement = Statement.objects.filter(user=request.user, pk=pk).first()
        target = Account.objects.filter(user=request.user, pk=request.data.get("account")).first()
        if not statement or not target:
            return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            statement.account = target
            statement.save(update_fields=["account"])
            statement.transactions.update(account=target)
        return Response({"id": statement.id, "account": target.id})

    def delete(self, request, pk):
        statement = Statement.objects.filter(user=request.user, pk=pk).select_related("account").first()
        if not statement:
            return Response({"error": "Statement not found."}, status=status.HTTP_404_NOT_FOUND)
        with_entries = str(request.query_params.get("delete_entries", "")).lower() in ("1", "true", "yes")
        txs = statement.transactions.all()
        expense_ids = [i for i in txs.values_list("expense_id", flat=True) if i]
        income_ids = [i for i in txs.values_list("income_id", flat=True) if i]
        label = f"{statement.account.name} {statement.period_start} – {statement.period_end}"
        with transaction.atomic(), activity_context(quiet={"expense", "income"}):
            deleted = {"expenses": 0, "income": 0}
            if with_entries:
                deleted["expenses"] = Expense.objects.filter(user=request.user, id__in=expense_ids).delete()[0]
                deleted["income"] = Income.objects.filter(user=request.user, id__in=income_ids).delete()[0]
            statement.delete()
        log_activity(request.user, "import", "deleted", f"Statement {label}",
                     details={"filename": statement.filename, **deleted}, source="app", reason="")
        return Response({"deleted_expenses": deleted["expenses"], "deleted_income": deleted["income"]})


class AccountTransactionsView(APIView):
    """GET /api/statements/transactions/?month=YYYY-MM&account=<id>&q=<text>
    Every transaction on one account (or all of them) in a month."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = StatementTransaction.objects.filter(user=request.user)
        account = request.query_params.get("account")
        if account:
            qs = qs.filter(account_id=account)
        month = request.query_params.get("month")
        if not month:
            latest = qs.aggregate(m=Max("date"))["m"]
            month = f"{latest.year}-{latest.month:02d}" if latest else f"{date.today():%Y-%m}"
        bounds = _month_range(month)
        if not bounds:
            return Response({"error": "month must look like 2026-05."}, status=status.HTTP_400_BAD_REQUEST)
        qs = qs.filter(date__range=bounds)
        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
        rows = list(qs.select_related("account").order_by("date", "id"))
        money_in = sum((t.amount for t in rows if t.amount > 0), Decimal("0"))
        money_out = sum((-t.amount for t in rows if t.amount < 0), Decimal("0"))
        return Response({
            "month": month,
            "count": len(rows),
            "money_in": _money(money_in),
            "money_out": _money(money_out),
            "transactions": [{
                "id": t.id, "date": t.date.isoformat(), "title": t.title, "description": t.description,
                "amount": _money(t.amount), "kind": t.kind, "category": t.category,
                "account_id": t.account_id, "account": t.account.name, "statement_id": t.statement_id,
            } for t in rows],
        })


class RecurringChargesView(APIView):
    """GET /api/statements/recurring/ — pre-authorized debits, subscriptions and
    other charges that repeat, across all accounts."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        txs = list(StatementTransaction.objects.filter(user=request.user, amount__lt=0))
        ends = dict(Statement.objects.filter(user=request.user)
                    .values("account").annotate(end=Max("period_end")).values_list("account", "end"))
        items = find_recurring(txs, ends, lambda tx: tx.category or _match(EXPENSE_RULES, tx.description, "Other"))
        active = [i for i in items if i["monthly_amount"] and not i["own_transfer"]]
        total = sum((Decimal(i["monthly_amount"]) for i in active), Decimal("0"))
        by_account = {}
        for i in active:
            # A charge that moved between cards counts toward the latest one.
            account = i["occurrences"][-1]["account_id"]
            by_account[account] = by_account.get(account, Decimal("0")) + Decimal(i["monthly_amount"])
        return Response({
            "items": items,
            "monthly_total": _money(total),
            "pre_authorized_count": sum(1 for i in active if i["pre_authorized"]),
            "by_account": {str(k): _money(v) for k, v in by_account.items()},
        })
