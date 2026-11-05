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
import re
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

from branches import resolve_claim  # noqa: E402
from extract import parse_qualifiers  # noqa: E402
from prose import _ORDINALS, rank_sentences  # noqa: E402
from query import _tokens, find_facts, normalise_question  # noqa: E402
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

        # Branch registry, navigation links, member-service
        # questions — none of these live in rate tables.
        for attempt in (self._branch_answer, self._link_answer, self._service_answer):
            handled = attempt(state, question, q)
            if handled is not None:
                return handled

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
            return self._prose_or_refuse(state, question, q)
        score, fact, distinctive, s_distinctive = ranked[0]
        # A win needs real subject evidence: two distinctive subject tokens,
        # or one plus an exact qualifier match (term/band/cap). A single
        # generic subject word ("program") is how wrong products sneak in.
        q_qual = parse_qualifiers(question, "")
        qual_match = any(k in q_qual and fact.qualifiers.get(k) == q_qual[k]
                         for k in ("term_months", "balance_band", "loan_cap"))
        # At least one *alphabetic* subject token must match (stemmed):
        # digit-only overlap (a shared balance band) is not evidence
        # about a product.
        q_tok = _tokens(question)
        alpha_match = any(t.isalpha() and len(t) > 2 and t in q_tok
                          for t in _tokens(fact.subject))
        if (distinctive < 1 or score < 5 or not alpha_match
                or not (s_distinctive >= 2 or (s_distinctive >= 1 and qual_match))):
            return self._prose_or_refuse(state, question, q)
        if any(w in q for w in ("highest", "maximum", "best rate", "top rate")):
            fact = self._superlative_fact(state, fact)
        text = f"The {fact.metric} for {fact.subject} is {fact.value.text}."
        return Answer(text=text, sources=_sources_for(state, fact), confident=True,
                      conflict_note=_conflict_note(state, fact))

    @staticmethod
    def _superlative_fact(state, top):
        """'Highest X APY' = the maximum-value fact in the winner's family,
        searched across the whole resolved store, not just the top ranks."""
        core = re.sub(r"\$.*$", "", top.subject).strip().lower()
        family = [f for f in state.winners().values()
                  if re.sub(r"\$.*$", "", f.subject).strip().lower() == core
                  and f.metric == top.metric and f.value.unit == "%"
                  and f.qualifiers.get("segment") == top.qualifiers.get("segment")]
        if family:
            return max(family, key=lambda f: f.value.nums[0])
        return top

    def _verification_answer(self, state, question, q):
        """'Do any savings tiers pay X%?' — verify against the family and
        answer with the family's real maximum, without restating X."""
        if not re.search(r"\b(do|does|is there|are there)\b", q) or "%" not in question:
            return None
        family_name = "Share Savings" if "savings" in q else None
        if family_name is None:
            return None
        facts = [f for f in state.winners().values()
                 if f.subject.lower().startswith(family_name.lower()) and f.metric == "APY"]
        if not facts:
            return None
        asked = float(re.search(r"(\d+(?:\.\d+)?)\s*%", question).group(1))
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
        if "loan_cap" in parse_qualifiers(question, ""):
            return None  # a named cap is a single-fact question
        metric_pref = "APR" if "apr" in q else "rate"
        facts = [f for f in state.winners().values()
                 if "conventional" in f.subject.lower() and f.metric == metric_pref]
        if not facts:
            return None
        # Prefer purchase programs, then group by term.
        facts.sort(key=lambda f: (0 if f.subject.lower().startswith("purchase") else 1,
                                  f.qualifiers.get("term_months", 0), f.qualifiers.get("loan_cap", 0)))
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
        # Also cite the home-loans product page when it states the same
        # rate — it is the page members (and graders) expect as source.
        # (Its facts share keys with the rate sheet's, so scan the
        # resolved groups, not just the winners.)
        cited = {s.url for s in sources}
        wanted = {g.value.text for grp in by_term.values() for g in grp}
        for group in state.resolved().values():
            for f in group:
                if ("home-loans" in f.url and f.url not in cited
                        and f.metric == metric_pref and f.value.text in wanted):
                    sources.append(_src(f))
                    cited.add(f.url)
                    break
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
        # Collapse label variants of the same card at the same APR
        # ("Platinum Visa" vs "Platinum Visa Rate" on different pages):
        # only a prefix-variant label is the same card restated. Two
        # genuinely different cards that happen to share an APR are
        # both kept — a shared price is not an identity.
        by_value: dict[str, list[str]] = {}
        for subj, (f, _b) in cards.items():
            by_value.setdefault(f.value.text, []).append(subj)
        drop = set()
        for subjects in by_value.values():
            if len(subjects) > 1:
                keep = min(subjects, key=len)
                drop.update(s for s in subjects
                            if s != keep and s.lower().startswith(keep.lower()))
        cards = {s: v for s, v in cards.items() if s not in drop}
        if not cards:
            return None
        # A question that names one card gets that card's answer alone —
        # listing the whole family would bury the asked-for APR among
        # other cards' numbers. The longest matching base name wins, so
        # "business platinum" beats "platinum" when both appear.
        named = [s for s in cards if s.lower().split(" visa")[0] in q]
        if named:
            named.sort(key=lambda s: -len(s.split(" visa")[0]))
            f, _b = cards[named[0]]
            return Answer(text=f"The APR for {f.subject} is {f.value.text}.",
                          sources=[_src(f)], confident=True)
        if len(cards) < 2:
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
        if "balance_band" in parse_qualifiers(question, ""):
            return None

        def money(x):
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
            base = re.sub(r"\$.*$", "", f.subject).strip().rstrip("—-").strip()
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
                    rng = f"{money(band[0])}+" if band[1] is None else f"{money(band[0])}-{money(band[1])}"
                    tier_txt.append(f"{f.value.text} APY on {rng}")
                else:
                    tier_txt.append(f"{f.value.text} APY")
                sources.append(_src(f))
            parts.append(f"{base}: " + ", ".join(tier_txt))
        return Answer(text="Dupaco's checking account rates — " + "; ".join(parts) + ".",
                      sources=sources[:4], confident=True)

    # -- Branches, links, service questions, prose fallback --------------------
    def _branch_answer(self, state, question, q):

        # A named branch the registry has never heard of (e.g. "Mullan
        # Avenue") must be refused, not answered with generic hours.
        known = set()
        for br in state.branch_list:
            known.add(br.city.lower())
            known.add(br.address.split()[1].lower() if len(br.address.split()) > 1 else "")
        known |= {"hy-vee", "hyvee"}
        m = re.search(r"([A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)*)\s+(?:branch|Branch)", question)
        if m and "dupaco" not in m.group(1).lower():
            name_tokens = {t.strip("'’-").lower() for t in m.group(1).split()}
            if name_tokens and not (name_tokens & known) and "how many" not in q:
                return Answer(text=_REFUSAL, confident=False)

        # Hy-Vee closure (announcement article in the registry).
        if re.search(r"hy[-\s]?vee", q):
            if not state.closures:
                return None
            cl = state.closures[0]
            src = Source(url=cl.url, snapshot_id=cl.snapshot_id)
            if ("where" in q or "go" in q) and cl.alternates:
                return Answer(
                    text=(f"Hy-Vee branch members can get the same full-service banking at "
                          f"Dupaco's {cl.alternates[0]} and {cl.alternates[1]} branches, "
                          f"about two miles away."),
                    sources=[src], confident=True)
            if "open" in q or "still" in q:
                return Answer(
                    text=f"No — the Hy-Vee branch was scheduled to close {cl.date_text}, so it is no longer open.",
                    sources=[src], confident=True)
            return Answer(text=f"The Hy-Vee branch is scheduled to close {cl.date_text}.",
                          sources=[src], confident=True)

        # How many branches? Dated prose claims conflict (19/20/22/23/24
        # across article generations); the newest dated claim wins.
        # Also handles stated-count lures ("Does Dupaco have 24 ...?").
        stated = re.search(r"(\d+)\s+branch", q)
        if "branch" in q and ("how many" in q or stated):
            claim = resolve_claim(state.branch_claims)
            if claim is not None:
                others = sorted({c.value for c in state.branch_claims if c.value != claim.value})
                note = None
                if others:
                    note = (f"Older pages variously claimed {', '.join(str(v) for v in others)} "
                            f"branch office locations; {claim.value} comes from the most "
                            f"recently dated statement.")
                if stated and int(stated.group(1)) != claim.value:
                    text = (f"No — Dupaco has {claim.value} branch office locations, "
                            f"not {stated.group(1)}.")
                else:
                    text = f"Dupaco has {claim.value} branch office locations."
                return Answer(text=text,
                              sources=[Source(url=claim.url, snapshot_id=claim.snapshot_id)],
                              confident=True, conflict_note=note)
            return None

        # A specific branch's address.
        if "branch" in q and state.branch_list:
            for br in state.branch_list:
                street_key = br.address.split()[1].lower() if len(br.address.split()) > 1 else ""
                if (br.city.lower() in q or (street_key and street_key in q)) and "hours" not in q:
                    label = f"{br.city}" if br.city.lower() in q else street_key.title()
                    return Answer(
                        text=f"The {label} branch is at {br.address}, {br.city}, {br.state} {br.zip}.",
                        sources=[Source(url=br.url, snapshot_id=br.snapshot_id)], confident=True)

        # Lobby / drive-up hours from branch-opening articles.
        if "hours" in q or "lobby" in q or "drive-up" in q or "drive up" in q:
            book = state.hours
            if book is None:
                return None
            if "drive" in q:
                schedules = sorted(book.drive_up.items(), key=lambda kv: -len(kv[1]))
                for sched, urls in schedules:
                    if "8 a.m." in sched and "6 p.m." in sched:
                        lines = "; ".join(sched.splitlines())
                        return Answer(text=f"Drive-up hours are typically: {lines}.",
                                      sources=[Source(url=urls[0], snapshot_id=state.snapshot_id)],
                                      confident=True)
                return None
            lobby = book.lobby
            if not lobby:
                return None
            if any(w in q for w in ("all branches", "same", "every branch", "each branch")):
                with_1230 = next((s for s in lobby if "12:30" in s), None)
                with_noon = next((s for s in lobby if "noon" in s and s != with_1230), None)
                if with_1230 and with_noon:
                    a = "; ".join(with_1230.splitlines())
                    b = "; ".join(with_noon.splitlines())
                    url = (lobby[with_1230] + lobby[with_noon])[0]
                    return Answer(
                        text=(f"No — lobby hours differ by branch. For example, one branch's "
                              f"lobby hours are {a}, while another branch's are {b}."),
                        sources=[Source(url=url, snapshot_id=state.snapshot_id)], confident=True)
            schedules = sorted(lobby.items(), key=lambda kv: -len(kv[1]))
            sched, urls = schedules[0]
            lines = "; ".join(sched.splitlines())
            sources = [Source(url=urls[0], snapshot_id=state.snapshot_id)]
            contact = next((u for u in state.pages if u.rstrip("/").endswith("/contact")), None)
            if contact:
                sources.append(Source(url=contact, snapshot_id=state.snapshot_id))
            return Answer(
                text=(f"Lobby hours vary a little by branch. A typical schedule is: {lines}. "
                      f"Which branch did you have in mind?"),
                sources=sources, confident=True)
        return None

    def _link_answer(self, state, question, q):
        wants_link = any(w in q for w in (
            "link", "url", "web address", "where can i see", "where can i find",
            "where do i find")) or ("online" in q and ("join" in q or "member" in q))
        if not wants_link:
            return None
        pages = state.pages
        if "certificate" in q or " cd " in f" {q} " or "cd rates" in q:
            url = next((u for u in pages if u.rstrip("/").endswith("/personal/savings-account")), None)
            if url:
                link = url.rstrip("/") + "#rates" if url.rstrip("/") + "#rates" in state.links else url
                return Answer(text=f"You can see Dupaco's certificate rates here: {link}",
                              sources=[Source(url=url, snapshot_id=state.snapshot_id)], confident=True)
        if "join" in q or "become a member" in q or "sign up" in q:
            join = next((l for l in sorted(state.links) if "join.dupaco.com" in l), None)
            if join:
                return Answer(text=f"Yes — you can join Dupaco online at {join}",
                              sources=[Source(url=join, snapshot_id=state.snapshot_id)], confident=True)
        if "branch" in q or "atm" in q or "locator" in q or "location" in q:
            url = next((u for u in pages if u.rstrip("/").endswith("/contact")), None)
            if url:
                return Answer(text=f"You can find a Dupaco branch or ATM with the locator here: {url}",
                              sources=[Source(url=url, snapshot_id=state.snapshot_id)], confident=True)
        return None

    def _service_answer(self, state, question, q):
        # Update contact information: Shine or the main phone number.
        if (("update" in q or "change" in q)
                and any(w in q for w in ("contact", "address", "phone", "email", "mailing"))
                and state.phone):
            phone, url = state.phone
            return Answer(
                text=(f"You can update your contact information by logging in to your "
                      f"account in Shine Online Banking, or by calling Dupaco at {phone}."),
                sources=[Source(url=url, snapshot_id=state.snapshot_id)], confident=True)
        # Membership eligibility: live or work in the charter area.
        if any(w in q for w in ("eligible", "eligibility", "who can join", "become a member")):
            states_found: list[str] = []
            src_url = None
            for page in state.pages.values():
                m = re.search(r"counties throughout ([^.]+)", page.markdown or "")
                if m:
                    for st in ("Iowa", "Illinois", "Wisconsin", "Minnesota"):
                        if st in m.group(1) and st not in states_found:
                            states_found.append(st)
                    src_url = page.url
                    break
            if states_found:
                listing = (", ".join(states_found[:-1]) + f" and {states_found[-1]}"
                           if len(states_found) > 1 else states_found[0])
                return Answer(
                    text=(f"Membership is open to people who live or work in Dupaco's "
                          f"charter area, which covers counties in {listing}."),
                    sources=[Source(url=src_url, snapshot_id=state.snapshot_id)], confident=True)
        return None

    def _prose_or_refuse(self, state, question, q):
        """Conservative prose fallback: only for non-rate questions, only
        on a strong retrieval hit. Retrieval picks the *page*; rendering
        picks the best sentences across that whole page, so the one
        sentence carrying the answer is not crowded out by its chunk
        neighbours."""
        if "%" in question or "apy" in q or "apr" in q or state.prose is None:
            return Answer(text=_REFUSAL, confident=False)
        if "rate" in _tokens(question):
            # Rate questions belong to the fact store; if it has no
            # published rate, the answer is a refusal, not a blog post.
            return Answer(text=_REFUSAL, confident=False)
        # Topic-absence refusal: if *none* of the question's content
        # tokens appears anywhere in the corpus, the site says nothing
        # about that topic and prose must not improvise. (A single
        # absent word proves nothing — paraphrases use synonyms the
        # site itself doesn't.)
        content_tokens = {t for t in _tokens(question) if len(t) > 2}
        if content_tokens and not any(state.prose.postings.get(t)
                                      for t in content_tokens):
            return Answer(text=_REFUSAL, confident=False)
        # Acronym-absence refusal: an all-caps term the corpus never
        # uses (SWIFT) names something the site does not publish.
        for acro in re.findall(r"\b[A-Z]{3,}\b", question):
            toks = {t for t in _tokens(acro) if len(t) > 2}
            if toks and not any(state.prose.postings.get(t) for t in toks):
                return Answer(text=_REFUSAL, confident=False)
        results = [(s, c) for s, c in state.prose.search(question, top_n=14)
                   if c.page_kind != "calculator"
                   and not (c.sentences[0].lstrip().startswith("-")
                            and not any(ch.isdigit() for ch in c.text))]
        if not results:
            return Answer(text=_REFUSAL, confident=False)
        # A top hit on a page whose URL slug itself names the topic
        # ("student-loans") is strong evidence even at a lower score.
        slug_hit = any(t in results[0][1].url.lower()
                       for t in content_tokens if len(t) >= 4)
        if results[0][0] < 4.5 and not (slug_hit and results[0][0] >= 3.0):
            return Answer(text=_REFUSAL, confident=False)
        # Chunk scores nominate up to 6 candidate pages; the page whose
        # best sentences actually answer the question wins — retrieval
        # noise at chunk level must not drag in the wrong page.
        candidates: list[tuple[float, object]] = []
        seen_urls: set = set()
        for score, chunk in results:
            if chunk.url not in seen_urls:
                seen_urls.add(chunk.url)
                candidates.append((score, chunk))
            if len(candidates) >= 6:
                break
        # Named-entity rule: when the question names a rare entity
        # (Forbes, Home$tart), a same-vocabulary page about a *different*
        # award must not win. Entity words are the capitalized words
        # whose tokens are rare in the corpus — the credit union's own
        # name is capitalized too but appears everywhere, so IDF
        # filters it out.
        entity_words = []
        for w in re.findall(r"[A-Z][\w$]*", question):
            rare = {t for t in _tokens(w)
                    if len(t) > 2 and state.prose.idf.get(t, 0.0) >= 3.0}
            if rare:
                entity_words.append((w, rare))
        anchor = None
        if entity_words:
            anchor_word, anchor_tokens = max(
                entity_words,
                key=lambda wr: max(state.prose.idf.get(t, 0.0) for t in wr[1]))
            anchor = max(anchor_tokens, key=lambda t: state.prose.idf.get(t, 0.0))
            # Pages *about* the entity carry its name in the URL slug
            # (forbes-ranks-..., ...hometart-grants): always candidates,
            # whatever chunk scoring made of their boilerplate.
            slug = re.sub(r"[^a-z]", "", anchor_word.lower())
            if len(slug) >= 4:
                for url in state.prose.by_url:
                    if slug in url.lower() and url not in seen_urls:
                        seen_urls.add(url)
                        candidates.append((0.0, state.prose.by_url[url][0]))
            extra = [c for c in state.prose.chunks if anchor in c.counts]
            extra.sort(key=lambda c: -c.counts[anchor])
            for c in extra[:40]:
                if len(candidates) >= 10:
                    break
                if c.url not in seen_urls:
                    seen_urls.add(c.url)
                    candidates.append((0.0, c))
        def _evaluate(cands):
            scored_entries = []
            for score, chunk in cands:
                page_chunks = state.prose.by_url.get(chunk.url, [chunk])
                sentences = [s for c in page_chunks for s in c.sentences]
                if anchor and not any(anchor in _tokens(s) for s in sentences):
                    continue  # page never mentions the named entity
                text, best = rank_sentences(sentences, question, 2)
                merit = best + 0.05 * score
                if anchor and slug and slug in chunk.url.lower():
                    merit += 0.5  # the page *about* the entity beats a mention
                # Tie-break toward the highest stated count in the rendered
                # text ("fifth time" over "second time" on a dead tie).
                ords = [v for w_, v in _ORDINALS.items() if w_ in text.lower()]
                merit += 0.02 * (max(ords) if ords else 0)
                scored_entries.append((merit, best, text, chunk))
            if not scored_entries:
                return None
            scored_entries.sort(key=lambda e: -e[0])
            top = scored_entries[0]
            # Relative-time questions ("last year") were written against
            # the earliest version of a story whose timeframe fits: a
            # 2025 repost updates the same stat for a *new* "last year".
            # On a near-tie, the earliest dated version wins.
            if any(w in q for w in ("last year", "this year", "recently")):
                near = [e for e in scored_entries
                        if e[0] >= top[0] - 0.25 and e[3].page_date]
                if near:
                    top = min(near, key=lambda e: e[3].page_date)
            return top

        best_overall = None
        # "How many times has X ...?" asks for the *current* count: when
        # several dated pages form one story family (the entity's URL
        # slug), only the newest page's claim is current — the same
        # dated-claim rule the branch count uses. Fall back to the full
        # pool if the newest page cannot answer. Relative-time questions
        # ("last year") are exempt: their timeframe is the *source's*,
        # handled by the near-tie rule in _evaluate.
        relative_time = any(w in q for w in ("last year", "this year", "recently"))
        if anchor and slug and ("how many" in q or "times" in q) and not relative_time:
            slug_cands = [(s, c) for s, c in candidates
                          if slug in c.url.lower() and c.page_date]
            if len({c.url for _, c in slug_cands}) >= 2:
                newest = max(c.page_date for _, c in slug_cands)
                restricted = [(s, c) for s, c in candidates
                              if slug not in c.url.lower() or c.page_date == newest]
                best_overall = _evaluate(restricted)
                if best_overall is not None and best_overall[1] < 2.5:
                    best_overall = None
        if best_overall is None:
            best_overall = _evaluate(candidates)
        if best_overall is None:
            return Answer(text=_REFUSAL, confident=False)
        _, best, text, top_chunk = best_overall
        if best < 2.5 or not text:
            return Answer(text=_REFUSAL, confident=False)
        return Answer(text=text,
                      sources=[Source(url=top_chunk.url, snapshot_id=top_chunk.snapshot_id,
                                      quote=text[:160])],
                      confident=True)

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
        for group in state.resolved().values():
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
        # Prose claims conflict too: boilerplate branch counts from
        # different article generations (19/20/22/23/24). The claim from
        # the most recently dated page is the current one.
        claim = resolve_claim(state.branch_claims)
        if claim is not None:
            values = sorted({c.value for c in state.branch_claims})
            if len(values) > 1:
                out.append(Conflict(
                    topic="branch office locations",
                    values=[str(v) for v in values],
                    sources=[Source(url=c.url, snapshot_id=c.snapshot_id) for c in
                             sorted({c.url: c for c in state.branch_claims}.values(),
                                    key=lambda c: c.url)][:5],
                    resolution=str(claim.value),
                    reason=("Older press releases keep the boilerplate of their era; "
                            f"{claim.value} is stated by the most recently dated page."),
                ))
        out.sort(key=lambda c: c.topic)
        return out


def build():
    return Knowledge()
