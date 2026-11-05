"""Unit tests for the knowledge system.

Self-contained: fixtures are tiny synthetic pages, so the suite runs
anywhere with nothing but the standard library:

    python3 -m unittest discover -s tests -v

The integration behaviour against the real snapshots is covered by
tools/simulate.py and the grading harness itself.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import branches
import extract
import mdparse
import prose
import query
import solution
from store import FactStore


# ---------------------------------------------------------------------------
# Contract-shaped stubs (the harness supplies the real ones at grade time)
# ---------------------------------------------------------------------------

@dataclass
class Page:
    url: str
    title: str
    markdown: str
    fetched_at: str = "2026-09-28T00:00:00Z"


@dataclass
class Snapshot:
    snapshot_id: str
    taken_on: str
    pages: list = field(default_factory=list)


RATES_MD_V1 = """# Dupaco Rates

## Certificates

| Certificate | APY | Dividend Rate |
| --- | --- | --- |
| 10 Month Fixed Certificate Special | 3.90% | 3.83% |
| 17 Month Fixed Certificate Special | 4.10% | 4.02% |
"""

RATES_MD_V2 = RATES_MD_V1.replace("3.90%", "4.00%").replace("3.83%", "3.92%")

CALC_MD = """# Certificate Calculator

| Certificate | APY |
| --- | --- |
| 10 Month Fixed Certificate Special | 3.50% |
"""


def make_ks() -> object:
    ks = solution.build()
    ks.ingest(Snapshot("snap-may", "2026-05-12", [
        Page("https://www.dupaco.com/rates", "Rates", RATES_MD_V1),
    ]))
    ks.ingest(Snapshot("snap-sep", "2026-09-28", [
        Page("https://www.dupaco.com/rates", "Rates", RATES_MD_V2),
        Page("https://www.dupaco.com/calculators/certificate", "Calculator", CALC_MD),
    ]))
    return ks


# ---------------------------------------------------------------------------

class TestMdparse(unittest.TestCase):
    def test_authority_ordering(self):
        self.assertEqual(mdparse.page_authority("https://www.dupaco.com/rates")[0], 50)
        self.assertEqual(mdparse.page_authority("https://www.dupaco.com/personal/savings-account")[0], 40)
        self.assertEqual(mdparse.page_authority("https://www.dupaco.com/calculators/auto")[0], 20)
        self.assertEqual(mdparse.page_authority("https://www.dupaco.com/newsroom/story")[0], 10)
        self.assertGreater(mdparse.page_authority("https://www.dupaco.com/rates")[0],
                           mdparse.page_authority("https://www.dupaco.com/calculators/x")[0])

    def test_parse_tables(self):
        tables = mdparse.parse_tables(RATES_MD_V1)
        self.assertTrue(tables)
        cert = next(t for t in tables if "APY" in t.headers)
        self.assertIn(["10 Month Fixed Certificate Special", "3.90%", "3.83%"], cert.rows)

    def test_clean_text(self):
        out = mdparse.clean_text("**Bold** [Rates](https://x.example) and _more_")
        self.assertNotIn("**", out)
        self.assertIn("Rates", out)


class TestExtract(unittest.TestCase):
    def test_term_and_flags(self):
        q = extract.parse_qualifiers("10 Month Fixed Jumbo Certificate Special", "")
        self.assertEqual(q["term_months"], 10)
        self.assertIn("jumbo", q["flags"])
        self.assertIn("special", q["flags"])

    def test_balance_bands(self):
        q = extract.parse_qualifiers("$5,000 - $24,999.99", "")
        self.assertEqual(q["balance_band"], (5000.0, 24999.99))
        q2 = extract.parse_qualifiers("$100,000+", "")
        self.assertEqual(q2["balance_band"], (100000.0, None))

    def test_model_year_band(self):
        q = extract.parse_qualifiers("2024-27 Model Year", "")
        self.assertEqual(q["model_years"], (2024, 2027))

    def test_segment_from_text(self):
        self.assertEqual(extract.parse_qualifiers("Business Platinum Visa", "")["segment"],
                         "business")

    def test_find_value(self):
        v = extract.find_value("5.39%")
        self.assertEqual((v.nums, v.unit, v.text), ((5.39,), "%", "5.39%"))
        self.assertIsNone(extract.find_value("no numbers here"))


class TestStore(unittest.TestCase):
    def test_authority_resolution_and_time_travel(self):
        ks = make_ks()
        store = ks.store

        def cert_winner(state):
            return next(f for f in state.winners().values()
                        if "10 Month" in f.subject and f.metric == "APY")

        # Rate sheet (4.00) beats the calculator page (3.50) in Sep...
        self.assertEqual(cert_winner(store.state_as_of(None)).value.text, "4.00%")
        # ...and as of May the answer was 3.90.
        self.assertEqual(cert_winner(store.state_as_of("2026-05-15")).value.text, "3.90%")

    def test_changed_facts(self):
        ks = make_ks()
        changed = ks.store.changed_facts(ks.store.states[0], ks.store.states[1])
        self.assertTrue(any(old == "3.90%" and new == "4.00%"
                            for _k, old, new in changed))


class TestQuery(unittest.TestCase):
    def test_balance_point_containment(self):
        band = {"balance_band": (5000.0, 24999.99)}
        self.assertTrue(query._qualifier_compatible({}, band, [], q_point=10000.0))
        self.assertFalse(query._qualifier_compatible({}, band, [], q_point=100000.0))
        self.assertEqual(query.question_balance_point("I keep $15,000 in savings"), 15000.0)

    def test_model_year_containment(self):
        f = {"model_years": (2024, 2027)}
        self.assertTrue(query._qualifier_compatible({}, f, [2025]))
        self.assertFalse(query._qualifier_compatible({}, f, [2019]))


class TestBranches(unittest.TestCase):
    def test_newest_dated_claim_wins(self):
        claims = [
            branches.Claim(24, (2024, 6, 1), "https://x.example/old", "s1"),
            branches.Claim(22, (2026, 4, 23), "https://x.example/new", "s1"),
            branches.Claim(23, (2026, 3, 10), "https://x.example/mid", "s1"),
        ]
        self.assertEqual(branches.resolve_claim(claims).value, 22)


class TestProse(unittest.TestCase):
    def test_ordinal_with_time_beats_bare_ordinal(self):
        sents = [
            "It was the third year in a row that the award was given.",
            "This was the fifth time Forbes recognized Dupaco.",
        ]
        text, _best = prose.rank_sentences(
            sents, "How many times has Forbes recognized Dupaco?", 1)
        self.assertIn("fifth", text)

    def test_bullets_stay_atomic(self):
        para = "\n".join(f"- Item number {i} with some descriptive text here."
                         for i in range(30))
        pieces = prose._paragraph_texts(para)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all("Item number" in p for p in pieces))


class TestEndToEnd(unittest.TestCase):
    def test_current_and_as_of_answers(self):
        ks = make_ks()
        now = ks.answer("What is the APY for the 10-month certificate special?")
        self.assertIn("4.00%", now.text)
        self.assertNotIn("3.90%", now.text)
        self.assertNotIn("3.50%", now.text)  # calculator value must not leak
        then = ks.answer("What is the APY for the 10-month certificate special?",
                         as_of="2026-05-15")
        self.assertIn("3.90%", then.text)

    def test_conflicts_reported(self):
        ks = make_ks()
        topics = " ".join(c.topic for c in ks.conflicts())
        self.assertIn("certificate", topics.lower())

    def test_refusals(self):
        ks = make_ks()
        arm = ks.answer("What is the APR on a 5/1 ARM?")
        self.assertFalse(arm.confident)
        swift = ks.answer("What is Dupaco's SWIFT code?")
        self.assertFalse(swift.confident)


if __name__ == "__main__":
    unittest.main()
