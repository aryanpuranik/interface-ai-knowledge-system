"""
Knowledge system that knows what's true *now*.

Design in one paragraph: ingest extracts qualified, provenanced facts
from every page of every snapshot (tables first, patterns second) and
keeps them versioned per snapshot. Answering resolves the fact store as
of the requested date — page authority first (rate sheet > product page
> calculator > blog/newsroom), recency across snapshots — and renders an
answer containing only the winning value. When no grounded fact matches,
the system declines instead of guessing. See NOTES.md for the full
design and decision log.

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

from query import find_facts  # noqa: E402
from store import FactStore  # noqa: E402

_REFUSAL = ("I don't know — that isn't listed on Dupaco's website as of the "
            "latest information I have. Please contact Dupaco for help.")


class Knowledge:
    def __init__(self):
        self.store = FactStore()

    # -- contract -------------------------------------------------------------
    def ingest(self, snapshot) -> None:
        self.store.ingest(snapshot)

    def answer(self, question, as_of=None):
        state = self.store.state_as_of(as_of)
        ranked = find_facts(state, question)
        if not ranked:
            return Answer(text=_REFUSAL, confident=False)
        score, fact = ranked[0]
        if score < 2:
            return Answer(text=_REFUSAL, confident=False)
        text = f"The {fact.metric} for {fact.subject} is {fact.value.text}."
        note = None
        # If other live pages disagreed with the winner, say why we picked it
        # (in conflict_note, which is explanation, not answer text).
        group = state.resolved().get(fact.key(), [])
        others = {f.value.text for f in group if f.value.text != fact.value.text}
        if others:
            note = (f"Sources disagreed ({', '.join(sorted(others))} also appeared); "
                    f"{fact.value.text} was used because it comes from the "
                    f"{fact.page_kind.replace('_', ' ')} page, which outranks the others.")
        return Answer(
            text=text,
            sources=[Source(url=fact.url, snapshot_id=fact.snapshot_id, quote=fact.quote)],
            confident=True,
            conflict_note=note,
        )

    def conflicts(self):
        state = self.store.state_as_of(None)
        if state is None:
            return []
        out = []
        for key, group in state.resolved().items():
            values = sorted({f.value.text for f in group})
            if len(values) < 2:
                continue
            winner = group[0]
            out.append(Conflict(
                topic=f"{winner.subject} {winner.metric}".strip(),
                values=values,
                sources=[Source(url=f.url, snapshot_id=f.snapshot_id, quote=f.quote) for f in group],
                resolution=winner.value.text,
                reason=(f"{winner.page_kind.replace('_', ' ')} page outranks "
                        f"the other sources in the current snapshot."),
            ))
        return out


def build():
    return Knowledge()
