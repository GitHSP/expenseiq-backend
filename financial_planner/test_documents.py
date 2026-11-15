import hashlib
import shutil
import tempfile
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from expenses.models import Account, Statement
from . import documents as store
from .doc_models import Document
from .models import Investment

PDF = b"%PDF-1.4\n% a tiny test pdf\n"


class DocumentTests(TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.settings_patch = override_settings(DOCS_DIR=self.dir)
        self.settings_patch.enable()
        self.user = get_user_model().objects.create_user(email="doc@example.test", username="doc", password="x-Test-12345")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.inv = Investment.objects.create(user=self.user, kind="insurance", name="Life", currency="CAD",
                                             start_date=date(2022, 1, 1), payment=Decimal("50"))

    def tearDown(self):
        self.settings_patch.disable()
        shutil.rmtree(self.dir, ignore_errors=True)

    def upload(self, raw=PDF, name="contract.pdf", **data):
        return self.client.post("/api/fp/documents/", {"file": SimpleUploadedFile(name, raw), **data}, format="multipart")

    def test_upload_encrypted_download_delete(self):
        r = self.upload(kind="policy", investment=self.inv.id)
        self.assertEqual(r.status_code, 201, r.data)
        doc = Document.objects.get()
        self.assertEqual((doc.kind, doc.investment_id, doc.content_type), ("policy", self.inv.id, "application/pdf"))
        on_disk = open(f"{self.dir}/{doc.stored_as}", "rb").read()
        self.assertNotIn(b"tiny test pdf", on_disk)                       # encrypted at rest
        got = self.client.get(f"/api/fp/documents/{doc.id}/")
        self.assertEqual(got.content, PDF)
        self.assertEqual(self.client.get(f"/api/fp/documents/?investment={self.inv.id}").data[0]["name"], "contract.pdf")
        self.assertTrue(self.upload().data["already"])                   # same file again: not stored twice
        self.assertEqual(Document.objects.count(), 1)
        self.client.delete(f"/api/fp/documents/{doc.id}/")
        self.assertFalse(Document.objects.exists())
        self.assertEqual(len(list(__import__("os").listdir(self.dir))), 0)

    def test_only_pdfs_and_photos_and_only_mine(self):
        self.assertEqual(self.upload(b"MZ\x90\x00 an exe", "x.pdf").status_code, 400)   # the name lies
        self.assertEqual(self.upload(b"\xff\xd8\xff\xe0 jpeg", "photo.jpg").status_code, 201)
        doc = Document.objects.get()
        other = get_user_model().objects.create_user(email="o@example.test", username="o", password="x-Test-12345")
        c = APIClient()
        c.force_authenticate(other)
        self.assertEqual(c.get(f"/api/fp/documents/{doc.id}/").status_code, 404)
        self.assertEqual(c.get("/api/fp/documents/").data, [])
        self.assertEqual(c.post("/api/fp/documents/", {"file": SimpleUploadedFile("c.pdf", PDF + b"x"),
                                                       "investment": self.inv.id}, format="multipart").status_code, 400)
        self.assertEqual(APIClient().get(f"/api/fp/documents/{doc.id}/").status_code, 401)

    def test_an_imported_statement_links_itself(self):
        acc = Account.objects.create(user=self.user, name="Visa", kind="credit_card")
        st = Statement.objects.create(user=self.user, account=acc, filename="nov.pdf", file_hash=hashlib.sha256(PDF).hexdigest(),
                                      period_start=date(2026, 10, 1), period_end=date(2026, 10, 31))
        r = self.upload(name="nov.pdf")
        self.assertEqual((r.data["kind"], r.data["statement"], r.data["account"], r.data["doc_date"]),
                         ("statement", st.id, acc.id, "2026-10-31"))
