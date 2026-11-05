"""Fact extraction: turn pages into qualified, provenanced facts.

A *fact* is the atomic unit of the whole system:

    (subject, metric, qualifiers) -> value

where the qualifiers (term, balance band, model-year band, jumbo/special
flags, personal vs business, ...) are part of the fact's identity. Two
values only contradict each other if every qualifier matches; otherwise
they are simply different facts. Every fact carries its provenance:
source URL, snapshot, page authority, and the exact quote it came from.

Extraction is tables-first because that is where this website keeps its
rates, tiers and fees; a small set of pattern extractors covers facts
that live in prose (routing number, phone numbers).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from mdparse import clean_text, page_authority, parse_tables

# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

_RANGE_RE = re.compile(r"(\$)?\s*(\d[\d,]*(?:\.\d+)?)\s*%\s*[-–]\s*(\$)?\s*(\d[\d,]*(?:\.\d+)?)\s*%")
_PCT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*%")
_MONEY_RANGE_RE = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*[-–]\s*\$\s*(\d[\d,]*(?:\.\d+)?)")
_MONEY_RE = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)")


@dataclass
class Value:
    text: str                 # as it should appear in an answer, e.g. "5.39%" or "12.50%-19.50%"
    nums: tuple               # numeric components, e.g. (5.39,) or (12.5, 19.5)
    unit: str                 # "%" or "$"


def find_value(cell: str) -> Value | None:
    """Find the first monetary/percent value in a table cell, ranges included."""
    m = _RANGE_RE.search(cell)
    if m:
        lo, hi = float(m.group(2).replace(",", "")), float(m.group(4).replace(",", ""))
        return Value(f"{m.group(2)}%-{m.group(4)}%", (lo, hi), "%")
    m = _MONEY_RANGE_RE.search(cell)
    if m:
        lo, hi = float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
        return Value(f"${m.group(1)}-${m.group(2)}", (lo, hi), "$")
    m = _PCT_RE.search(cell)
    if m:
        return Value(f"{m.group(1)}%", (float(m.group(1).replace(",", "")),), "%")
    m = _MONEY_RE.search(cell)
    if m:
        return Value(f"${m.group(1)}", (float(m.group(1).replace(",", "")),), "$")
    return None


# ---------------------------------------------------------------------------
# Qualifiers
# ---------------------------------------------------------------------------

_TERM_MONTHS_RE = re.compile(r"(\d{1,3})\s*[-–]?\s*months?\b", re.I)
_TERM_YEARS_RE = re.compile(r"(\d{1,2})\s*[-–]?\s*years?\b(?!\s*model)", re.I)
_MODEL_YEAR_RE = re.compile(r"(20\d{2})\s*[-–]\s*(\d{2}|20\d{2})\b")
_BAL_BAND_RE = re.compile(r"\$\s*([\d,]+)(?:\.\d+)?\s*[-–]\s*(?:\$\s*)?([\d,]+)")
_BAL_MIN_RE = re.compile(r"\$\s*([\d,]+)(?:\.\d+)?\s*(?:\+|and (?:up|over|more))", re.I)

_FLAG_WORDS = (
    "jumbo", "special", "standard", "premium", "rewards", "platinum",
    "fixed", "variable", "ira", "intro", "introductory", "promotional",
)


def parse_qualifiers(context: str, url: str) -> dict:
    """Extract the qualifier signature from a label/section/title context."""
    q: dict = {}
    ctx = context or ""
    m = _TERM_MONTHS_RE.search(ctx)
    if m:
        q["term_months"] = int(m.group(1))
    else:
        m = _TERM_YEARS_RE.search(ctx)
        if m and "model year" not in ctx.lower():
            q["term_months"] = int(m.group(1)) * 12
    m = _MODEL_YEAR_RE.search(ctx)
    if m:
        lo = int(m.group(1))
        hi_raw = m.group(2)
        hi = int(hi_raw) if len(hi_raw) == 4 else int(str(lo)[:2] + hi_raw)
        q["model_years"] = (lo, hi)
    m = _BAL_BAND_RE.search(ctx)
    if m:
        q["balance_band"] = (float(m.group(1).replace(",", "")), float(m.group(2).replace(",", "")))
    else:
        m = _BAL_MIN_RE.search(ctx)
        if m:
            q["balance_band"] = (float(m.group(1).replace(",", "")), None)
    low = ctx.lower()
    flags = tuple(w for w in _FLAG_WORDS if re.search(rf"\b{w}\b", low))
    if flags:
        q["flags"] = flags
    if "/business" in (url or "") or re.search(r"\bbusiness\b", low):
        q["segment"] = "business"
    elif "/personal" in (url or ""):
        q["segment"] = "personal"
    return q


def metric_from_header(header: str, section: str) -> str:
    """What kind of number is this column? APY, APR, dividend rate, fee..."""
    h = (header or "").lower()
    if "apy" in h:
        return "APY"
    if "apr" in h:
        return "APR"
    if "dividend" in h:
        return "dividend rate"
    if "interest rate" in h:
        return "interest rate"
    if "fee" in h:
        return "fee"
    if "rate" in h:
        return "rate"
    s = (section or "").lower()
    if "apy" in s:
        return "APY"
    if "apr" in s:
        return "APR"
    return "rate"


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

@dataclass
class Fact:
    subject: str              # product-ish label, e.g. "17-Month Fixed Certificate Special"
    context: str              # section + page title context the fact was found in
    metric: str               # APY / APR / dividend rate / fee / ...
    value: Value
    qualifiers: dict = field(default_factory=dict)
    url: str = ""
    page_title: str = ""
    page_kind: str = "other"
    authority: int = 0
    snapshot_id: str = ""
    taken_on: str = ""
    quote: str = ""

    def key(self) -> tuple:
        """Canonical identity: same key <=> the two facts claim the same thing."""
        subj = re.sub(r"[^a-z0-9 ]", " ", self.subject.lower())
        subj = re.sub(r"\s+", " ", subj).strip()
        qual = tuple(sorted(
            (k, v if not isinstance(v, tuple) else tuple(v)) for k, v in self.qualifiers.items()
        ))
        return (subj, self.metric.lower(), qual)


def _subject_for(section: str, page_title: str, row_label: str) -> str:
    """Best human label for a table row: prefer the row's own product cell."""
    label = (row_label or "").strip()
    if label and len(label) > 2 and not re.fullmatch(r"[\d\s$%.,/–-]+", label):
        return label
    if section:
        return section
    return page_title


def extract_table_facts(page) -> list[Fact]:
    authority, kind = page_authority(page.url)
    facts: list[Fact] = []
    for table in parse_tables(page.markdown):
        for row in table.rows:
            # Leading non-value cells form the row label ("2024-27 Model Year",
            # "60 Months"); value cells follow.
            first_value = next((i for i, c in enumerate(row) if find_value(c)), None)
            if first_value is None:
                continue
            label = " ".join(c for c in row[:first_value] if c).strip()
            context = " ".join(x for x in [table.section, page.title, label] if x)
            for j in range(first_value, len(row)):
                value = find_value(row[j])
                if value is None:
                    continue
                header = table.headers[j] if j < len(table.headers) else ""
                # A fee/amount column in a rates table etc.: metric comes from
                # the header when it says anything, else the section.
                metric = metric_from_header(header, table.section)
                if value.unit == "$" and metric not in ("fee",):
                    # Dollar cells in a rate table are minimums/balance bands,
                    # not rates; never let them pose as a rate fact.
                    metric = "amount"
                facts.append(Fact(
                    subject=_subject_for(table.section, page.title, label),
                    context=context,
                    metric=metric,
                    value=value,
                    qualifiers=parse_qualifiers(context + " " + header, page.url),
                    url=page.url,
                    page_title=page.title,
                    page_kind=kind,
                    authority=authority,
                    quote=" | ".join(c for c in row if c),
                ))
    return facts


# ---------------------------------------------------------------------------
# Pattern facts (prose)
# ---------------------------------------------------------------------------

_ROUTING_RE = re.compile(r"routing number[^0-9]{0,40}(\d{9})", re.I)
_PHONE_RE = re.compile(r"\b(800|866|877|888|844|855)-\d{3}-\d{4}\b")


def extract_pattern_facts(page) -> list[Fact]:
    authority, kind = page_authority(page.url)
    text = clean_text(page.markdown)
    facts: list[Fact] = []
    m = _ROUTING_RE.search(text)
    if m:
        facts.append(Fact(
            subject="routing number", context=page.title, metric="number",
            value=Value(m.group(1), (float(m.group(1)),), ""),
            url=page.url, page_title=page.title, page_kind=kind,
            authority=authority, quote=m.group(0),
        ))
    return facts


def extract_facts(page) -> list[Fact]:
    return extract_table_facts(page) + extract_pattern_facts(page)
