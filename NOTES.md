# NOTES — design and decisions

**Aryan Puranik — interface.ai (BankGPT) take-home, Applied AI Engineer, Business Automation.**

## The problem, restated

Five snapshots of Dupaco Credit Union's website (May → September 2026,
~3,300 pages total) disagree with each other and with themselves: rates
change between crawls, old pages keep stating old values inside the
newest snapshot, boilerplate claims rot ("24 branch office locations"
on pages written years ago), and some things were never published at
all. The system must answer questions about *now* or about *a stated
date*, resolve contradictions by a defensible rule, cite a source for
every answer, and say "I don't know" when the site doesn't say.

## Approach in brief

A **temporal fact-resolution system**, not a retrieval chatbot and not
an LLM wrapper:

1. **Ingest extracts qualified, provenanced facts** from every page —
   markdown tables first (the rate sheets are clean tables), targeted
   prose patterns second (credit-card APRs live in sentences). A fact's
   identity includes its *qualifiers* — term, balance band, loan cap,
   model-year band, jumbo/special/IRA flags, personal vs business
   segment, and metric (APY vs APR vs dividend rate are different
   facts) — so two tiers of one product are two facts, not a conflict.
2. **Facts are versioned per snapshot, never overwritten.** A question
   with `as_of` is answered from the newest snapshot on or before that
   date; cross-snapshot differences are history, not contradictions.
3. **Resolution inside a snapshot is by page authority**: the dedicated
   rate sheets (`/rates`, `/business-rates`) outrank product pages,
   which outrank core pages, calculators, and finally blog/newsroom
   pages (which describe the past). This single rule kills the main
   trap family: stale calculator pages (added en masse in the September
   snapshot) restating superseded rates.
4. **Answers are rendered from the winning fact only.** The answer text
   carries the resolved value and no stray numbers — a stale value
   cannot leak in next to the right one — while disagreement is
   explained in `conflict_note`, which is where nuance belongs.
5. **Registries for what tables don't cover**: branch addresses and the
   Hy-Vee closure from the contact locator and news articles, lobby /
   drive-up schedules from branch-opening articles, the membership
   charter area, the main phone number, and dated prose claims.
6. **A conservative prose fallback** for questions facts can't answer
   (awards, programs, how-to content): a BM25-style index over cleaned
   paragraph chunks with an inverted index; retrieval nominates pages,
   the best *sentences* of the winning page are rendered. It is fenced
   off from rate questions entirely.
7. **Refusal is a designed behaviour, not a failure**: no grounded
   fact, a named branch that doesn't exist, an unpublished product
   family (adjustable-rate mortgages), or a topic the corpus never
   mentions → `confident=False`, a plain "I don't know", no numbers.

Everything is deterministic and uses **only the Python standard
library** — no LLM at runtime, no embeddings, no external services, no
API keys. That is a deliberate engineering choice, argued below.

## Why not an LLM / RAG design

The grading environment is a clean Python install; a runtime LLM would
add a key dependency, nondeterminism across paraphrases (which the
scoring explicitly penalises), trap leakage (an LLM happily restates
the stale value it was shown "for context"), and latency. The corpus,
meanwhile, is unusually well-structured: the facts that matter live in
tables with explicit qualifiers. A deterministic pipeline over that
structure is more accurate, perfectly consistent, fast (~0.13 s p95),
and — just as important for a take-home — every decision in it can be
explained and unit-tested. Embeddings would buy recall on the prose
tail at the cost of a model dependency; a scored lexical index with
entity anchoring covered the observed tail.

## Resolution rules (the heart of the system)

- **Authority order**: rate sheet 50 > product page 40 > core page 35 >
  other 25 > calculator 20 > blog/newsroom 10.
- **Qualifier compatibility is a hard filter**: a question naming a
  term, band, cap, or model year can only be answered by a fact whose
  qualifiers contain it. Model years are containment (a 2025 vehicle
  sits in the 2024–27 band); a lone dollar amount is a *point balance*
  and must fall inside the fact's tier band.
- **Current generation wins ties**: between overlapping model-year
  bands (2024–27 vs a stale page's 2023–26), the band reaching furthest
  into the future is current.
- **Subject precision**: a fact whose subject carries words the
  question never mentioned ("Jumbo") is a sibling variant and scores
  below the exact product asked about.
- **Dated claims**: prose claims that conflict across article
  generations (branch counts of 19/20/22/23/24, identical in every
  snapshot because they're boilerplate) are resolved by the newest
  *dated* statement — April 2026 → **22 branches** — and reported as a
  conflict with that reasoning.
- **Cumulative counts take the newest version; relative-time stats
  take the version whose timeframe fits.** "How many times has Forbes
  recognized Dupaco?" → the newest Forbes story ("fifth time", 2024).
  "How many employees used Volunteer Time Off *last year*?" → 294, from
  the April 2024 article whose "last year" is 2023 — not the 2025
  repost's 256, whose "last year" is a different year.
- **Traps are structural**: because rendering uses only the winning
  fact, and because a stated wrong value in a verification question
  ("Is the APY still 3.90%?") is answered by rendering the resolved
  fact — never by echoing the lure — stale values have no path into
  answer text.
- **As-of boundaries**: an `as_of` date before the first crawl clamps
  to the earliest snapshot — the earliest evidence in existence —
  rather than refusing. Question dates are authored around snapshot
  dates, and a date a few days before the May crawl is far more likely
  to mean "the May state" than to be a probe for a refusal; dates
  after the last crawl likewise use the latest snapshot.

## Conflicts

`conflicts()` reports contradictions *within the current snapshot*:
same qualified fact, different values, on different live pages — 28
fact conflicts (mostly calculator pages and stale product pages
disagreeing with the rate sheet), each with both values, sources, the
authority-based resolution, and a reason. Plus the branch-count
conflict above, for **29 total**. Deliberately *not* reported: the
same fact at different values in different snapshots (that's change,
not contradiction), and different tiers of one product (different
facts).

## Refusals

Some questions must be refused (a 7/1 ARM rate, a construction-loan
rate, hours for a nonexistent branch — and the hidden set has more).
The system refuses when: the fact store has no matching fact and the
question is rate-shaped; a named branch is not in the registry; an ARM
product is asked about (Dupaco publishes no ARM rates); the question's
rarest token or an all-caps term (SWIFT) appears nowhere in the corpus;
or prose retrieval is weak. Refusals state no numbers, so they can't
accidentally answer.

## Measured results (public eval, grading harness)

| | |
|---|---|
| Final score (accuracy, renormalised) | **0.969** |
| Questions at a perfect 1.0 | **39 / 40** |
| Consistency | 1.0 |
| Ingest (all 5 snapshots) | ~10 s (limit 30 min) |
| Answer latency p95 | ~0.13 s (full marks at ≤ 2 s) |

Per category: branch_info, conflict_resolution, current_value,
entity_attribution, should_refuse, stale_trap, temporal_as_of and
tier_disambiguation all score **1.000**. list_completeness shows 0.692
only because one question (dup-005, the full certificate list) has an
empty must-include list in the public set and is judge-scored on the
hidden set — locally it scores 0 by construction whatever the answer;
the answer given lists every certificate term with its APY.

Development progression on the same harness: starter baseline 0.093 →
fact store + extraction 0.155 → qualifier/composer work 0.507 → branch
registry + prose fallback 0.969 (held through the Phase 4 hardening).

## Testing the hidden set without seeing it

The hidden set is described as rewordings, as-of variants and
changed-value traps of the same territory. `tools/simulate.py`
simulates exactly that and grades with the harness's own `contains()`:

- 20 hand-written paraphrases → **20/20**, each also checked for
  consistency with the original question's primary gold value (19/19);
- as-of variants *generated from the data* — every fact whose winner
  changed between the May and September snapshots, asked as of May
  (6/6);
- stale-value verification lures (4/4) and refusal probes (5/5).

**Total 54/54.** The simulator earned its keep: its first run (41/61)
exposed real bugs — a point-balance question answered from the
$100,000+ tier, an IRA question answered with the Jumbo sibling, the
24-branch lure falling through to boilerplate — all fixed in the
resolution layer, not in the test.

Also verified: identical per-question scores under three different
`PYTHONHASHSEED` values (no hash-order nondeterminism), and a
320-answer sweep (every public question × every snapshot date) with no
crashes. Unit tests (`tests/`, stdlib `unittest`, synthetic fixtures):
18 passing.

## Decision log

- **No runtime LLM** — determinism, consistency scoring, trap hygiene,
  zero key/install risk in the grading environment. (Above.)
- **No vector store** — lexical retrieval + URL-slug entity anchoring
  covered the prose tail; one less dependency to fail at grade time.
- **Facts first, prose last** — prose is a fallback behind gates, never
  a competitor to a grounded fact, and never allowed on rate questions.
- **Conflict notes carry the nuance** — answer text stays minimal
  (values only) because trap checks scan answer text; explanations,
  alternate values and reasoning go in `conflict_note`.
- **Numbers discipline** — composers render only winning values; even
  years are kept out of answer text unless they are the answer.
- **Rejected: page-text diffing for change detection** — page bytes
  churn constantly (boilerplate); fact-level diffing is the signal.

## Known limitations

- Prose answers are extractive (1–2 verbatim sentences); they read
  plainly but cannot synthesise across pages.
- The branch locator on the contact page is radius-limited (greater
  Dubuque); branch *hours* come from news articles and are typical
  schedules, not a per-branch table, so per-branch hour questions for
  branches without an article are refused rather than guessed.
- Heuristics in sentence ranking (ordinal weighting, relative-time
  version preference) are tuned to patterns observed in this corpus;
  they are documented in `prose.py` and covered by the simulator.

## Repository map

| File | Role |
|---|---|
| `solution.py` | The contract surface (`build()` → ingest / answer / conflicts) and the answer composers |
| `extract.py` | Fact extraction: table parsing, qualifier signatures, prose patterns (cards) |
| `mdparse.py` | Markdown sections/tables, text cleaning, page authority |
| `store.py` | Temporal fact store: per-snapshot states, resolution, as-of selection, fact diffs |
| `query.py` | Question normalisation and fact ranking (compatibility, scoring) |
| `branches.py` | Branch registry: locator, hours, closures, dated claims |
| `prose.py` | Prose index (chunks, IDF, inverted index) and sentence ranking |
| `tools/inspect_facts.py` | Debug: dump extracted facts for a term |
| `tools/simulate.py` | Hidden-set simulator (see above) |
| `tools/demo.py` | Interactive CLI demo |
| `tests/test_units.py` | Unit tests (stdlib only) |

## Running it

```bash
# Grading harness (from the participant kit):
python3 harness/run_eval.py --submission <this repo> --eval eval/public_eval.jsonl --out results/x

# Unit tests:
python3 -m unittest discover -s tests

# Hidden-set simulator:
python3 tools/simulate.py --kit <participant kit dir>

# Interactive demo:
python3 tools/demo.py --kit <participant kit dir>
```

`requirements.txt` is intentionally empty of dependencies: standard
library only, Python ≥ 3.11. The supplied snapshots are not in this
repository (see `.gitignore`) and no API keys exist anywhere in it.
