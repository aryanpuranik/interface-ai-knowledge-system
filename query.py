"""Question answering over the resolved fact store.

The question is normalised and its qualifiers extracted with the same
code that keys the facts, so a question and a fact match when they are
*about the same thing in the same terms*. Matching is deliberately
strict: a fact only wins if the question shares distinctive (non-generic)
vocabulary with it and every qualifier the question names is compatible
(term, balance band, loan cap, segment, and model-year containment —
a 2025 vehicle belongs to the 2024-27 band). Deposit products default
to APY and loan products to APR when the question just says "rate",
which is how the website itself presents them.
"""
from __future__ import annotations

import re

from extract import parse_qualifiers

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = set(
    "what is the a an on of for do does you your i me my can tell please "
    "with and or in at to how much many are was were will would should".split()
)
_GENERIC = {
    "rate", "rates", "interest", "apr", "apy", "annual", "percentage",
    "percent", "loan", "loans", "account", "accounts", "dupaco", "current",
}

_NUM_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11",
    "twelve": "12", "fifteen": "15", "seventeen": "17", "eighteen": "18",
    "twenty": "20", "thirty": "30", "sixty": "60",
}
_SYNONYMS = {"cd": "certificate", "cds": "certificate"}
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_SINGLE_AMOUNT_RE = re.compile(r"\$\s*([\d,]+)(?:\.\d+)?")


def normalise_question(question: str) -> str:
    q = (question or "").lower().replace("-", " ").replace("/", " ")
    tokens = []
    for tok in _WORD_RE.findall(q):
        tok = _NUM_WORDS.get(tok, tok)
        tok = _SYNONYMS.get(tok, tok)
        tokens.append(tok)
    return " ".join(tokens)


def _stem(tok: str) -> str:
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def _tokens(text: str) -> set:
    return {_stem(t) for t in _WORD_RE.findall((text or "").lower().replace("-", " ")) if t not in _STOP}


def question_years(question: str) -> list[int]:
    return [int(m.group(0)) for m in _YEAR_RE.finditer(question or "")]


_POINT_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")


def question_balance_point(question: str) -> float | None:
    """A lone dollar amount in a question ("I keep $15,000 in Share
    Savings") is a point balance: tier facts must contain it."""
    amounts = [float(a.replace(",", "")) for a in _POINT_RE.findall(question or "")]
    return max(amounts) if amounts else None


def _qualifier_compatible(q_qual: dict, f_qual: dict, q_years: list[int],
                          q_point: float | None = None) -> bool:
    """Hard filter: a question that names a term/band/cap must not be
    answered with a fact for a different term/band/cap. Model years are
    containment: a question year must fall inside the fact's band."""
    for k in ("term_months", "loan_cap", "segment"):
        if k in q_qual and k in f_qual and q_qual[k] != f_qual[k]:
            return False
    if "model_years" in f_qual and q_years:
        lo, hi = f_qual["model_years"]
        if not any(lo <= y <= hi for y in q_years):
            return False
    if "model_years" in q_qual and "model_years" in f_qual and q_qual["model_years"] != f_qual["model_years"]:
        return False
    if "balance_band" in q_qual and "balance_band" in f_qual:
        qb, fb = q_qual["balance_band"], f_qual["balance_band"]
        if qb[1] is not None:
            # Bands match within a dollar ("$24,999" vs the table's
            # "$24,999.99" are the same tier).
            if abs(fb[0] - qb[0]) >= 1 or fb[1] is None or abs(fb[1] - qb[1]) >= 1:
                return False
        if qb[1] is None and fb[0] is not None and fb[0] > qb[0]:
            return False
    if q_point is not None and "balance_band" in f_qual and "balance_band" not in q_qual:
        lo, hi = f_qual["balance_band"]
        if lo is not None and q_point < lo:
            return False
        if hi is not None and q_point > hi + 0.99:
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


_DEPOSIT_WORDS = ("certificate", "savings", "checking", "money market", "ira")
_LOAN_WORDS = ("loan", "mortgage", "visa", "card", "heloc", "auto", "personal loan")


def find_facts(state, question: str, limit: int = 8):
    """Rank the resolved facts of a snapshot state against a question.

    Returns [(score, fact, distinctive_overlap, subject_overlap)] best first.
    """
    if state is None:
        return []
    q_norm = normalise_question(question)
    q_tokens = _tokens(q_norm)
    q_qual = parse_qualifiers(question, "")
    q_years = question_years(question)
    q_point = question_balance_point(question) if "balance_band" not in q_qual else None
    metric = _metric_preference(q_norm)
    wants_business = "business" in q_norm
    deposit_q = any(w in q_norm for w in _DEPOSIT_WORDS)
    loan_q = any(w in q_norm for w in _LOAN_WORDS)
    winners = state.winners()
    scored = []
    for key, fact in winners.items():
        if not _qualifier_compatible(q_qual, fact.qualifiers, q_years, q_point):
            continue
        f_tokens = _tokens(fact.subject + " " + fact.context)
        overlap = q_tokens & f_tokens
        distinctive = {t for t in overlap if t not in _GENERIC and (len(t) > 2 or t.isdigit())}
        if not distinctive:
            continue
        s_distinctive = {t for t in (q_tokens & _tokens(fact.subject))
                         if t not in _GENERIC and (len(t) > 2 or t.isdigit())}
        n_alpha = len({t for t in distinctive if not t.isdigit()})
        n_digit = len(distinctive) - n_alpha
        score = 3 * n_alpha + n_digit + len(overlap - distinctive)
        # Subject precision: a fact whose subject carries distinctive
        # words the question never mentioned ("Jumbo") is a sibling
        # variant, not the asked-about product; prefer the exact one.
        subj_tokens = _tokens(fact.subject)
        extra_subj = {t for t in subj_tokens - q_tokens
                      if t not in _GENERIC and (len(t) > 2 or t.isdigit())}
        score -= 0.5 * len(extra_subj)
        # Qualifier-family mismatch: a fact tiered by term is a loan fact;
        # a question tiered by balance is asking about a deposit (and the
        # reverse). Extra unasked-for restrictive qualifiers are evidence
        # the fact belongs to a different product family.
        if "term_months" in fact.qualifiers and "term_months" not in q_qual and "balance_band" in q_qual:
            score -= 6
        if "balance_band" in fact.qualifiers and "balance_band" not in q_qual and "term_months" in q_qual:
            score -= 6
        # The authoritative page's version of a fact should outrank a
        # stale page's version when both match the question.
        score += fact.authority / 10.0
        if "model_years" in fact.qualifiers and q_years:
            # Overlapping model-year bands (2024-27 vs a stale page's
            # 2023-26): the band reaching furthest into the future is
            # the current generation.
            score += fact.qualifiers["model_years"][1] - 2024
        # Metric discipline.
        if ("rate" in q_tokens or metric) and fact.value.unit != "%":
            score -= 6
        if fact.value.unit == "$" and not any(
                w in q_norm for w in ("fee", "cost", "charge", "price", "amount",
                                      "minimum", "balance", "much", "limit", "$")):
            score -= 8  # a dollar fact does not answer a non-money question
        if metric and fact.metric == metric:
            score += 4
        if metric and fact.metric != metric and fact.metric in ("APY", "APR"):
            score -= 4
        if metric is None and deposit_q and fact.metric == "APY":
            score += 3
        if metric is None and deposit_q and fact.metric == "dividend rate":
            score -= 1
        if metric is None and loan_q and fact.metric == "APR":
            score += 3
        # Segment soft preference: unmarked questions are about personal
        # banking on this site; business facts need to be asked for.
        seg = fact.qualifiers.get("segment")
        if seg == "business" and not wants_business:
            score -= 3
        if seg == "personal" and wants_business:
            score -= 3
        if "term_months" in q_qual and fact.qualifiers.get("term_months") == q_qual["term_months"]:
            score += 4
        # Unspecified is weaker than explicit: when the question names
        # a term, cap or band, a fact that doesn't specify it at all
        # ranks below one that matches it. (An explicit *mismatch* is
        # already hard-filtered in _qualifier_compatible; this orders
        # the middle case the filter deliberately lets through.)
        for _qk in ("term_months", "loan_cap", "balance_band"):
            if _qk in q_qual and _qk not in fact.qualifiers:
                score -= 3
        if "flags" in q_qual:
            shared = set(q_qual["flags"]) & set(fact.qualifiers.get("flags", ()))
            score += 2 * len(shared)
            # An intro/promo rate must never answer a standard-rate question.
            if "standard" in q_qual["flags"] and "intro" in fact.qualifiers.get("flags", ()):
                score -= 8
        scored.append((score, fact, len(distinctive), len(s_distinctive)))
    scored.sort(key=lambda s: (-s[0], s[1].subject, s[1].metric))
    return scored[:limit]
