"""Markdown parsing utilities: sections, tables, text cleaning, page classification.

The credit-union pages are long markdown documents with heavy navigation
boilerplate, but the facts we care about live in markdown tables and in
clearly headed sections. These helpers turn a raw page into structured
pieces the fact extractor can work on. Standard library only.
"""
from __future__ import annotations

import html
import re

_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_RE = re.compile(r"<[^>]+>")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")


def clean_text(text: str) -> str:
    """Strip markdown decoration, keeping the human-readable text."""
    text = html.unescape(text or "")
    text = _IMG_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _HTML_RE.sub(" ", text)
    text = text.replace("**", " ").replace("__", " ").replace("`", "")
    text = text.replace("*", " ").replace("\\", " ")
    return re.sub(r"\s+", " ", text).strip()


def split_sections(markdown: str) -> list[tuple[str, str]]:
    """Split a page into (heading, body) sections. Heading '' = preamble."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in (markdown or "").splitlines():
        m = _HEADING_RE.match(line)
        if m:
            sections.append((clean_text(m.group(2)), []))
        else:
            sections[-1][1].append(line)
    return [(h, "\n".join(body)) for h, body in sections]


def _split_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [clean_text(c) for c in cells]


def _is_separator(line: str) -> bool:
    cells = line.strip().strip("|").split("|")
    return bool(cells) and all(_SEP_CELL_RE.match(c.strip()) for c in cells if c.strip() != "")


class Table:
    __slots__ = ("section", "headers", "rows", "preamble")

    def __init__(self, section: str, headers: list[str], rows: list[list[str]], preamble: str = ""):
        self.section = section
        self.headers = headers
        self.rows = rows
        # Short plain-text label printed just above the table ("Home Loans -
        # Purchase", "Rewards Visa"). Dupaco uses these instead of headings.
        self.preamble = preamble

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Table(section={self.section!r}, headers={self.headers!r}, rows={len(self.rows)})"


def parse_tables(markdown: str) -> list[Table]:
    """Extract every markdown table, tagged with the section it appears in."""
    tables: list[Table] = []
    section = ""
    pending = ""
    lines = (markdown or "").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        hm = _HEADING_RE.match(line)
        if hm:
            section = clean_text(hm.group(2))
            pending = ""
            i += 1
            continue
        stripped = line.strip()
        if stripped and not stripped.startswith("|"):
            cleaned = clean_text(stripped)
            # A short, sentence-free plain line is a label for what follows.
            if (len(cleaned) <= 70 and cleaned.count(" ") <= 8 and "." not in cleaned
                    and "](" not in stripped
                    and not stripped.startswith(("[", "!", "*", "-", "#", ">"))):
                pending = cleaned
        if line.strip().startswith("|") and i + 1 < len(lines) and _is_separator(lines[i + 1]):
            headers = _split_row(line)
            rows: list[list[str]] = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                row = _split_row(lines[i])
                if any(c for c in row):
                    # Normalise row width to the header width.
                    row = (row + [""] * len(headers))[: len(headers)] if headers else row
                    rows.append(row)
                i += 1
            if rows:
                tables.append(Table(section, headers, rows, pending))
            pending = ""
            continue
        i += 1
    return tables


# ---------------------------------------------------------------------------
# Page classification: how much do we trust a page as a source for a fact?
# Higher wins when two pages disagree inside the same snapshot.
# ---------------------------------------------------------------------------

def page_authority(url: str) -> tuple[int, str]:
    """Return (authority_rank, page_kind) for a URL.

    The dedicated rate sheets are the credit union's canonical source.
    Product pages come next; calculators are known to go stale; blog and
    newsroom posts describe the past and are corroboration only.
    """
    u = (url or "").lower()
    path = u.split("dupaco.com", 1)[-1]
    if path.rstrip("/") in ("/rates", "/business-rates"):
        return 50, "rate_sheet"
    if "/calculators/" in path or path.startswith("/calculators"):
        return 20, "calculator"
    if "/learn/blog/" in path or "/learn/" in path and "/blog" in path:
        return 10, "blog"
    if "/newsroom/" in path:
        return 10, "newsroom"
    if path.rstrip("/") in ("", "/", "/contact", "/about", "/about/join"):
        return 35, "core"
    if "/personal/" in path or "/business/" in path or path.startswith("/business"):
        return 40, "product"
    return 25, "other"
