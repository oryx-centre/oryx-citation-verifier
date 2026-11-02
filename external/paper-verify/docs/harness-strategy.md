# Harness Strategy

paper-verify keeps one core verification pipeline and separates frontend-specific behavior into harness profiles. This avoids long-lived provider branches while still giving Claude Code, Cursor, Codex, Gemini, OpenCode/OMO, OMC, OMX, and Gajae Code their own setup path.

## Principle

- Core stays shared: extraction, fetching, judging, scoring, Markdown reports, JSON output, and MCP output are the same for every frontend.
- Harness profiles vary by frontend: configuration files, command surface, skill wording, default judge choice, and failure handling differ.
- Branches are temporary delivery vehicles: use branches to introduce or test a profile, then merge back to the main code line.

## Profile Matrix

| Profile | Best surface | Skill/config anchor | Default judge path | Use when |
|---|---|---|---|---|
| Claude Code | MCP stdio | `CLAUDE.md`, `.claude/agents`, MCP config | `anthropic`, `keyword` fallback | Tool-calling loop inside Claude Code |
| Cursor | CLI JSON | `.cursor/rules` / workspace rules | `openai`, `keyword` fallback | Current-file or editor-driven verification |
| Codex | CLI JSON, optional MCP | `AGENTS.md`, Codex skills | `keyword`, `openai`, `cli:codex` | Repo review, tests, PR preparation |
| Gemini | CLI judge / JSON | prompt contract, `GEMINI.md` | `gemini`, `cli:gemini`, `keyword` | Long-context second opinion or cross-check |
| OpenCode / OMO | CLI JSON events | `AGENTS.md`, `.opencode/skills`, `.omo` | `cli:opencode`, `cli:omo`, `keyword` | Tool-enabled multi-model review |
| OMC | CLI provider advisor | `AGENTS.md`, `.omc`, `CLAUDE.md` | `cli:omc`, `keyword` | Provider advisor cross-check |
| OMX | Codex JSONL | `AGENTS.md`, `.omx`, skills | `cli:omx`, `cli:codex`, `keyword` | Codex-compatible read-only review |
| Gajae Code | MCP / control plane | `AGENTS.md`, `.gjc`, skills | `cli:codex`, `cli:claude`, `keyword` | Structured receipt-oriented review |

## Recommended Branch Policy

Use feature branches for profile development only:

```bash
git switch -c codex/paper-verify-harness-profiles
```

Do not keep permanent provider branches such as `claude-code`, `cursor`, `codex`, and `gemini`. They will drift from the verifier core. Instead, keep provider-specific differences in:

- `paperverify/harness/`
- `docs/providers/`
- frontend config examples

## Gating Contract

Agents should read structured output instead of prose:

- `has_failure`: document has at least one tier-F citation.
- `overall_score`: average citation score.
- `overall_tier`: document tier.
- `tier_distribution`: counts by `A`, `B`, `C`, `F`.
- `citations[].breakdown`: score details.

For automation, `L1` should be the cheap preflight. `L2` should be the default semantic check. `L3` should be explicit because it may be slower and more expensive.

> **⚠️ L1 caveat.** L1 scores **reachability only** (HTTP-alive → 100, unreachable → 0). It does **not** verify that the page content supports the claim, and it **cannot detect soft-404s** (pages returning HTTP 200 with error/placeholder content). Use **L2 / L3** for content verification.

## Read-only adapter contract

All CLI adapters set `PAPER_VERIFY_READ_ONLY=1` and
`PAPER_VERIFY_OUTPUT=verdict-reason`. Harnesses should honor these hints and
must not edit files, browse beyond the supplied source, or execute repository
commands during a judge call. JSON/JSONL event streams are reduced to model
text; tool logs and status events are never treated as evidence.

Every citation now exposes `review_flags` and `evidence_quality` in JSON:

- `weak`: mismatch/uncertain/inaccessible, disagreement, dead landing, soft-404,
  archive-only, or missing verified source.
- `review`: no hard failure but not a fully corroborated `A`-tier match.
- `strong`: only an `A`-tier `Match` with no hard flags.

Agents should gate on `review_flags`, not on the aggregate score alone.

