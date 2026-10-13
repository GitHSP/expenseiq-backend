from decimal                    import Decimal

from django.db                  import transaction
from rest_framework             import generics, status
from rest_framework.parsers     import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response    import Response
from rest_framework.views       import APIView

from .models      import Expense, Income
from .serializers import ExpenseSerializer, IncomeSerializer
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

MAX_STATEMENT_BYTES = 5 * 1024 * 1024
MAX_STATEMENT_FILES = 10


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

        files, total_rows = [], 0
        for upload in uploads:
            if upload.size > MAX_STATEMENT_BYTES:
                return Response({"error": f"{upload.name} is too large (5 MB max per file)."}, status=status.HTTP_400_BAD_REQUEST)
            try:
                rows, info = parse_statement(upload.name, upload.read())
            except StatementError as e:
                return Response({"error": f"{upload.name}: {e}"}, status=status.HTTP_400_BAD_REQUEST)
            total_rows += len(rows)
            files.append({"filename": upload.name, "rows": rows, "info": info, "flip": suggest_flip(rows, info)})

        if total_rows > MAX_ROWS:
            return Response({"error": f"These files have {total_rows} transactions — please import at most {MAX_ROWS} at a time."},
                            status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "files": [
                {"filename": f["filename"], "format": f["info"]["format"],
                 "suggest_flip": f["flip"], "count": len(f["rows"])}
                for f in files
            ],
            "rows": match_checklist(request.user, build_preview(request.user, files), files),
        })


class StatementImportView(APIView):
    """
    POST /api/statements/import/
    {"filename": "...", "rows": [{"kind": "expense"|"income", "title", "amount",
                                  "date", "category"}, ...]}
    All rows are validated first; nothing is saved unless every row is valid.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        rows = request.data.get("rows") or []
        if not isinstance(rows, list) or not (rows or request.data.get("checklist_items")):
            return Response({"error": "Nothing to import."}, status=status.HTTP_400_BAD_REQUEST)
        if len(rows) > MAX_ROWS:
            return Response({"error": f"Import at most {MAX_ROWS} rows at a time."}, status=status.HTTP_400_BAD_REQUEST)

        filename = str(request.data.get("filename") or "statement")[:100]
        serializers_to_save, errors = [], []
        for i, row in enumerate(rows):
            if not isinstance(row, dict) or row.get("kind") not in ("expense", "income"):
                errors.append({"row": i, "errors": {"kind": ["Must be 'expense' or 'income'."]}})
                continue
            data = {
                "title":    row.get("title"),
                "amount":   row.get("amount"),
                "category": row.get("category"),
                "date":     row.get("date"),
                "notes":    f"Imported from {str(row.get('source') or filename)[:100]}",
            }
            if row["kind"] == "expense":
                data["tags"] = ["imported"]
                serializer = ExpenseSerializer(data=data)
            else:
                serializer = IncomeSerializer(data=data)
            if serializer.is_valid():
                serializers_to_save.append(serializer)
            else:
                errors.append({"row": i, "errors": serializer.errors})

        if errors:
            return Response(
                {"error": f"{len(errors)} row(s) couldn't be imported — nothing was saved.", "details": errors[:20]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tick_ids = request.data.get("checklist_items") or []
        if not isinstance(tick_ids, list) or not all(isinstance(i, int) for i in tick_ids):
            return Response({"error": "checklist_items must be a list of ids."}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic(), activity_context(source="import", quiet={"expense", "income"}):
            for serializer in serializers_to_save:
                serializer.save(user=request.user)
            # Same side effects as ticking in the Planner: debt payments
            # reduce the debt's balance, savings go to the emergency fund.
            ticked = 0
            for item in ChecklistItem.objects.filter(
                id__in=tick_ids, monthly_plan__user=request.user, is_completed=False,
            ):
                toggle_checklist_item(item)
                ticked += 1

        expenses = sum(1 for s in serializers_to_save if isinstance(s, ExpenseSerializer))
        sources = sorted({str(r.get("source") or filename) for r in rows if isinstance(r, dict)}) or [filename]
        totals = {"expense": Decimal("0"), "income": Decimal("0")}
        for s in serializers_to_save:
            totals["expense" if isinstance(s, ExpenseSerializer) else "income"] += s.validated_data["amount"]
        log_activity(
            request.user, "import", "imported", ", ".join(sources)[:255],
            details={
                "files": sources,
                "expenses": expenses, "expense_total": str(totals["expense"]),
                "income": len(serializers_to_save) - expenses, "income_total": str(totals["income"]),
                "ticked": ticked,
            },
            source="import", reason="",
        )
        return Response({
            "imported_expenses": expenses,
            "imported_income":   len(serializers_to_save) - expenses,
            "ticked_checklist":  ticked,
        }, status=status.HTTP_201_CREATED)
