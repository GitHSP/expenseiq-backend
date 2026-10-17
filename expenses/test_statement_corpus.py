"""Runs the realistic statement corpus (tools/statement_qa) as a test.
Needs reportlab (requirements-dev.txt); skipped where it isn't installed."""

import importlib.util
import sys
import unittest
from pathlib import Path

from django.test import SimpleTestCase

QA_DIR = Path(__file__).resolve().parents[1] / "tools" / "statement_qa"


@unittest.skipUnless(importlib.util.find_spec("reportlab"), "reportlab not installed")
class StatementCorpusTests(SimpleTestCase):
    def test_every_statement_imports_exactly(self):
        sys.path.insert(0, str(QA_DIR))
        try:
            from corpus import build
            from run import check
        finally:
            sys.path.remove(str(QA_DIR))
        for st in build():
            with self.subTest(statement=st.name):
                result = check(st)
                self.assertIsNone(result["error"])
                self.assertEqual(result["missing"], [], "transactions not found")
                self.assertEqual(result["extra"], [], "transactions that shouldn't be there")
                self.assertEqual(result["junk"], [], "titles containing summary/category text")
