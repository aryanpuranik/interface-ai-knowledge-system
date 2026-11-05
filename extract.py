"""Fact extraction: turn pages into qualified, provenanced facts.

A *fact* is the atomic unit of the whole system:

    (subject, metric, qualifiers) -> value

where the qualifiers (term, balance band, model-year band, loan cap,
jumbo/special/intro flags, personal vs business, ...) are part of the
fact's identity. Two values only contradict each other if every
qualifier matches; otherwise they are simply different facts. Every
fact carries its provenance: source URL, snapshot, page authority, and
the exact quote it came from.

Extraction is tables-first because that is where this website keeps its
rates, tiers and fees. Column roles come from the table headers: label
columns (Account Type, Balance Tier, Program, Term...) build the row
label even when they contain dollar amounts, value columns produce
facts, and noise columns (Min. to Open, Payment Example, Points) are
skipped. A small set of pattern extractors covers facts that live in
prose (routing number, credit-card APRs).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from mdparse import clean_text, page_authority, parse_tables

# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

_RANGE_RES = [
    re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*%\s*[-–]\s*(\d[\d,]*(?:\.\d+)?)\s*%"),
    re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*%\s*(?:APR\s*)?to\s*(\d[\d,]*(?:\.\d+)?)\s*%", re.I),
]
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
    for rx in _RANGE_RES:
        m = rx.search(cell)
        if m:
            lo, hi = float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
            return Value(f"{m.group(1)}%-{m.group(2)}%", (lo, hi), "%")
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
_BAL_BAND_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*[-–]\s*(?:\$\s*)?([\d,]+(?:\.\d+)?)")
_BAL_BETWEEN_RE = re.compile(r"between\s*\$\s*([\d,]+(?:\.\d+)?)\s*and\s*\$\s*([\d,]+(?:\.\d+)?)", re.I)
_BAL_MIN_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:\+|and (?:up|over|more))", re.I)
_LOAN_CAP_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*or less", re.I)

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
    m = _BAL_BETWEEN_RE.search(ctx) or _BAL_BAND_RE.search(ctx)
    if m:
        q["balance_band"] = (float(m.group(1).replace(",", "")), float(m.group(2).replace(",", "")))
    else:
        m = _BAL_MIN_RE.search(ctx)
        if m:
            q["balance_band"] = (float(m.group(1).replace(",", "")), None)
    m = _LOAN_CAP_RE.search(ctx)
    if m:
        q["loan_cap"] = float(m.group(1).replace(",", ""))
    low = ctx.lower()
    flags = tuple(w for w in _FLAG_WORDS if re.search(rf"\b{w}\b", low))
    if flags:
        q["flags"] = flags
    # Segment: what the text itself says beats the URL; the URL is the
    # fallback (a page-level guess mislabels mixed pages like /business-rates).
    if re.search(r"\bbusiness\b", low):
        q["segment"] = "business"
    elif re.search(r"\bpersonal\b", low):
        q["segment"] = "personal"
    elif "/business" in (url or ""):
        q["segment"] = "business"
    elif "/personal" in (url or ""):
        q["segment"] = "personal"
    return q


def metric_from_header(header: str, section: str) -> str:
    """What kind of number is this column? APY, APR, dividend rate, fee..."""
    h = (header or "").lower()
    if "apy" in h or "annual percentage yield" in h:
        return "APY"
    if "apr" in h or "annual percentage rate" in h:
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
        qualifiers = dict(self.qualifiers)
        # Canonical segment: an unmarked fact is a personal-banking fact.
        # Without this, the segment-less rate sheet and the personal
        # product page mint duplicate keys for the very same fact.
        qualifiers.setdefault("segment", "personal")
        qual = tuple(sorted(
            (k, v if not isinstance(v, tuple) else tuple(v)) for k, v in qualifiers.items()
        ))
        return (subj, self.metric.lower(), qual)


_LABEL_HEADER_RE = re.compile(
    r"tier|type|program|term|account|balance|loan amount|description|service|product|model|vehicle|loan\b", re.I)
_SKIP_HEADER_RE = re.compile(r"min\.|payment|points|effective|disclosure", re.I)


def _subject_for(section: str, page_title: str, row_label: str) -> str:
    label = (row_label or "").strip()
    if label.startswith("$") and section:
        # A row whose label starts with an amount band ("$10,000-24,999
        # 36 Months") is a tier row of the section's product, not a
        # product itself; name it after the section.
        return f"{section} {label}"
    if label and len(label) > 2 and not re.fullmatch(r"[\d\s$%.,/–-]+", label):
        return label
    if section:
        return section
    return page_title


def extract_table_facts(page) -> list[Fact]:
    authority, kind = page_authority(page.url)
    facts: list[Fact] = []
    for table in parse_tables(page.markdown):
        headers = table.headers
        for row in table.rows:
            label_parts: list[str] = []
            value_cells: list[tuple[int, Value]] = []
            for j, cell in enumerate(row):
                header = headers[j] if j < len(headers) else ""
                if header and _SKIP_HEADER_RE.search(header):
                    continue
                value = find_value(cell)
                if value is not None and not (header and _LABEL_HEADER_RE.search(header)):
                    value_cells.append((j, value))
                elif cell:
                    label_parts.append(cell)
            if not value_cells:
                continue
            label = " ".join(label_parts).strip()
            context = " ".join(x for x in [table.section, page.title, label] if x)
            for j, value in value_cells:
                header = headers[j] if j < len(headers) else ""
                metric = metric_from_header(header, table.section)
                if value.unit == "$" and metric != "fee":
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
_CARD_NAME_RE = re.compile(r"(Business\s+)?(Platinum|Rewards)\s+Visa", re.I)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


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
    facts.extend(_extract_card_facts(page, text, authority, kind))
    return facts


def _extract_card_facts(page, text: str, authority: int, kind: str) -> list[Fact]:
    """Credit-card APRs live in prose: '...standard APR ... variable rate
    from 12.50% APR to 19.50% APR...' in a per-card section/footnote."""
    out: list[Fact] = []
    seen: set = set()
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if "apr" not in sentence.lower() or "%" not in sentence:
            continue
        name_m = _CARD_NAME_RE.search(sentence)
        # The card a sentence belongs to may be named just before it
        # (section lead-in); search a short window of preceding text.
        if name_m is None:
            idx = text.find(sentence[:40])
            window = text[max(0, idx - 400):idx] if idx > 0 else ""
            names = list(_CARD_NAME_RE.finditer(window))
            if not names:
                continue
            name_m = names[-1]
        value = find_value(sentence)
        if value is None or value.unit != "%":
            continue
        business = bool(name_m.group(1)) or "/business" in page.url
        subject = f"{'Business ' if business else ''}{name_m.group(2).title()} Visa"
        low = sentence.lower()
        flags = []
        if "introductory" in low or "intro " in low:
            flags.append("intro")
        if "standard" in low:
            flags.append("standard")
        if "variable" in low:
            flags.append("variable")
        if "balance transfer" in low:
            flags.append("balance transfer")
        key = (subject, tuple(value.nums), tuple(flags))
        if key in seen:
            continue
        seen.add(key)
        qual = parse_qualifiers(subject + " " + sentence, page.url)
        if flags:
            qual["flags"] = tuple(sorted(set(qual.get("flags", ())) | set(flags)))
        out.append(Fact(
            subject=subject, context=page.title + " " + subject, metric="APR",
            value=value, qualifiers=qual,
            url=page.url, page_title=page.title, page_kind=kind,
            authority=authority, quote=sentence[:300],
        ))
    return out


def extract_facts(page) -> list[Fact]:
    return extract_table_facts(page) + extract_pattern_facts(page)
