#!/usr/bin/env python3
"""Interactive demo for the Dupaco knowledge system.

Loads the five snapshots once, then answers questions in a REPL (or
one-shot from the command line). Not part of the graded path — it
exists so a reviewer can poke at the system in a terminal.

Usage:
    python3 tools/demo.py --kit /path/to/hackathon-knowledge-kit-participant
    python3 tools/demo.py --kit ... "What is the routing number?"
    python3 tools/demo.py --kit ... --as-of 2026-05-15 "What was the 10-month jumbo APY?"

REPL commands:
    conflicts            list the contradictions in the current snapshot
    asof YYYY-MM-DD ...  ask the following question as of that date
    quit                 exit
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load(kit: Path):
    sys.path.insert(0, str(kit / "harness"))
    from run_eval import load_snapshots
    import solution
    ks = solution.build()
    for snap in load_snapshots(kit / "snapshots"):
        ks.ingest(snap)
    return ks


def _show(ans) -> None:
    print(f"\n{ans.text}")
    for s in ans.sources:
        print(f"  source: {s.url} [{s.snapshot_id}]")
    if ans.conflict_note:
        print(f"  conflict note: {ans.conflict_note}")
    print(f"  confident: {ans.confident}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", required=True, help="participant kit directory")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("question", nargs="*")
    args = ap.parse_args()

    print("Loading snapshots...")
    ks = _load(Path(args.kit))
    n = len(ks.store.states)
    print(f"Ready — {n} snapshots ingested "
          f"({sum(len(s.facts) for s in ks.store.states)} facts total).")

    if args.question:
        _show(ks.answer(" ".join(args.question), as_of=args.as_of))
        return 0

    print("Ask a question, or: 'conflicts', 'asof YYYY-MM-DD <question>', 'quit'.")
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.lower() in ("quit", "exit"):
            return 0
        if line.lower() == "conflicts":
            for c in ks.conflicts():
                print(f"- {c.topic}: {c.values} -> resolved: {c.resolution} ({c.reason})")
            continue
        as_of = None
        if line.lower().startswith("asof "):
            parts = line.split(None, 2)
            if len(parts) == 3:
                as_of, line = parts[1], parts[2]
        _show(ks.answer(line, as_of=as_of))


if __name__ == "__main__":
    raise SystemExit(main())
