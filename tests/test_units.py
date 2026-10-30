"""Unit tests for parsing, fact extraction, routing, retrieval and security helpers."""
from __future__ import annotations

import unittest
from decimal import Decimal

from tests.helpers import ROOT  # noqa: F401  (sets sys.path)

from advisor import parsing
from advisor.facts import extract_financial_facts, extract_policy_facts, parse_amount
from advisor.router import classify_category, route_question
from advisor.security import allowed_classifications, hash_password, verify_password

SAMPLES = ROOT / "sample_docs"


class TestParsing(unittest.TestCase):
    def test_pdf_pages_are_preserved(self):
        pages = parsing.extract_pages("r.pdf", (SAMPLES / "finance_q1_2026_report.pdf").read_bytes())
        self.assertEqual([p.number for p in pages], [1, 2])
        self.assertIn("Revenue: AED 4,200,000", pages[0].text)
        chunks = parsing.chunk_pages(pages)
        self.assertTrue(all(c.page in (1, 2) for c in chunks))

    def test_docx_tables_become_rows(self):
        pages = parsing.extract_pages("r.docx", (SAMPLES / "finance_q2_2026_report.docx").read_bytes())
        self.assertIn("Operating profit | AED 828,700", pages[0].text)

    def test_rejections(self):
        with self.assertRaises(parsing.ParseError):
            parsing.extract_pages("a.pdf", b"not a pdf at all")
        with self.assertRaises(parsing.ParseError):
            parsing.extract_pages("a.pdf", (SAMPLES / "bad" / "corrupt_report.pdf").read_bytes())
        with self.assertRaises(parsing.ParseError):
            parsing.extract_pages("a.exe", b"MZ....")
        with self.assertRaises(parsing.ParseError):
            parsing.extract_pages("a.txt", b"   ")

    def test_metadata(self):
        self.assertEqual(parsing.find_effective_date("Effective date: 1 January 2025."), "2025-01-01")
        self.assertEqual(parsing.find_effective_date("Effective date: 2026-03-01"), "2026-03-01")
        self.assertEqual(parsing.find_period("Reporting period: Q2 2026"), "Q2 2026")


class TestFacts(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(parse_amount("4,200,000"), Decimal("4200000"))
        self.assertEqual(parse_amount("(180,000)"), Decimal("-180000"))
        self.assertEqual(parse_amount("4.2", "million"), Decimal("4200000"))

    def test_financial_lines(self):
        text = ("Revenue: AED 4,200,000\n| Cost of goods sold | AED 2,520,000 |\n"
                "Q1 2026 revenue (preliminary): AED 4,150,000\nGross margin: 40%\nSales: 12 months of growth")
        facts = extract_financial_facts(text, "Q2 2026")
        self.assertEqual([(f.metric, f.period, f.value) for f in facts], [
            ("revenue", "Q2 2026", Decimal("4200000")),
            ("cogs", "Q2 2026", Decimal("2520000")),
            ("revenue", "Q1 2026", Decimal("4150000")),
        ])

    def test_policy_sentences(self):
        text = ("Full-time employees are entitled to 22 working days of paid annual leave per year. "
                "Annual leave requests must be submitted at least 14 days in advance. "
                "Up to 5 days of unused annual leave may be carried over.")
        facts = [(f.topic, f.value, f.unit) for f in extract_policy_facts(text)]
        self.assertEqual(facts, [("annual_leave", "22", "working days"), ("carry_over", "5", "days")])


class TestRouting(unittest.TestCase):
    def test_routes(self):
        cases = {
            "What was the gross margin in Q2?": "finance",
            "How much did we spend on marketing versus budget?": "finance",
            "How many vacation days do I get?": "hr",
            "Can I work from home on Fridays?": "hr",
            "What is the notice period for grade 5?": "hr",
            "Who is the CEO?": "general",
            "What services does the company offer?": "general",
        }
        for q, expected in cases.items():
            self.assertEqual(route_question(q).advisor, expected, q)

    def test_category_detection(self):
        text = (SAMPLES / "salary_bands_2026_confidential.txt").read_text()
        self.assertEqual(classify_category(text), "hr")


class TestSecurityHelpers(unittest.TestCase):
    def test_password_hashing(self):
        h = hash_password("S3cret!pass")
        self.assertTrue(verify_password("S3cret!pass", h))
        self.assertFalse(verify_password("wrong", h))
        self.assertNotIn("S3cret", h)
        self.assertNotEqual(h, hash_password("S3cret!pass"))       # salted

    def test_policy(self):
        self.assertEqual(allowed_classifications("user"), ("public",))
        self.assertEqual(allowed_classifications("admin"), ("public", "confidential"))
        self.assertEqual(allowed_classifications("unknown-role"), ("public",))


if __name__ == "__main__":
    unittest.main()
