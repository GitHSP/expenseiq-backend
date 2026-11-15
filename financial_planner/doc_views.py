"""
Documents API (/api/fp/documents/): list, upload (multipart "file"), rename,
delete, and download (decrypted, owner only). A bank or card statement that
was imported is linked to its statement by the file's fingerprint.
"""
import hashlib
from datetime import date

from django.http import HttpResponse
from rest_framework import status
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import documents as store
from .doc_models import Document

MAX_PER_USER = 500


def doc_json(d):
    return {"id": d.id, "name": d.name, "kind": d.kind, "content_type": d.content_type, "size": d.size,
            "investment": d.investment_id, "account": d.account_id, "statement": d.statement_id,
            "doc_date": d.doc_date.isoformat() if d.doc_date else None, "uploaded_at": d.uploaded_at.isoformat()}


def _owned(model, user, pk):
    if pk in (None, "", "null"):
        return None, None
    try:
        obj = model.objects.filter(user=user, pk=int(pk)).first()
    except (TypeError, ValueError):
        obj = None
    return obj, (None if obj else f"{model.__name__} not found.")


class DocumentListView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, JSONParser]

    def get(self, request):
        qs = Document.objects.filter(user=request.user)
        for field in ("investment", "account", "statement"):
            if request.query_params.get(field):
                qs = qs.filter(**{f"{field}_id": request.query_params[field]})
        return Response([doc_json(d) for d in qs[:200]])

    def post(self, request):
        from expenses.models import Account, Statement
        from .models import Investment
        upload = request.FILES.get("file")
        if not upload:
            return Response({"error": "Choose a file."}, status=status.HTTP_400_BAD_REQUEST)
        if upload.size > store.MAX_BYTES:
            return Response({"error": "That file is over 20 MB."}, status=status.HTTP_400_BAD_REQUEST)
        if Document.objects.filter(user=request.user).count() >= MAX_PER_USER:
            return Response({"error": "You've reached 500 documents. Delete some old ones first."}, status=status.HTTP_400_BAD_REQUEST)
        raw = upload.read()
        ctype = store.sniff(raw[:16])
        if not ctype:
            return Response({"error": "Only PDFs and photos (JPG, PNG, HEIC) can be stored."}, status=status.HTTP_400_BAD_REQUEST)
        digest = hashlib.sha256(raw).hexdigest()
        same = Document.objects.filter(user=request.user, sha256=digest).first()
        if same:                                                 # already stored: don't keep it twice
            return Response({**doc_json(same), "already": True})

        investment, e1 = _owned(Investment, request.user, request.data.get("investment"))
        account, e2 = _owned(Account, request.user, request.data.get("account"))
        if e1 or e2:
            return Response({"error": e1 or e2}, status=status.HTTP_400_BAD_REQUEST)
        kind = request.data.get("kind") if request.data.get("kind") in dict(Document.KINDS) else "other"
        doc_date = None
        if request.data.get("doc_date"):
            try:
                doc_date = date.fromisoformat(str(request.data["doc_date"]))
            except ValueError:
                return Response({"error": "doc_date must be a date like 2026-10-16."}, status=status.HTTP_400_BAD_REQUEST)
        # An imported bank or card statement: link it to its statement and account.
        statement = Statement.objects.filter(user=request.user, file_hash=digest).select_related("account").first()
        if statement:
            kind, account = "statement", account or statement.account
            doc_date = doc_date or statement.period_end
        name = str(request.data.get("name") or upload.name or "Document").strip()[:150]
        doc = Document.objects.create(user=request.user, name=name, kind=kind, investment=investment, account=account,
                                      statement=statement, content_type=ctype, size=len(raw), sha256=digest,
                                      stored_as=store.save(raw), doc_date=doc_date)
        return Response(doc_json(doc), status=status.HTTP_201_CREATED)


class DocumentDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, pk):
        return Document.objects.filter(user=request.user, pk=pk).first()

    def get(self, request, pk):
        """The file itself, decrypted. ?inline=1 opens it in the browser."""
        doc = self._get(request, pk)
        raw = store.load(doc.stored_as) if doc else None
        if raw is None:
            return Response({"error": "Document not found."}, status=status.HTTP_404_NOT_FOUND)
        response = HttpResponse(raw, content_type=doc.content_type)
        how = "inline" if request.query_params.get("inline") else "attachment"
        safe = "".join(c for c in doc.name if c.isalnum() or c in " ._-()")[:100] or "document"
        response["Content-Disposition"] = f'{how}; filename="{safe}"'
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response

    def patch(self, request, pk):
        doc = self._get(request, pk)
        if not doc:
            return Response({"error": "Document not found."}, status=status.HTTP_404_NOT_FOUND)
        if "name" in request.data:
            doc.name = str(request.data["name"]).strip()[:150] or doc.name
        if request.data.get("kind") in dict(Document.KINDS):
            doc.kind = request.data["kind"]
        doc.save()
        return Response(doc_json(doc))

    def delete(self, request, pk):
        doc = self._get(request, pk)
        if doc:
            store.remove(doc.stored_as)
            doc.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
