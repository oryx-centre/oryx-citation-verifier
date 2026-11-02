# Psychology live-corpus validation — 2026-09-08

Release target: **0.2.0**, JSON schema **7**. The previous local review was made
against `7bb7f43`; upstream `807e3f1` has now been merged and preserved.

## What was actually run

- Twenty distinct papers: four each from cognitive, counseling, clinical,
  learning/educational psychology, and neuroscience.
- **Round 1: 20/20** live CLI runs on unmodified local briefing documents.
- **Round 2: 20/20** live CLI runs on the same documents after source fallback fixes.
- **Replay: 20/20** CLI evidence imports using the round-2 results without network.
- The 20 document hashes were identical between rounds; each run wrote JSON,
  Markdown, claims JSONL and stderr logs.
- Available source text increased from **8/20 to 19/20**. One remaining missing
  abstract stayed Uncertain. A later direct Semantic Scholar probe returned 429;
  this does not establish why every earlier attempt failed.
- Full local suite: **489 passed**. Feature gate: **21 verified**, drift/lie/open 0.

A passing case checks process exit, nonempty citations, expected title/metadata,
keyword-scope disclosure, no heuristic `strong` evidence, abstention for empty
content, and output files. It is **not a measurement of semantic accuracy**.
Keyword judgements were used; live paid provider/CLI LLM judgements were not tested.

## Fixes driven by the real corpus

1. Crossref records missing an abstract try Europe PMC, then Semantic Scholar.
   Require DOI and normalized title identity; preserve Crossref bibliography.
2. Record `abstract_source` and `abstract_url` separately from metadata `source`.
3. L2 does not fill a missing abstract with all page navigation and related-paper
   text. L3 retains bounded visible-page extraction. PDF bytes remain unusable text.
4. Preserve mathematical inequality signs while stripping actual HTML/JATS tags.
5. Keyword-only high scores are always review evidence, never strong evidence.
6. JSON CLI adapters ignore tool, status, user and failed-result events; stderr
   and nonzero exits cannot provide a positive verdict.
7. Preserve upstream CLI profiles, schema-6 review signals and new tie-break evidence.

## Reproduction

The portable manifest contains public titles/DOIs only, not personal briefing files.
Use a source checkout with its development environment installed:

```bash
python -m pytest -q
python tools/feature_gate.py
python tools/live_corpus_check.py --manifest examples/psychology-live20.json --out dist/live20 --live
python tools/live_corpus_check.py --manifest examples/psychology-live20.json --out dist/live20-replay --replay dist/live20
```

The portable manifest uses title-plus-DOI text; the original local run used full
briefings. These inputs have different claim context and must not be presented
as identical semantic benchmarks. Network behavior and abstracts can change.
A fresh output directory is required, and a failure returns nonzero.

## Scope of the accompanying source review

The private collection held 83 unique papers, all with currently registered
Crossref DOIs, and two local PDFs. Two duplicate index rows explained the displayed
85-entry index. The two PDFs had 67 reference entries, 49 with DOI links; all 49
resolved in Crossref. Two substantial bibliography inconsistencies were confirmed:

- The cited G*Power title corresponds to the 2007 paper, while its DOI, year and
  pages identify the 2009 paper. See [2007](https://doi.org/10.3758/BF03193146)
  and [2009](https://doi.org/10.3758/BRM.41.4.1149).
- A Posner/Rothbart reference gives a different title/journal from the work identified
  by [its DOI](https://doi.org/10.1146/annurev.psych.58.110405.085516).

These were source-paper bibliography errors, not fabricated articles. The review
also identified a within-paper results/discussion inconsistency. DOI registration
alone does not verify claim support, methodology, or absence of retraction.
Full PDF/OCR ingestion, bibliography entries without identifiers, statistical
reanalysis, and broad clinical/research validity remain outside the automatic gate.

The full personal review and input/output evidence are retained locally. Neither
private briefings nor PDF text are distributed with this release.
