#!/usr/bin/env python3
"""Developer tool: load the snapshots and inspect extracted facts.

Usage (from the kit folder, or anywhere with --snapshots):
    python tools/inspect_facts.py [--snapshots PATH] [--grep TEXT] [--diff]

Prints per-snapshot fact counts and, with --grep, every fact whose
subject/context mentions TEXT. --diff prints facts whose winning value
changed between consecutive snapshots.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "kit" / "hackathon-knowledge-kit-participant" / "harness"))

from store import FactStore  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    kit = Path(__file__).resolve().parent.parent.parent / "kit" / "hackathon-knowledge-kit-participant"
    ap.add_argument("--snapshots", type=Path, default=kit / "snapshots")
    ap.add_argument("--grep", default="")
    ap.add_argument("--diff", action="store_true")
    args = ap.parse_args()

    from run_eval import load_snapshots  # the kit harness loader
    store = FactStore()
    states = [store.ingest(s) for s in load_snapshots(args.snapshots)]
    for st in states:
        kinds = {}
        for f in st.facts:
            kinds[f.page_kind] = kinds.get(f.page_kind, 0) + 1
        print(f"{st.snapshot_id} ({st.taken_on}): {len(st.facts)} facts, {len(st.winners())} resolved keys; by kind {kinds}")
    if args.grep:
        st = states[-1]
        hits = [f for f in st.facts if args.grep.lower() in (f.subject + " " + f.context).lower()]
        print(f"\n-- {len(hits)} facts matching {args.grep!r} in latest snapshot --")
        for f in sorted(hits, key=lambda x: (x.subject, x.metric)):
            print(f"  [{f.page_kind:10s} a={f.authority}] {f.subject} :: {f.metric} = {f.value.text} q={f.qualifiers} <- {f.url}")
    if args.diff:
        for a, b in zip(states, states[1:]):
            changed = store.changed_facts(a, b)
            print(f"\n-- {a.taken_on} -> {b.taken_on}: {len(changed)} winning facts changed --")
            for key, old, new in changed[:40]:
                print(f"  {key[0]} :: {key[1]}: {old} -> {new}")


if __name__ == "__main__":
    main()
