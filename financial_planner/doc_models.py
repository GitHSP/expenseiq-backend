"""
Documents: the user's own files (policy contracts, illustrations, insurance
and bank statements, receipts), kept encrypted on the server. The file itself
is in DOCS_DIR (documents.py); this row is what's shown in the app.
"""
from django.conf import settings
from django.db import models


class Document(models.Model):
    KINDS = [("policy", "Policy / contract"), ("illustration", "Illustration"), ("statement", "Statement"),
             ("receipt", "Receipt"), ("other", "Other")]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="documents")
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=15, choices=KINDS, default="other")
    investment = models.ForeignKey("financial_planner.Investment", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="documents")
    account = models.ForeignKey("expenses.Account", on_delete=models.SET_NULL, null=True, blank=True, related_name="documents")
    statement = models.ForeignKey("expenses.Statement", on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="documents")
    content_type = models.CharField(max_length=60)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64, db_index=True)
    stored_as = models.CharField(max_length=64, unique=True)      # random name of the encrypted file
    doc_date = models.DateField(null=True, blank=True)            # e.g. the statement's date
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-doc_date", "-uploaded_at"]
