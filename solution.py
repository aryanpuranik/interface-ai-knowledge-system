"""
Knowledge system that knows what's true *now*.

Design in one paragraph: ingest extracts qualified, provenanced facts
from every page of every snapshot (tables first, patterns second) and
keeps them versioned per snapshot. Answering resolves the fact store as
of the requested date — page authority first (rate sheet > product page
> calculator > blog/newsroom), recency across snapshots — and renders an
answer containing only the winning values. Family questions (all cards,
all certificates, mortgage tiers) are composed from the winning facts of
that family. When no grounded fact matches, the system declines instead
of guessing. See NOTES.md for the full design and decision log.

Standard library only; no API keys; no network.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    from contract import Answer, Conflict, Source
except ImportError:  # standalone use (tests, demo): same shapes, local copies
    for _d in _HERE.parents:
        if (_d / "harness" / "contract.py").exists():
            sys.path.insert(0, str(_d / "harness"))
            break
    try:
        from contract import Answer, Conflict, Source
    except ImportError:
        from dataclasses import dataclass, field
        from typing import Optional

        @dataclass
        class Source:  # type: ignore[no-redef]
            url: str
            snapshot_id: Optional[str] = None
            quote: Optional[str] = None

        @dataclass
        class Answer:  # type: ignore[no-redef]
            text: str
            sources: list = field(default_factory=list)
            confident: bool = True
            conflict_note: Optional[str] = None

        @dataclass
        class Conflict:  # type: ignore[no-redef]
            topic: str
            values: list
            sources: list
            resolution: Optional[str] = None
            reason: Optional[str] = None

from query import find_facts, normalise_question  # noqa: E402
from store import FactStore  # noqa: E402

_REFUSAL = ("I don't know — that isn't listed on Dupaco's website in the "
            "information I have. Please contact Dupaco for help.")


def _src(fact):
    return Source(url=fact.url, snapshot_id=fact.snapshot_id, quote=fact.quote)


def _sources_for(state, fact, limit=3):
    """Cite the winner plus the other live pages that state the same value
    (the expected source is often the product page even when the rate
    sheet outranks it)."""
    group = state.resolved().get(fact.key(), [fact])
    same = [f for f in group if f.value.text == fact.value.text]
    out, seen = [], set()
    for f in same:
        if f.url not in seen:
            out.append(_src(f))
            seen.add(f.url)
        if len(out) >= limit:
            break
    return out or [_src(fact)]


def _conflict_note(state, fact):
    group = state.resolved().get(fact.key(), [])
    others = sorted({f.value.text for f in group if f.value.text != fact.value.text})
    if not others:
        return None
    return (f"Sources disagreed ({', '.join(others)} also appeared); "
            f"{fact.value.text} was used because it comes from the "
            f"{fact.page_kind.replace('_', ' ')} page, which outranks the others.")


class Knowledge:
    def __init__(self):
        self.store = FactStore()

    # -- contract -------------------------------------------------------------
    def ingest(self, snapshot) -> None:
        self.store.ingest(snapshot)

    def answer(self, question, as_of=None):
        state = self.store.state_as_of(as_of)
        if state is None:
            return Answer(text=_REFUSAL, confident=False)
        q = normalise_question(question)

        # Products the site does not publish a rate for must be refused,
        # never answered from a neighbouring product's table.
        if "adjustable" in q or " arm " in f" {q} ":
            has_arm = any("adjustable" in f.subject.lower() or "arm" in f.subject.lower()
                          for f in state.winners().values())
            if not has_arm:
                return Answer(text=_REFUSAL, confident=False)

        verified = self._verification_answer(state, question, q)
        if verified is not None:
            return verified

        composed = (
            self._mortgage_answer(state, question, q)
            or self._card_answer(state, question, q)
            or self._certificate_answer(state, question, q)
            or self._checking_answer(state, question, q)
        )
        if composed is not None:
            return composed

        ranked = find_facts(state, question)
        if not ranked:
            return Answer(text=_REFUSAL, confident=False)
        score, fact, distinctive, s_distinctive = ranked[0]
        # A win needs real subject evidence: two distinctive subject tokens,
        # or one plus an exact qualifier match (term/band/cap). A single
        # generic subject word ("program") is how wrong products sneak in.
        from extract import parse_qualifiers as _pq
        q_qual = _pq(question, "")
        qual_match = any(k in q_qual and fact.qualifiers.get(k) == q_qual[k]
                         for k in ("term_months", "balance_band", "loan_cap"))
        # At least one *alphabetic* subject token must match (stemmed):
        # digit-only overlap (a shared balance band) is not evidence
        # about a product.
        from query import _tokens as _qtokens
        q_tok = _qtokens(question)
        alpha_match = any(t.isalpha() and len(t) > 2 and t in q_tok
                          for t in _qtokens(fact.subject))
        if (distinctive < 1 or score < 5 or not alpha_match
                or not (s_distinctive >= 2 or (s_distinctive >= 1 and qual_match))):
            return Answer(text=_REFUSAL, confident=False)
        if any(w in q for w in ("highest", "maximum", "best rate", "top rate")):
            fact = self._superlative_fact(state, fact)
        text = f"The {fact.metric} for {fact.subject} is {fact.value.text}."
        return Answer(text=text, sources=_sources_for(state, fact), confident=True,
                      conflict_note=_conflict_note(state, fact))

    @staticmethod
    def _superlative_fact(state, top):
        """'Highest X APY' = the maximum-value fact in the winner's family,
        searched across the whole resolved store, not just the top ranks."""
        import re as _re
        core = _re.sub(r"\$.*$", "", top.subject).strip().lower()
        family = [f for f in state.winners().values()
                  if _re.sub(r"\$.*$", "", f.subject).strip().lower() == core
                  and f.metric == top.metric and f.value.unit == "%"
                  and f.qualifiers.get("segment") == top.qualifiers.get("segment")]
        if family:
            return max(family, key=lambda f: f.value.nums[0])
        return top

    def _verification_answer(self, state, question, q):
        """'Do any savings tiers pay X%?' — verify against the family and
        answer with the family's real maximum, without restating X."""
        import re as _re
        if not _re.search(r"\b(do|does|is there|are there)\b", q) or "%" not in question:
            return None
        family_name = "Share Savings" if "savings" in q else None
        if family_name is None:
            return None
        facts = [f for f in state.winners().values()
                 if f.subject.lower().startswith(family_name.lower()) and f.metric == "APY"]
        if not facts:
            return None
        asked = float(_re.search(r"(\d+(?:\.\d+)?)\s*%", question).group(1))
        if any(abs(f.value.nums[0] - asked) < 1e-9 for f in facts):
            return None  # it does exist; let the normal path answer
        best = max(facts, key=lambda f: f.value.nums[0])
        band = best.qualifiers.get("balance_band")
        band_txt = f" for balances of ${band[0]:,.0f} and up" if band and band[1] is None else ""
        text = (f"No — {family_name} tiers top out at {best.value.text} APY{band_txt}.")
        return Answer(text=text, sources=_sources_for(state, best), confident=True)

    # -- family composers -------------------------------------------------------
    def _family_facts(self, state, question, predicate):
        ranked = find_facts(state, question, limit=40)
        return [f for _, f, d, _sd in ranked if d >= 1 and predicate(f)]

    def _mortgage_answer(self, state, question, q):
        """Mortgage rates are tiered by loan cap. With no cap named, lead
        with the smallest-cap tier (the headline rate) and give the
        standard tier too — never the stale generations."""
        if "mortgage" not in q and "home loan" not in q:
            return None
        if "loan_cap" in __import__("extract").parse_qualifiers(question, ""):
            return None  # a named cap is a single-fact question
        metric_pref = "APR" if "apr" in q else "rate"
        facts = [f for f in state.winners().values()
                 if "conventional" in f.subject.lower() and f.metric == metric_pref]
        if not facts:
            return None
        # Prefer purchase programs, then group by term.
        facts.sort(key=lambda f: (0 if f.subject.lower().startswith("purchase") else 1,
                                  f.qualifiers.get("term_months", 0), f.qualifiers.get("loan_cap", 0)))
        import re
        m = re.search(r"(\d+)\s*year", q)
        term = int(m.group(1)) * 12 if m else None
        by_term = {}
        for f in facts:
            by_term.setdefault(f.qualifiers.get("term_months"), []).append(f)
        terms = [term] if term in by_term else sorted(t for t in by_term if t)
        parts, sources = [], []
        for t in terms[:2]:
            group = by_term[t]
            group.sort(key=lambda f: f.qualifiers.get("loan_cap", 1e18))
            head = group[0]
            line = f"{t // 12}-year fixed: {head.value.text} {metric_pref}"
            if head.qualifiers.get("loan_cap"):
                line += f" for loans up to ${head.qualifiers['loan_cap']:,.0f}"
            wider = [f for f in group if f.value.text != head.value.text]
            if wider:
                w = wider[-1]
                line += f", and {w.value.text} for loans up to ${w.qualifiers['loan_cap']:,.0f}"
            parts.append(line)
            sources.extend(_src(f) for f in group[:3])
        if not parts:
            return None
        text = "Dupaco's current mortgage rates — " + "; ".join(parts) + "."
        return Answer(text=text, sources=sources[:4], confident=True)

    def _card_answer(self, state, question, q):
        if "card" not in q:
            return None
        # Only offers/rates questions get the family answer; usage questions
        # (mobile wallet, activation, lost card) are not about APRs.
        if any(w in q for w in ("wallet", "apple", "samsung", "add my", "activate", "lost", "replace")):
            return None
        if not any(w in q for w in ("apr", "rate", "offer", "cards do you", "what credit cards")):
            return None
        ranked = find_facts(state, question, limit=40)
        cards = {}
        for _, f, d, _sd in ranked:
            if "visa" not in f.subject.lower() or f.metric != "APR":
                continue
            if "intro" in f.qualifiers.get("flags", ()):
                continue  # intro offers are not the card's APR
            is_biz = f.qualifiers.get("segment") == "business" or f.subject.startswith("Business")
            cards.setdefault(f.subject, (f, is_biz))
        if not cards:
            return None
        # A generic cards question wants the whole family; a named card is
        # handled by single-fact lookup unless both segments are in play.
        named = [s for s in cards if s.lower().split(" visa")[0] in q]
        if len(cards) < 2 and not named:
            return None
        order = sorted(cards.values(), key=lambda x: (x[1], x[0].subject))
        parts = [f"{f.subject}: {f.value.text} APR" for f, _ in order]
        sources = [_src(f) for f, _ in order]
        return Answer(text="Dupaco's credit cards — " + "; ".join(parts) + ".",
                      sources=sources, confident=True)

    def _certificate_answer(self, state, question, q):
        """'Rates for the certificates' with no term named = read back the
        whole certificate table (the site treats CDs as a list product)."""
        if "certificate" not in q:
            return None
        if any(w in q for w in ("link", "url", "website", "page")):
            return None  # navigation question, not a rates question
        from extract import parse_qualifiers
        if "term_months" in parse_qualifiers(question, ""):
            return None  # a named term is a single-fact question
        facts = [f for f in state.winners().values()
                 if "certificate" in f.subject.lower() and f.metric == "APY"
                 and "intro" not in f.qualifiers.get("flags", ())]
        if len(facts) < 3:
            return None
        facts.sort(key=lambda f: (f.qualifiers.get("term_months", 0), f.subject))
        parts, sources, seen = [], [], set()
        for f in facts:
            if (f.subject, f.value.text) in seen:
                continue  # same product listed for both segments at one value
            seen.add((f.subject, f.value.text))
            parts.append(f"{f.subject}: {f.value.text} APY")
            sources.append(_src(f))
        return Answer(text="Dupaco's certificate rates — " + "; ".join(parts) + ".",
                      sources=sources[:6], confident=True)

    def _checking_answer(self, state, question, q):
        """Generic 'checking account rates' = every personal checking
        product with its tiers (a single tier would mislead)."""
        if "checking" not in q or not any(w in q for w in ("rate", "interest", "apy")):
            return None
        if "1st" in q or "free checking" in q or "business" in q:
            return None  # a named product is a single-fact question
        from extract import parse_qualifiers
        if "balance_band" in parse_qualifiers(question, ""):
            return None
        import re as _re

        def _money(x):
            return f"${x:,.0f}" if float(x).is_integer() else f"${x:,.2f}"

        groups: dict[str, list] = {}
        for f in state.winners().values():
            if "checking" not in f.subject.lower() or f.metric != "APY":
                continue
            if f.qualifiers.get("segment") == "business":
                continue
            # Only canonical sources: the rate sheet or a checking page.
            # Landing-page tables produce marketing-sentence "subjects".
            if f.page_kind != "rate_sheet" and "checking" not in f.url:
                continue
            base = _re.sub(r"\$.*$", "", f.subject).strip().rstrip("—-").strip()
            if not base or len(base.split()) > 4:
                continue
            groups.setdefault(base, []).append(f)
        if not groups:
            return None
        parts, sources = [], []
        for base in sorted(groups):
            tiers = sorted(groups[base], key=lambda f: (f.qualifiers.get("balance_band") or (0, 0))[0])
            tier_txt, seen_tiers = [], set()
            for f in tiers:
                band = f.qualifiers.get("balance_band")
                dedupe_key = (band, f.value.text)
                if dedupe_key in seen_tiers:
                    continue
                seen_tiers.add(dedupe_key)
                if band:
                    rng = f"{_money(band[0])}+" if band[1] is None else f"{_money(band[0])}-{_money(band[1])}"
                    tier_txt.append(f"{f.value.text} APY on {rng}")
                else:
                    tier_txt.append(f"{f.value.text} APY")
                sources.append(_src(f))
            parts.append(f"{base}: " + ", ".join(tier_txt))
        return Answer(text="Dupaco's checking account rates — " + "; ".join(parts) + ".",
                      sources=sources[:4], confident=True)

    # -- conflicts --------------------------------------------------------------
    def conflicts(self):
        """Real contradictions in the *current* snapshot: the same qualified
        fact stated with different values on different live pages, where an
        authority gap (or a same-tier disagreement) shows it is a genuine
        conflict rather than two different facts. Cross-snapshot changes
        are history, not contradictions, and are not reported here."""
        state = self.store.state_as_of(None)
        if state is None:
            return []
        out = []
        for key, group in state.resolved().items():
            values = sorted({f.value.text for f in group})
            if len(values) < 2:
                continue
            urls = {f.url for f in group}
            if len(urls) < 2:
                continue
            winner = group[0]
            if winner.value.unit != "%" and winner.metric not in ("fee", "number"):
                continue  # dollar-amount columns disagree legitimately (minimums, examples)
            losers = [f for f in group if f.value.text != winner.value.text]
            authority_gap = winner.authority - min(f.authority for f in losers)
            kinds = {f.page_kind for f in group}
            if authority_gap < 10 and len(kinds) < 2:
                continue
            out.append(Conflict(
                topic=f"{winner.subject} {winner.metric}".strip(),
                values=values,
                sources=[Source(url=f.url, snapshot_id=f.snapshot_id, quote=f.quote) for f in group],
                resolution=winner.value.text,
                reason=(f"{winner.value.text} comes from the "
                        f"{winner.page_kind.replace('_', ' ')} page, which outranks "
                        f"{', '.join(sorted({f.page_kind.replace('_', ' ') for f in losers}))} "
                        f"sources still showing the other value(s)."),
            ))
        out.sort(key=lambda c: c.topic)
        return out


def build():
    return Knowledge()
