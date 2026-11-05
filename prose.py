"""Prose retrieval fallback (BM25-style, stdlib only).

Tables carry the rates, but a large share of what members ask lives in
prose: program descriptions, closure announcements, award stories,
how-to guidance. This index chunks every page into paragraphs, scores
them against the question with TF-IDF weights and a page-authority
prior, and renders the one or two most relevant *sentences* — never a
whole paragraph dump, so stray numbers stay out of the answer.

The fallback is deliberately conservative: it only runs when the fact
store has no confident answer, it refuses rate-shaped questions (the
fact store owns those), and it declines when retrieval is weak.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from mdparse import clean_text, page_authority
from query import _STOP, _tokens

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“(])")
_SKIP_MARKERS = ("Handpicked", "View all", "Loading locations",
                 "data:image", "Please Select")


@dataclass
class Chunk:
    text: str
    sentences: list[str]
    counts: Counter
    url: str
    snapshot_id: str
    authority: int
    page_kind: str
    page_date: tuple | None = None


_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
             "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}


def _prefix_hit(tok: str, tokens: set) -> bool:
    """'recognition' should count 'recognized' as a near-hit."""
    if len(tok) < 6:
        return False
    return any(t.startswith(tok[:6]) or tok.startswith(t[:6]) for t in tokens if len(t) >= 6)


def rank_sentences(sentences: list[str], question: str, count: int = 2) -> tuple[str, float]:
    """Best `count` sentences (original order) for the question across a
    page's chunks. Exact stemmed hits score 1, prefix near-hits 0.7, and
    ordinal/'time' sentences get a bonus for recognition questions."""
    q_tokens = {t for t in _tokens(question) if t not in _STOP}
    wants_ordinal = bool({"time", "recognition", "received", "many", "honor",
                          "award", "rank"} & set(_tokens(question)))
    scored = []
    for i, sent in enumerate(sentences):
        s_tokens = _tokens(sent)
        hits = len(q_tokens & s_tokens)
        hits += 0.7 * sum(1 for t in q_tokens - s_tokens if _prefix_hit(t, s_tokens))
        if wants_ordinal:
            ord_val = max((_ORDINALS[t] for t in set(_ORDINALS) & s_tokens), default=0)
            if "time" in s_tokens and ord_val:
                # "the fifth time Forbes recognized..." states the count
                # itself; between generations the highest count is the
                # current claim ("fifth time" supersedes "second").
                hits += 2.0 + 0.45 * ord_val
            elif "time" in s_tokens:
                hits += 2.0
            elif ord_val:
                # A bare ordinal is a ranking or a duration ("third in
                # the state", "third year in a row"), not the count.
                hits += 0.8 + 0.2 * ord_val
        scored.append((hits, i, sent))
    if not scored:
        return "", 0.0
    best = max(s[0] for s in scored)
    top_entry = sorted(scored, key=lambda s: (-s[0], s[1]))[0]
    chosen = sorted(sorted(scored, key=lambda s: (-s[0], s[1]))[:count], key=lambda s: s[1])
    # Anaphora carry: "This initiative provides up to $15,000..." only
    # makes sense (and only carries the answer) right after the sentence
    # that names the program. Append any chosen sentence's $-bearing
    # successor.
    for entry in list(chosen):
        nxt = next((s for s in scored if s[1] == entry[1] + 1), None)
        if nxt is not None and "$" in nxt[2] and all(nxt[1] != c[1] for c in chosen):
            chosen = sorted(chosen + [nxt], key=lambda s: s[1])
    # Prefer a rendering without stray percentages when a comparable
    # percentage-free sentence exists (percentages belong to rate facts).
    if any("%" in s[2] for s in chosen):
        clean = [s for s in chosen if "%" not in s[2]]
        rest = sorted((s for s in scored if "%" not in s[2] and s not in chosen),
                      key=lambda s: (-s[0], s[1]))
        while len(clean) < count and rest:
            clean.append(rest.pop(0))
        if clean and max(s[0] for s in clean) >= best - 1.5:
            chosen = sorted(clean, key=lambda s: s[1])
    return " ".join(s[2] for s in chosen), best


def _paragraph_texts(para: str) -> list[str]:
    """Cleaned chunk texts for one raw paragraph block. Long blocks
    (feature lists, bullet lists) are split on line boundaries, then by
    sentences, so a single answer-bearing bullet is not lost inside a
    2,000-character list."""
    whole = clean_text(para)
    if len(whole) <= 900 and not any(
            ln.lstrip().startswith(("-", "•")) for ln in para.splitlines()):
        return [whole]
    pieces: list[str] = []
    buf = ""
    for line in para.splitlines():
        cleaned = clean_text(line)
        if not cleaned:
            continue
        if line.lstrip().startswith(("-", "•")):
            # A bullet is a self-contained claim; never merge it into a
            # neighbouring bullet's chunk (multi-year stat lists!).
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.append(cleaned)
            continue
        if buf and len(buf) + len(cleaned) + 1 > 850:
            pieces.append(buf)
            buf = ""
        buf = f"{buf} {cleaned}".strip() if buf else cleaned
    if buf:
        pieces.append(buf)
    out: list[str] = []
    for piece in pieces:
        if len(piece) <= 900:
            out.append(piece)
            continue
        buf = ""
        for sent in _SENT_SPLIT.split(piece):
            if buf and len(buf) + len(sent) + 1 > 850:
                out.append(buf)
                buf = ""
            buf = f"{buf} {sent}".strip() if buf else sent
        if buf:
            out.append(buf)
    return out


@dataclass
class ProseIndex:
    chunks: list[Chunk] = field(default_factory=list)
    idf: dict[str, float] = field(default_factory=dict)
    by_url: dict[str, list[Chunk]] = field(default_factory=dict)
    postings: dict[str, list[int]] = field(default_factory=dict)

    @classmethod
    def build(cls, pages: list[Page], snapshot_id: str) -> "ProseIndex":
        from branches import page_date
        idx = cls()
        df: Counter = Counter()
        for page in pages:
            body = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", page.markdown)
            p_date = page_date(page.markdown)
            authority, kind = page_authority(page.url)
            for para in re.split(r"\n\s*\n", body):
                for text in _paragraph_texts(para):
                    if not (60 <= len(text) <= 900):
                        continue
                    if any(m in text for m in _SKIP_MARKERS):
                        continue
                    if text.count("[") > 2 or text.count("|") > 2:
                        continue
                    sentences = [s.strip().lstrip("-• ").strip()
                                 for s in _SENT_SPLIT.split(text)
                                 if 25 <= len(s.strip()) <= 420
                                 and not s.strip().startswith("#")]
                    if not sentences:
                        continue
                    counts = Counter(_tokens(text))
                    chunk = Chunk(text=text, sentences=sentences, counts=counts,
                                  url=page.url, snapshot_id=snapshot_id,
                                  authority=authority, page_kind=kind,
                                  page_date=p_date)
                    idx.chunks.append(chunk)
                    idx.by_url.setdefault(page.url, []).append(chunk)
                    df.update(counts.keys())
        n = max(len(idx.chunks), 1)
        idx.idf = {t: math.log(1 + n / (1 + c)) for t, c in df.items()}
        # Inverted index so search touches only chunks that mention a
        # query token (a full scan per question was the latency tail).
        idx.postings = {}
        for i, chunk in enumerate(idx.chunks):
            for tok in chunk.counts:
                idx.postings.setdefault(tok, []).append(i)
        return idx

    def search(self, question: str, top_n: int = 5) -> list[tuple[float, Chunk]]:
        q_tokens = {t for t in _tokens(question) if t not in _STOP}
        if not q_tokens:
            return []
        candidate_ids: set = set()
        for t in q_tokens:
            candidate_ids.update(self.postings.get(t, ()))
        scored: list[tuple[float, Chunk]] = []
        for ci in candidate_ids:
            chunk = self.chunks[ci]
            score = 0.0
            for t in q_tokens:
                tf = chunk.counts.get(t, 0)
                if tf:
                    score += self.idf.get(t, 0.5) * (tf / (tf + 1.2))
                elif _prefix_hit(t, set(chunk.counts)):
                    score += 0.7 * self.idf.get(t, 0.5) * 0.45
            if score:
                score *= 0.75 + chunk.authority / 200.0
                scored.append((score, chunk))
        scored.sort(key=lambda sc: -sc[0])
        return scored[:top_n]


def best_sentences(chunk: Chunk, question: str, count: int = 2) -> tuple[str, int]:
    """The `count` sentences of the chunk most relevant to the question,
    in original order; returns (text, distinctive token hits)."""
    q_tokens = {t for t in _tokens(question) if t not in _STOP}
    ranked = []
    for i, sent in enumerate(chunk.sentences):
        s_tokens = _tokens(sent)
        hits = len(q_tokens & s_tokens)
        rare = sum(1 for t in q_tokens & s_tokens if len(t) > 3)
        ranked.append((hits + rare, i, sent))
    ranked.sort(key=lambda r: (-r[0], r[1]))
    chosen = sorted(ranked[:count], key=lambda r: r[1])
    total_hits = sum(r[0] for r in ranked[:count])
    return " ".join(r[2] for r in chosen), total_hits
