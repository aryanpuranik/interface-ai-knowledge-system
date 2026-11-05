#!/usr/bin/env python3
"""Hidden-set simulator.

The graded set is larger than the public one and leans on rewordings,
as-of variants, and changed-value traps. This tool stress-tests the
submission the same way, with cases the public eval never showed:

* paraphrases — hand-written rewordings of public questions, same gold;
* as-of variants — questions generated from facts whose winning value
  actually changed between snapshots (expectation = the as-of state's
  winner, trap = the other era's value);
* verification lures — "Is it still X?" baited with the stale value;
* refusal probes — topics the site does not publish;
* consistency — paraphrase answers must carry the same numbers as the
  original question's answer.

Checks reuse the grading harness's own `contains` so semantics match.

Usage:
    python3 tools/simulate.py --kit /path/to/hackathon-knowledge-kit-participant
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Case bank: paraphrases / lures / refusal probes (gold mirrors public set)
# ---------------------------------------------------------------------------

PARAPHRASES = [
    # (origin, question, must_include, must_not_include)
    ("dup-016", "I need a 60 month car loan for a 2025 model - what's the APR?",
     ["5.39%"], ["5.34%"]),
    ("dup-028", "What's the rate on a personal loan with a 36-month term?",
     ["10.49"], []),
    ("dup-006", "Back in May 2026, what was the APY for the 10-month jumbo certificate special?",
     ["3.90%"], ["4.00%"]),
    ("dup-005", "What APY does the 17-month certificate special pay right now?",
     ["4.10%"], []),
    ("dup-045", "I keep $15,000 in Share Savings - what dividend rate do I earn?",
     ["0.15"], []),
    ("dup-052", "What APY will my 1st Rate Checking earn on a $10,000 balance?",
     ["2.50%"], ["3%"]),
    ("dup-034", "What are the APR ranges on Dupaco's Visa cards?",
     ["12.50", "21.00"], ["2.90%"]),
    ("dup-068", "Where is the Galena branch located?",
     ["11375 Oldenburg", "61036"], []),
    ("dup-073", "How many branch offices does Dupaco operate?",
     ["22"], ["24 branch"]),
    ("dup-087", "How many times has Forbes named Dupaco a Best-In-State Credit Union?",
     ["fifth"], ["2021"]),
    ("dup-060", "I moved - how do I change the address on my account?",
     ["800-373-7600"], ["member number"]),
    ("dup-104", "Are lobby hours the same at every Dupaco branch?",
     ["12:30", "noon"], []),
    ("dup-066", "When does the drive-up open and close on weekdays?",
     ["8 a.m.", "6 p.m."], []),
    ("dup-031", "Tell me about the Home$tart program for first-time buyers.",
     ["15,000"], []),
    ("dup-101", "Do you offer student loans?",
     ["student loan"], ["Share Savings"]),
    ("dup-081", "Who is eligible to join Dupaco?",
     ["Iowa", "Illinois", "Wisconsin"], []),
    ("dup-082", "Can I become a member online?",
     ["join.dupaco.com"], []),
    ("dup-008", "Where can I see the current CD rates online?",
     ["dupaco.com/personal/savings-account"], []),
    ("dup-094", "Which mobile wallets work with Dupaco cards?",
     ["Apple Pay", "Google Pay", "Samsung Pay"], []),
    ("dup-088", "How many employees took Volunteer Time Off last year?",
     ["294"], []),
]

AS_OF = {"dup-006": "2026-05-20"}   # paraphrases asked as of a past snapshot

LURES = [
    ("Is the 60-month auto loan APR 5.34% for a 2025 vehicle?",
     ["5.39%"], ["5.34%"]),
    ("Is the 10-month jumbo certificate APY still 3.90%?",
     ["4.00%"], ["3.90%"]),
    ("Does Dupaco have 24 branch office locations?",
     ["22"], ["24 branch"]),
    ("Do any Share Savings tiers pay 3% APY?",
     ["0.20%"], []),
]

REFUSALS = [
    "What is the APR on a 5/1 ARM at Dupaco?",
    "What is Dupaco's SWIFT code for international wires?",
    "What are the lobby hours at the Springfield branch?",
    "What is Dupaco's stock ticker symbol?",
    "What is the exchange rate for euros at Dupaco today?",
]


def _numbers(text: str) -> set:
    import re
    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", required=True, help="participant kit directory")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args()

    kit = Path(args.kit)
    sys.path.insert(0, str(kit / "harness"))
    sys.path.insert(0, args.repo)
    from run_eval import load_snapshots          # noqa: E402
    from scoring import contains                 # noqa: E402
    import solution                              # noqa: E402

    ks = solution.build()
    for snap in load_snapshots(kit / "snapshots"):
        ks.ingest(snap)
    store = ks.store

    cases = []  # (family, label, ok, detail)

    def check(family, label, ans, must, must_not):
        text = ans.text or ""
        missed = [m for m in must if not contains(text, m)]
        traps = [t for t in must_not if contains(text, t)]
        ok = not missed and not traps
        detail = "" if ok else f"missed={missed} traps={traps} | {text[:130]}"
        cases.append((family, label, ok, detail))
        return text

    # -- paraphrases (+ consistency vs the original question's numbers) -----
    public = {json.loads(l)["id"]: json.loads(l)
              for l in (kit / "eval" / "public_eval.jsonl").read_text().splitlines()}
    for origin, q, must, must_not in PARAPHRASES:
        ans = ks.answer(q, as_of=AS_OF.get(origin))
        text = check("paraphrase", f"{origin}: {q[:52]}", ans, must, must_not)
        # Consistency: the paraphrase and the original must agree on the
        # original's primary gold value (full number-set equality is too
        # strict — a tier answer is a legitimate subset of a list answer).
        if origin in public and public[origin].get("must_include"):
            primary = public[origin]["must_include"][0]
            orig = ks.answer(public[origin]["question"],
                             as_of=public[origin].get("as_of"))
            same = contains(orig.text, primary) and contains(text, primary)
            cases.append(("consistency", f"{origin} primary value agrees",
                          same, "" if same else
                          f"primary={primary} orig={orig.text[:80]} para={text[:80]}"))

    # -- as-of variants generated from facts that actually changed ----------
    first, last = store.states[0], store.states[-1]
    old_winners = first.winners()
    generated, seen_q = 0, set()
    for key, old, new in store.changed_facts(first, last):
        _subj, metric, _qual = key
        if metric not in ("APY", "APR", "dividend rate") or generated >= 12:
            continue
        subj = old_winners[key].subject   # the fact's own readable label
        if "$" in subj or not (2 <= len(subj.split()) <= 8):
            continue
        q = f"What was the {metric} for the {subj} in May 2026?"
        if q in seen_q:
            continue
        seen_q.add(q)
        ans = ks.answer(q, as_of="2026-05-15")
        check("as-of", q[:64], ans, [old], [new])
        generated += 1

    # -- verification lures ---------------------------------------------------
    for q, must, must_not in LURES:
        check("lure", q[:64], ks.answer(q), must, must_not)

    # -- refusal probes ---------------------------------------------------------
    for q in REFUSALS:
        ans = ks.answer(q)
        text = (ans.text or "").lower()
        refused = (not ans.confident) or "don't know" in text or "not listed" in text
        cases.append(("refusal", q[:64], refused,
                      "" if refused else f"answered confidently: {ans.text[:130]}"))

    # -- report -------------------------------------------------------------------
    families: dict[str, list] = {}
    for fam, label, ok, detail in cases:
        families.setdefault(fam, []).append((label, ok, detail))
    total_ok = sum(1 for _, _, ok, _ in cases if ok)
    print(f"\nSIMULATION: {total_ok}/{len(cases)} passed")
    for fam, items in families.items():
        ok = sum(1 for _, o, _ in items if o)
        print(f"  {fam:<12} {ok}/{len(items)}")
        for label, o, detail in items:
            if not o:
                print(f"    FAIL {label}\n         {detail}")
    return 0 if total_ok == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
