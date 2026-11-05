"""Temporal fact store.

Facts are never overwritten. Each snapshot's facts are kept as that
snapshot's *state of the world*, so a question can be answered as of any
date by resolving the newest snapshot on or before it. Inside one
snapshot, facts that share a canonical key but disagree are resolved by
page authority (rate sheet > product page > calculator > blog/news),
which is also what powers conflicts().
"""
from __future__ import annotations

import re

import branches as branch_mod
from extract import Fact, extract_facts
from prose import ProseIndex

_LINK_RE = re.compile(r"\]\((https?://[^)\s]+)\)")


class SnapshotState:
    def __init__(self, snapshot_id: str, taken_on: str):
        self.snapshot_id = snapshot_id
        self.taken_on = taken_on
        self.facts: list[Fact] = []
        self.pages: dict = {}          # url -> Page (kept for later retrieval fallback)
        # Phase 3 registries, built alongside the facts at ingest.
        self.branch_list: list = []
        self.hours = None
        self.closures: list = []
        self.branch_claims: list = []
        self.phone: tuple | None = None
        self.prose: ProseIndex | None = None
        self.links: set = set()

    def resolved(self) -> dict[tuple, list[Fact]]:
        """key -> all facts claiming that key in this snapshot, best first."""
        groups: dict[tuple, list[Fact]] = {}
        for f in self.facts:
            groups.setdefault(f.key(), []).append(f)
        for key in groups:
            groups[key].sort(key=lambda f: (-f.authority, f.url))
        return groups

    def winners(self) -> dict[tuple, Fact]:
        return {k: v[0] for k, v in self.resolved().items()}


class FactStore:
    def __init__(self):
        self.states: list[SnapshotState] = []   # ingest order = oldest first

    # -- ingest -------------------------------------------------------------
    def ingest(self, snapshot) -> SnapshotState:
        state = SnapshotState(snapshot.snapshot_id, snapshot.taken_on)
        for page in snapshot.pages:
            state.pages[page.url] = page
            state.links.update(_LINK_RE.findall(page.markdown or ""))
            for fact in extract_facts(page):
                fact.snapshot_id = snapshot.snapshot_id
                fact.taken_on = snapshot.taken_on
                state.facts.append(fact)
        pages = list(state.pages.values())
        sid = snapshot.snapshot_id
        state.branch_list = branch_mod.parse_locator(pages, sid)
        state.hours = branch_mod.parse_hours(pages)
        state.closures = branch_mod.parse_closures(pages, sid)
        state.branch_claims = branch_mod.parse_branch_count_claims(pages, sid)
        state.phone = branch_mod.contact_phone(pages)
        state.prose = ProseIndex.build(pages, sid)
        self.states.append(state)
        return state

    # -- time travel ----------------------------------------------------------
    def state_as_of(self, as_of: str | None) -> SnapshotState | None:
        """The snapshot in force on `as_of` (ISO date); latest when None."""
        if not self.states:
            return None
        if as_of is None:
            return self.states[-1]
        eligible = [s for s in self.states if s.taken_on <= as_of]
        return eligible[-1] if eligible else self.states[0]

    # -- diff (debugging + conflicts across time) ------------------------------
    def changed_facts(self, older: SnapshotState, newer: SnapshotState) -> list[tuple]:
        """(key, old_value, new_value) for facts whose winner changed."""
        ow, nw = older.winners(), newer.winners()
        out = []
        for key in sorted(set(ow) & set(nw), key=str):
            if ow[key].value.text != nw[key].value.text:
                out.append((key, ow[key].value.text, nw[key].value.text))
        return out
