# Dupaco Knowledge System — interface.ai take-home

A temporal fact-resolution system over five snapshots of Dupaco Credit
Union's website. It answers questions about the current site — or the
site *as of a past date* — resolves conflicting values by explicit
rules, cites a source for every answer, and says "I don't know" when
the site doesn't say.

**Design in one line:** extract qualified, provenanced facts from every
page; version them per snapshot; resolve by page authority; render
answers from the winning fact only; fall back to a fenced prose index
for non-fact questions; refuse when nothing is grounded.

- **Deterministic, standard library only** — no LLM at runtime, no
  embeddings, no API keys, nothing to install.
- **Public eval (grading harness): 0.969**, 39/40 questions perfect,
  consistency 1.0, p95 answer latency ~0.13 s, full ingest ~10 s.
- **Self-built hidden-set simulator: 54/54** (paraphrases, consistency,
  generated as-of variants, stale-value lures, refusal probes).
- **29 conflicts** reported with values, sources, resolution and reason.

## Files

`solution.py` is the contract surface (`build()` → `ingest` /
`answer` / `conflicts`). Supporting modules: `extract.py` (fact
extraction), `mdparse.py` (markdown + page authority), `store.py`
(temporal store), `query.py` (question → fact ranking),
`branches.py` (branch registry + dated claims), `prose.py` (prose
fallback). Tooling in `tools/` (simulator, CLI demo, fact inspector);
unit tests in `tests/`.

**Full design document: [NOTES.md](NOTES.md)** — architecture,
resolution rules, refusal policy, decision log, per-category results,
and limitations.

## Run

```bash
python3 -m unittest discover -s tests                 # unit tests
python3 tools/demo.py --kit <participant kit>         # interactive demo
python3 tools/simulate.py --kit <participant kit>     # hidden-set simulator
python3 harness/run_eval.py --submission . --eval eval/public_eval.jsonl --out results/x   # from the kit
```

The supplied website snapshots are interface.ai's data and are **not**
included in this repository.
