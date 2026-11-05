# NOTES — design and decisions

*(Filled in as the build progresses; this file is the design document for
the submission.)*

## Approach in brief

A temporal fact-resolution system, not a retrieval chatbot:

1. **Ingest** extracts qualified, provenanced facts from every page
   (markdown tables first, targeted patterns second). A fact's identity
   includes its qualifiers — term, balance band, model-year band,
   jumbo/special flags, personal vs business, and metric (APY vs APR vs
   dividend rate) — so different tiers are different facts, not conflicts.
2. **Facts are versioned per snapshot**, never overwritten, so a question
   can be answered as of any date from the snapshot in force then.
3. **Resolution** inside a snapshot is by page authority: the dedicated
   rate sheets (`/rates`, `/business-rates`) first, product pages next,
   calculators after that, blog/newsroom last (they describe the past).
4. **Answers are rendered from the winning fact only** — the answer text
   contains the resolved value and no other numbers, so a stale value
   can never leak in. Disagreement is explained in `conflict_note`.
5. **No grounded fact → decline** (`confident=False`, no numbers stated).

## Constraints honoured

- Python standard library only; no API keys; no network calls; answers
  come only from the supplied snapshots.
- The supplied snapshots are *not* in this repo (see `.gitignore`).
