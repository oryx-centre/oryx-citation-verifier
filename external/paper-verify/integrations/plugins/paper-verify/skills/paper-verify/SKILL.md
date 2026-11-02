---
name: paper-verify
description: Use when the user asks to verify citations, audit source links, check blog/report/lecture references, or reduce a long bibliography to sources that need human review using the paper-verify CLI or MCP tools.
---

# Paper Verify

## Workflow

Run `paper-verify` as a source triage engine:

```bash
paper-verify <file> --level L1 --json
paper-verify <file> --level L2 --judge keyword --json
```

Use provider judges only when available and useful:

```bash
paper-verify <file> --level L2 --judge anthropic --judge openai --tiebreak gemini --json
```

For local harnesses, use the read-only machine-output adapters:

```bash
paper-verify <file> --level L2 \
  --judge cli:claude --judge cli:codex --judge cli:opencode \
  --tiebreak cli:omx --json
```

The adapters parse model text from JSON/JSONL events and ignore tool/status
logs. They set `PAPER_VERIFY_READ_ONLY=1`.

## Triage

Use JSON fields for decisions:

- `tier`: prioritize `F` and `C`.
- `consensus` / `effective_verdict`: prioritize `Mismatch`, `Uncertain`, and `Inaccessible`.
- `judgements`: prioritize judge disagreement.
- `fetched.soft_404_suspect`: prioritize reachable pages that look like dead pages.
- `fetched.landing_status`: prioritize non-2xx landing pages.
- `fetched.source`: prioritize `archive` and `none`.
- `review_flags`: machine-readable risk signals; any non-empty list requires
  review unless the agent can explicitly resolve the flag.
- `evidence_quality`: `strong`, `review`, or `weak`; never auto-approve `weak`.

Return:

```markdown
## Must Review
- source — reason

## Review If Important
- source — reason

## Probably Safe
- source — reason
```

Do not treat reachable URLs as proof that claims are supported. Do not invent replacement sources.

