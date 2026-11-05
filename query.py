"""Question answering over the resolved fact store (Phase 1: fact lookup).

The question is normalised and its qualifiers extracted with the same
code that keys the facts, so a question and a fact match when they are
*about the same thing in the same terms*. Answers are rendered from the
winning fact only: the answer text contains the resolved value and no
other numbers, so a stale value can never leak into an answer.
"""
from __future__ import annotations

import re

from extract import parse_qualifiers

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = set(
    "what is the a an on of for do does you your i me my can tell please "
    "with and or in at to how much many are was were will would should".split()
)

# Spelled-out numbers and common synonyms, normalised before matching.
_NUM_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11",
    "twelve": "12", "fifteen": "15", "seventeen": "17", "eighteen": "18",
    "twenty": "20", "thirty": "30", "sixty": "60",
}
_SYNONYMS = {"cd": "certificate", "cds": "certificate"}


def normalise_question(question: str) -> str:
    q = (question or "").lower().replace("-", " ").replace("/", " ")
    tokens = []
    for tok in _WORD_RE.findall(q):
        tok = _NUM_WORDS.get(tok, tok)
        tok = _SYNONYMS.get(tok, tok)
        tokens.append(tok)
    return " ".join(tokens)


def _tokens(text: str) -> set:
    return {t for t in _WORD_RE.findall((text or "").lower().replace("-", " ")) if t not in _STOP}


def _qualifier_compatible(q_qual: dict, f_qual: dict) -> bool:
    """Hard filter: a question that names a term/band must not be answered
    with a fact for a different term/band."""
    for k in ("term_months", "model_years", "segment"):
        if k in q_qual and k in f_qual and q_qual[k] != f_qual[k]:
            return False
    if "balance_band" in q_qual and "balance_band" in f_qual:
        qb, fb = q_qual["balance_band"], f_qual["balance_band"]
        # A question asking about an amount fits a band that contains it;
        # a question naming a band must match the band.
        if qb[1] is not None and fb != qb:
            return False
        if qb[1] is None and fb[0] is not None and fb[0] > qb[0]:
            return False
    return True


def _metric_preference(question_norm: str) -> str | None:
    if re.search(r"\bapy\b", question_norm):
        return "APY"
    if re.search(r"\bapr\b", question_norm):
        return "APR"
    if "dividend" in question_norm:
        return "dividend rate"
    return None


def find_facts(state, question: str, limit: int = 5):
    """Rank the resolved facts of a snapshot state against a question."""
    if state is None:
        return []
    q_norm = normalise_question(question)
    q_tokens = _tokens(q_norm)
    q_qual = parse_qualifiers(question, "")
    metric = _metric_preference(q_norm)
    winners = state.winners()
    scored = []
    for key, fact in winners.items():
        if not _qualifier_compatible(q_qual, fact.qualifiers):
            continue
        f_tokens = _tokens(fact.subject + " " + fact.context)
        overlap = len(q_tokens & f_tokens)
        if overlap == 0:
            continue
        score = overlap
        # A question about a rate must be answered by a percent fact, not a
        # dollar amount that happens to share vocabulary.
        if ("rate" in q_tokens or metric) and fact.value.unit != "%":
            score -= 4
        if metric and fact.metric == metric:
            score += 3
        if metric and fact.metric != metric and fact.metric in ("APY", "APR"):
            score -= 3
        if "term_months" in q_qual and fact.qualifiers.get("term_months") == q_qual["term_months"]:
            score += 3
        scored.append((score, fact))
    scored.sort(key=lambda s: (-s[0], s[1].subject))
    return scored[:limit]
