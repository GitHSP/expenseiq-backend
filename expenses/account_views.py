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
        from .income_policy import counts_as_income, deposit_class, income_words
        from .income_policy import is_charge
        from .models import MerchantRule
        from .rules import bill_share, is_shared, shared_matchers, shared_name, shares_set
        words = income_words(request.user)
        bill_rules = list(MerchantRule.objects.filter(user=request.user, treat_as="bill"))
        at_share = shares_set(request.user)
        shared = shared_matchers(request.user)
        bills = shared_matchers(request.user, "bill")
        months = {}
        rows = (StatementTransaction.objects.filter(user=request.user)
                .values_list("date", "amount", "account__kind", "kind", "title", "description"))
        for d, amount, acct_kind, kind, title, desc in rows:
            m = months.setdefault(f"{d.year}-{d.month:02d}",
                                  {"income": Decimal("0"), "expenses": Decimal("0"), "shared": Decimal("0"), "by": {},
                                   "bills": {}, "mine": {}})
            text = f"{title} {desc}"
            if kind != "transfer" and is_charge(text) and not (amount > 0 and is_shared(text, shared)):
                continue                         # interest and fees aren't expenses
            share = None
            if amount < 0 and kind != "transfer" and bills and is_shared(text, bills):
                bill = shared_name(text, bills)          # rent, utilities... paid in full
                m["bills"][bill] = m["bills"].get(bill, Decimal("0")) - amount
                share = bill_share(bill_rules, text, -amount)
                m["mine"][bill] = m["mine"].get(bill, Decimal("0")) + (share if share is not None else -amount)
            if amount > 0 and acct_kind != "credit_card" and shared and is_shared(f"{title} {desc}", shared):
                m["shared"] += amount            # a roommate's share of the bills: lowers spending
                who = shared_name(f"{title} {desc}", shared)
                m["by"][who] = m["by"].get(who, Decimal("0")) + amount
            elif kind == "transfer":
                continue
            elif amount > 0 and acct_kind != "credit_card":
                if counts_as_income(kind, f"{title} {desc}", words):
                    m["income"] += amount
                elif deposit_class(f"{title} {desc}", words) == "refund":
                    m["expenses"] -= amount      # a refund or reversal lowers spending
                # anything else coming in (friends, own transfers) isn't income
            else:
                m["expenses"] += share if share is not None else -amount   # card refunds (positive) lower spending
        # With shares set, bills already count only the user's part; what
        # roommates send just pays them back. Without, it lowers spending.
        offset = (lambda v: Decimal("0")) if at_share else (lambda v: v["shared"])
        out = [{"month": k, "income": _money(v["income"]), "expenses": _money(v["expenses"] - offset(v)),
                "net": _money(v["income"] - v["expenses"] + offset(v)),
                "shared_by_others": _money(v["shared"]),
                "shared_by": {who: _money(a) for who, a in sorted(v["by"].items())},
                "shared_bills": {b: _money(a) for b, a in sorted(v["bills"].items())},
                "my_shares": {b: _money(a) for b, a in sorted(v["mine"].items())},
                "bills_at_share": at_share} for k, v in sorted(months.items())]
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


class TransactionDetailView(APIView):
    """PATCH /api/statements/transactions/<id>/ {kind: expense|income|transfer|skipped, category?}
    Change what a statement row is. An expense or income entry is created
    for it (or removed), so the dashboard, planner and Aurora follow."""
    permission_classes = [IsAuthenticated]
    KINDS = ("expense", "income", "transfer", "skipped")

    def patch(self, request, pk):
        tx = (StatementTransaction.objects.filter(user=request.user, pk=pk)
              .select_related("expense", "income", "statement").first())
        if tx is None:
            return Response({"error": "Transaction not found."}, status=status.HTTP_404_NOT_FOUND)
        kind = request.data.get("kind")
        if kind not in self.KINDS:
            return Response({"error": "kind must be expense, income, transfer or skipped."},
                            status=status.HTTP_400_BAD_REQUEST)
        category = request.data.get("category") or ""
        with transaction.atomic(), activity_context(source="app", reason=f"Changed on statement {tx.statement.filename}"):
            if kind != "expense" and tx.expense_id:
                tx.expense.delete()
                tx.expense = None
            if kind != "income" and tx.income_id:
                tx.income.delete()
                tx.income = None
            notes = f"Imported from {tx.statement.filename}"
            if kind == "expense":
                valid = {c[0] for c in Expense.CATEGORIES}
                cat = category if category in valid else tx.category if tx.category in valid else \
                    _match(EXPENSE_RULES, tx.description, "Other")
                if tx.expense_id:
                    if tx.expense.category != cat:
                        tx.expense.category = cat
                        tx.expense.save(update_fields=["category"])
                else:
                    tx.expense = Expense.objects.create(user=request.user, title=tx.title, amount=abs(tx.amount),
                                                        category=cat, date=tx.date, notes=notes)
                tx.category = cat
            elif kind == "income":
                valid = {c[0] for c in Income.CATEGORIES}
                cat = category if category in valid else tx.category if tx.category in valid else "Other"
                if tx.income_id:
                    if tx.income.category != cat:
                        tx.income.category = cat
                        tx.income.save(update_fields=["category"])
                else:
                    tx.income = Income.objects.create(user=request.user, title=tx.title, amount=abs(tx.amount),
                                                      category=cat, date=tx.date, notes=notes)
                tx.category = cat
            tx.kind = kind
            tx.save(update_fields=["kind", "category", "expense", "income"])
        if tx.account.debt_id:
            sync_debt(tx.account)
        return Response({"id": tx.id, "kind": tx.kind, "category": tx.category})


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


class RuleListView(APIView):
    """GET/POST /api/statements/rules/ — the user's tidy-up rules (the same ones
    Aurora sets), so they can be managed without her."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import MerchantRule
        from .rules import share_label
        return Response({"rules": [{"id": r.id, "match": r.match, "rename_to": r.rename_to, "category": r.category,
                                    "treat_as": r.treat_as, "my_share": share_label(r)}
                                   for r in MerchantRule.objects.filter(user=request.user)]})

    def post(self, request):
        from assistant.personal import add_rule
        d = request.data
        result = add_rule(request.user, d.get("match"), rename_to=d.get("rename_to") or "",
                          category=d.get("category") or "", treat_as=d.get("treat_as") or "",
                          my_share=d.get("my_share") or "")
        return Response(result, status=status.HTTP_201_CREATED if result.get("ok") else status.HTTP_400_BAD_REQUEST)


class RuleDetailView(APIView):
    """DELETE /api/statements/rules/<id>/"""
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        from .models import MerchantRule
        deleted, _ = MerchantRule.objects.filter(user=request.user, pk=pk).delete()
        if not deleted:
            return Response({"error": "Rule not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)
