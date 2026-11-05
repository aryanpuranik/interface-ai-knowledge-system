"""Branch registry, hours schedules, and dated prose claims.

Three kinds of information never appear as rate-table facts:

* the branch locator on the contact page (names, addresses, cities),
* lobby / drive-up hour blocks printed in branch-opening articles, and
* numeric claims in boilerplate prose ("22 branch office locations"),
  which conflict across article generations and are resolved by taking
  the claim from the most recently dated page: old press releases keep
  the boilerplate of their era, so the newest dated claim is current.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_ADDR_RE = re.compile(r"(\d{1,6}\s+[^,\n]+?),\s*([^,\n]+?),\s*([A-Z]{2}),?\s*(\d{5})")
_DATE_RE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),\s+(20\d{2})")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
_DAY_LINE = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)", re.I)


@dataclass
class Branch:
    name: str
    address: str
    city: str
    state: str
    zip: str
    url: str
    snapshot_id: str


def page_date(text: str) -> tuple[int, int, int] | None:
    """Best-effort publication date: the first date printed on the page."""
    for m in _DATE_RE.finditer(text[:4000]):
        return (int(m.group(3)), _MONTHS[m.group(1)[:3]], int(m.group(2)))
    return None


def parse_locator(pages: list[Page], snapshot_id: str) -> list[Branch]:
    branches: list[Branch] = []
    for page in pages:
        if not page.url.rstrip("/").endswith("/contact"):
            continue
        for block in re.split(r"\n### ", page.markdown):
            lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
            if len(lines) < 3:
                continue
            kind = lines[1]
            addr = _ADDR_RE.search(block)
            if addr and kind == "Dupaco Branch":
                branches.append(Branch(
                    name=lines[0], address=addr.group(1).strip(),
                    city=addr.group(2).strip(), state=addr.group(3),
                    zip=addr.group(4), url=page.url, snapshot_id=snapshot_id))
    return branches


def _schedule_block(text: str, label: str) -> list[str] | None:
    m = re.search(rf"\*\*{label}\*\*\s*(.*?)(?=\*\*[A-Z]|\Z)", text, re.S)
    if not m:
        return None
    lines = []
    for raw in m.group(1).splitlines():
        line = raw.strip().strip("*").strip()
        if _DAY_LINE.search(line) and re.search(r"\d|Closed", line):
            lines.append(re.sub(r"\s+", " ", line))
        if len(lines) >= 4:
            break
    return lines or None


@dataclass
class HoursBook:
    lobby: dict[str, list[str]] = field(default_factory=dict)      # schedule text -> urls
    drive_up: dict[str, list[str]] = field(default_factory=dict)

    def add(self, kind: str, lines: list[str], url: str) -> None:
        book = self.lobby if kind == "lobby" else self.drive_up
        book.setdefault("\n".join(lines), []).append(url)


def parse_hours(pages: list[Page]) -> HoursBook:
    book = HoursBook()
    for page in pages:
        for label, kind in (("Lobby", "lobby"), ("Drive-up", "drive_up")):
            lines = _schedule_block(page.markdown, label)
            if lines:
                book.add(kind, lines, page.url)
    return book


@dataclass
class Closure:
    date_text: str
    alternates: list[str]
    url: str
    snapshot_id: str


def parse_closures(pages: list[Page], snapshot_id: str) -> list[Closure]:
    out: list[Closure] = []
    for page in pages:
        low = page.url.lower()
        if "hy-vee" not in low and "hy-vee" not in page.title.lower():
            continue
        m = re.search(r"will close ([A-Z][a-z]+ \d{1,2}, \d{4})", page.markdown)
        if not m:
            continue
        alts: list[str] = []
        am = re.search(r"full-service branches at ([A-Z][\w. ]+?) and ([A-Z][\w. ]+?)(?:,|\"|\.)", page.markdown)
        if am:
            alts = [am.group(1).strip(), am.group(2).strip()]
        out.append(Closure(date_text=m.group(1), alternates=alts,
                           url=page.url, snapshot_id=snapshot_id))
    return out


@dataclass
class Claim:
    value: int
    date: tuple[int, int, int] | None
    url: str
    snapshot_id: str


def parse_branch_count_claims(pages: list[Page], snapshot_id: str) -> list[Claim]:
    claims: list[Claim] = []
    for page in pages:
        m = re.search(r"(\d{1,3}) branch office locations", page.markdown)
        if m:
            claims.append(Claim(value=int(m.group(1)), date=page_date(page.markdown),
                                url=page.url, snapshot_id=snapshot_id))
    return claims


def resolve_claim(claims: list[Claim]) -> Claim | None:
    """The claim from the most recently dated page wins; undated pages
    lose to dated ones; frequency breaks remaining ties."""
    if not claims:
        return None
    dated = [c for c in claims if c.date]
    pool = dated or claims
    newest = max((c.date or (0, 0, 0)) for c in pool)
    top = [c for c in pool if (c.date or (0, 0, 0)) == newest]
    if len(top) == 1:
        return top[0]
    from collections import Counter
    value = Counter(c.value for c in top).most_common(1)[0][0]
    return next(c for c in top if c.value == value)


def contact_phone(pages: list[Page]) -> tuple[str, str] | None:
    """The credit union's main phone number, from the contact page."""
    for page in pages:
        if not page.url.rstrip("/").endswith("/contact"):
            continue
        m = re.search(r"Phone:\**\s*\[?(800-\d{3}-\d{4})", page.markdown)
        if m:
            return m.group(1), page.url
    return None
