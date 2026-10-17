import hashlib
import logging
import re
from datetime                   import date, timedelta
from decimal                    import Decimal

from django.db                  import transaction
from rest_framework             import generics, status
from rest_framework.parsers     import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response    import Response
from rest_framework.views       import APIView

from .models      import Account, Expense, Income, Statement, StatementTransaction
from .accounts    import guess_account, match_existing_account, statement_period
from .serializers import ExpenseSerializer, IncomeSerializer
from .bulk        import MODELS as BULK_MODELS, bulk_delete
from financial_planner.models   import ChecklistItem
from financial_planner.services import toggle_checklist_item
from activity.context           import activity_context, log_activity
from .statement_import import (
    MAX_ROWS, StatementError, build_preview, match_checklist, parse_statement, suggest_flip,
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

# ─────────────────────────────────────────────
# STATEMENT IMPORT
# Two steps so nothing is saved until the user has reviewed it:
#   preview — parse an uploaded CSV/OFX/QFX file, return rows with guesses
#   commit  — save the rows the user confirmed
# ─────────────────────────────────────────────

logger = logging.getLogger(__name__)

MAX_STATEMENT_BYTES = 5 * 1024 * 1024
MAX_STATEMENT_FILES = 50                     # e.g. 6 months × 7 accounts in one go
MAX_UPLOAD_TOTAL_BYTES = 60 * 1024 * 1024    # keeps the small server's memory/disk safe


class StatementPreviewView(APIView):
    """
    POST /api/statements/preview/  (multipart: one or more "files")
    Several statements can be reviewed together so transfers between the
    user's own accounts (e.g. Simplii -> CIBC) can be paired up and skipped.
    """
    permission_classes = [IsAuthenticated]
    parser_classes     = [MultiPartParser]

    def post(self, request):
        uploads = request.FILES.getlist("files") or request.FILES.getlist("file")
        if not uploads:
            return Response({"error": "Choose a statement file to upload."}, status=status.HTTP_400_BAD_REQUEST)
        if len(uploads) > MAX_STATEMENT_FILES:
            return Response({"error": f"Upload at most {MAX_STATEMENT_FILES} statements at a time."}, status=status.HTTP_400_BAD_REQUEST)
        if sum(u.size for u in uploads) > MAX_UPLOAD_TOTAL_BYTES:
            return Response({"error": "These files add up to more than 60 MB — please upload them in two batches."},
                            status=status.HTTP_400_BAD_REQUEST)

        # One unreadable file shouldn't block the others: it's reported in
        # `skipped` and the rest are previewed.
        files, skipped, total_rows = [], [], 0
        for upload in uploads:
            if upload.size > MAX_STATEMENT_BYTES:
                skipped.append({"filename": upload.name, "error": "Too large (5 MB max per file)."})
                continue
            raw = upload.read()
            try:
                rows, info = parse_statement(upload.name, raw)
            except StatementError as e:
                skipped.append({"filename": upload.name, "error": str(e)})
                continue
            except Exception:
                logger.exception("Statement parse failed for %s", upload.name)
                skipped.append({"filename": upload.name, "error": "Something went wrong reading this file. Please try its CSV version."})
                continue
            total_rows += len(rows)
            files.append({"filename": upload.name, "rows": rows, "info": info, "flip": suggest_flip(rows, info),
                          "hash": hashlib.sha256(raw).hexdigest()})

        if not files:
            message = skipped[0]["error"] if len(skipped) == 1 else "None of these files could be read."
            return Response({"error": message, "skipped": skipped}, status=status.HTTP_400_BAD_REQUEST)

        if total_rows > MAX_ROWS:
            return Response({"error": f"These files have {total_rows} transactions — please import at most {MAX_ROWS} at a time."},
                            status=status.HTTP_400_BAD_REQUEST)

        # Which of the user's accounts each statement belongs to (a new one
        # is suggested when there's no match), and whether it's been
        # uploaded before.
        accounts = list(Account.objects.filter(user=request.user))
        uploaded = {s.file_hash: s for s in Statement.objects.filter(
            user=request.user, file_hash__in=[f["hash"] for f in files]).select_related("account")}
        file_info = []
        for f in files:
            guess = guess_account(f["filename"], f["info"], f["rows"])
            existing = match_existing_account(accounts, guess)
            start, end = statement_period(f["info"], f["rows"])
            before = uploaded.get(f["hash"])
            file_info.append({
                "filename": f["filename"], "format": f["info"]["format"],
                "suggest_flip": f["flip"], "count": len(f["rows"]),
                "file_hash": f["hash"],
                "period_start": start.isoformat(), "period_end": end.isoformat(),
                "account_id": before.account_id if before else (existing.id if existing else None),
                "account_guess": guess,
                "already_uploaded": {"date": before.uploaded_at.date().isoformat(), "account": before.account.name} if before else None,
            })

        return Response({
            "files": file_info,
            "accounts": [{"id": a.id, "name": a.name, "kind": a.kind} for a in accounts],
            "rows": match_checklist(request.user, build_preview(request.user, files), files),
            "skipped": skipped,
        })


class StatementImportView(APIView):
    """
    POST /api/statements/import/
    {"statements": [{"filename", "file_hash", "period_start", "period_end",
                     "account": {"id"} | {"name", "kind", "institution", "last4"},
                     "transactions": [{"date", "description", "title", "amount" (signed),
                                       "category", "kind": "transfer"|"duplicate"|"skipped",
                                       "import": {"kind": "expense"|"income", "title", "category"}?}]}],
     "checklist_items": [ids]}
    Every statement is kept with all its rows (for the Accounts page); rows
    with "import" are also saved as expenses/income. The older form
    {"filename", "rows": [{"kind", "title", "amount", "date", "category", "source"}]}
    saves just the rows. Everything is validated first; nothing is saved
    unless all of it is valid.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        statements = request.data.get("statements")
        rows = request.data.get("rows") or []
        tick_ids = request.data.get("checklist_items") or []
        if not isinstance(tick_ids, list) or not all(isinstance(i, int) for i in tick_ids):
            return Response({"error": "checklist_items must be a list of ids."}, status=status.HTTP_400_BAD_REQUEST)

        if statements is not None:
            if not isinstance(statements, list) or not statements:
                return Response({"error": "Nothing to import."}, status=status.HTTP_400_BAD_REQUEST)
            try:
                plans, entries = self._plan_statements(request.user, statements)
            except ValueError as e:
                return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        else:
            if not isinstance(rows, list) or not (rows or tick_ids):
                return Response({"error": "Nothing to import."}, status=status.HTTP_400_BAD_REQUEST)
            if len(rows) > MAX_ROWS:
                return Response({"error": f"Import at most {MAX_ROWS} rows at a time."}, status=status.HTTP_400_BAD_REQUEST)
            filename = str(request.data.get("filename") or "statement")[:100]
            plans = []
            entries = [(row, str(row.get("source") or filename) if isinstance(row, dict) else filename) for row in rows]

        serializers_to_save, errors = [], []
        for i, (row, source) in enumerate(entries):
            serializer, error = _entry_serializer(row, source)
            serializers_to_save.append(serializer)
            if error:
                errors.append({"row": i, "errors": error})
        if errors:
            return Response(
                {"error": f"{len(errors)} row(s) couldn't be imported — nothing was saved.", "details": errors[:20]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic(), activity_context(source="import", quiet={"expense", "income"}):
            saved = [s.save(user=request.user) for s in serializers_to_save]
            statements_saved = self._save_statements(request.user, plans, saved)
            # Same side effects as ticking in the Planner: debt payments
            # reduce the debt's balance, savings go to the emergency fund.
            ticked = 0
            for item in ChecklistItem.objects.filter(
                id__in=tick_ids, monthly_plan__user=request.user, is_completed=False,
            ):
                toggle_checklist_item(item)
                ticked += 1

        expenses = sum(1 for s in serializers_to_save if isinstance(s, ExpenseSerializer))
        sources = sorted({source for _, source in entries} | {p["filename"] for p in plans}) or ["statement"]
        totals = {"expense": Decimal("0"), "income": Decimal("0")}
        for s in serializers_to_save:
            totals["expense" if isinstance(s, ExpenseSerializer) else "income"] += s.validated_data["amount"]
        log_activity(
            request.user, "import", "imported", ", ".join(sources)[:255],
            details={
                "files": sources,
                "expenses": expenses, "expense_total": str(totals["expense"]),
                "income": len(serializers_to_save) - expenses, "income_total": str(totals["income"]),
                "ticked": ticked, "statements": statements_saved,
            },
            source="import", reason="",
        )
        return Response({
            "imported_expenses": expenses,
            "imported_income":   len(serializers_to_save) - expenses,
            "ticked_checklist":  ticked,
            "statements_saved":  statements_saved,
        }, status=status.HTTP_201_CREATED)

    # ── statements: check everything before saving anything ──
    def _plan_statements(self, user, statements):
        accounts = {a.id: a for a in Account.objects.filter(user=user)}
        plans, entries, total = [], [], 0
        for n, st in enumerate(statements, 1):
            if not isinstance(st, dict):
                raise ValueError(f"Statement {n} is not valid.")
            filename = str(st.get("filename") or "statement")[:255]
            txs = st.get("transactions")
            if not isinstance(txs, list) or not txs:
                raise ValueError(f"{filename} has no transactions.")
            total += len(txs)
            if total > MAX_ROWS:
                raise ValueError(f"Import at most {MAX_ROWS} transactions at a time.")

            acct = st.get("account") or {}
            if not isinstance(acct, dict):
                raise ValueError(f"Choose an account for {filename}.")
            if acct.get("id") is not None:
                if acct.get("id") not in accounts:
                    raise ValueError(f"Choose an account for {filename}.")
                account = {"id": acct["id"]}
            else:
                name = str(acct.get("name") or "").strip()[:100]
                if not name:
                    raise ValueError(f"Name the account for {filename}.")
                kind = acct.get("kind") if acct.get("kind") in dict(Account.KINDS) else "other"
                account = {"name": name, "kind": kind,
                           "institution": str(acct.get("institution") or "")[:50],
                           "last4": re.sub(r"\D", "", str(acct.get("last4") or ""))[-4:]}

            parsed = []
            for t in txs:
                if not isinstance(t, dict):
                    raise ValueError(f"{filename} has an invalid transaction.")
                try:
                    d = date.fromisoformat(str(t.get("date")))
                    amount = Decimal(str(t.get("amount"))).quantize(Decimal("0.01"))
                except (ValueError, ArithmeticError):
                    raise ValueError(f"{filename} has a transaction with an invalid date or amount.")
                description = str(t.get("description") or t.get("title") or "")[:255]
                entry = t.get("import")
                index = None
                if entry is not None:
                    if not isinstance(entry, dict):
                        raise ValueError(f"{filename} has an invalid transaction.")
                    index = len(entries)
                    entries.append(({**entry, "amount": str(abs(amount)), "date": d.isoformat()}, filename))
                hint = t.get("kind") if t.get("kind") in ("transfer", "duplicate", "skipped") else "skipped"
                parsed.append({
                    "date": d, "amount": amount, "description": description,
                    "title": str((entry or {}).get("title") or t.get("title") or description)[:255],
                    "category": str((entry or {}).get("category") or t.get("category") or "")[:50],
                    "kind": entry.get("kind") if entry else hint, "entry": index,
                })

            try:
                start = date.fromisoformat(str(st.get("period_start")))
                end = date.fromisoformat(str(st.get("period_end")))
            except ValueError:
                start, end = min(p["date"] for p in parsed), max(p["date"] for p in parsed)
            plans.append({"filename": filename, "file_hash": str(st.get("file_hash") or "")[:64],
                          "account": account, "start": min(start, end), "end": max(start, end), "transactions": parsed})
        return plans, entries

    def _save_statements(self, user, plans, saved):
        count = 0
        for plan in plans:
            spec = plan["account"]
            if "id" in spec:
                account = Account.objects.get(id=spec["id"], user=user)
            else:
                account, _ = Account.objects.get_or_create(
                    user=user, name=spec["name"],
                    defaults={"kind": spec["kind"], "institution": spec["institution"], "last4": spec["last4"]},
                )
            # The same file again: its rows are already on record.
            if plan["file_hash"] and Statement.objects.filter(user=user, file_hash=plan["file_hash"]).exists():
                continue
            statement = Statement.objects.create(
                user=user, account=account, filename=plan["filename"], file_hash=plan["file_hash"],
                period_start=plan["start"], period_end=plan["end"],
            )
            count += 1
            # Overlapping statements (e.g. a PDF and a CSV export of the same
            # weeks) shouldn't list a transaction twice.
            existing = {}
            for tx in StatementTransaction.objects.filter(
                account=account, date__range=(plan["start"] - timedelta(days=3), plan["end"] + timedelta(days=3)),
            ):
                existing.setdefault((tx.date, tx.amount, tx.description.lower()), tx)
            new = []
            for p in plan["transactions"]:
                obj = saved[p["entry"]] if p["entry"] is not None else None
                link = {"expense": obj} if p["kind"] == "expense" else {"income": obj} if p["kind"] == "income" else {}
                earlier = existing.get((p["date"], p["amount"], p["description"].lower()))
                if earlier is not None:
                    if obj is not None and not (earlier.expense_id or earlier.income_id):
                        for field, value in link.items():
                            setattr(earlier, field, value)
                        earlier.kind = p["kind"]
                        earlier.save(update_fields=[*link, "kind"])
                    continue
                new.append(StatementTransaction(
                    user=user, statement=statement, account=account, date=p["date"], amount=p["amount"],
                    description=p["description"], title=p["title"], category=p["category"], kind=p["kind"], **link,
                ))
            StatementTransaction.objects.bulk_create(new)
        return count


def _entry_serializer(row, source):
    """(serializer, errors) for one expense/income row to import."""
    if not isinstance(row, dict) or row.get("kind") not in ("expense", "income"):
        return None, {"kind": ["Must be 'expense' or 'income'."]}
    data = {
        "title":    row.get("title"),
        "amount":   row.get("amount"),
        "category": row.get("category"),
        "date":     row.get("date"),
        "notes":    f"Imported from {str(source)[:100]}",
    }
    if row["kind"] == "expense":
        data["tags"] = ["imported"]
        serializer = ExpenseSerializer(data=data)
    else:
        serializer = IncomeSerializer(data=data)
    return serializer, (None if serializer.is_valid() else serializer.errors)


# ─────────────────────────────────────────────
# BULK DELETE
# ─────────────────────────────────────────────

class BulkDeleteView(APIView):
    """
    POST /api/expenses/bulk-delete/  or  /api/income/bulk-delete/
    {"ids": [1, 2, 3], "label": "September 2026"}
    Only the caller's own entries are deleted; unknown ids are ignored.
    """
    permission_classes = [IsAuthenticated]
    kind = "expense"

    def post(self, request):
        ids = request.data.get("ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
            return Response({"error": "ids must be a non-empty list of numbers."}, status=status.HTTP_400_BAD_REQUEST)
        if len(ids) > 2000:
            return Response({"error": "Delete at most 2000 at a time."}, status=status.HTTP_400_BAD_REQUEST)
        model = BULK_MODELS[self.kind]
        qs = model.objects.filter(user=request.user, id__in=ids)
        count, total = bulk_delete(request.user, self.kind, qs, str(request.data.get("label") or "")[:60])
        return Response({"deleted": count, "total": str(total)})
