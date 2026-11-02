# Codex Harness

## Recommended Surface

Use CLI JSON for deterministic local workflows:

```bash
paper-verify path/to/paper.md --level L1 --json
paper-verify path/to/paper.md --level L2 --judge keyword --json
```

MCP can be added when the Codex environment supports it, but CLI JSON is the baseline.

## Skill Shape

- Put repository rules in `AGENTS.md`.
- Put reusable workflow instructions in a Codex skill.
- Gate PR readiness on `has_failure`, `overall_tier`, and targeted citation rows.
- Add tests when changing parser, scoring, or JSON schema behavior.

## Failure Policy

Codex should continue after individual fetch or judge failures, then report the failed citation rows. It should not infer correctness from the Markdown report alone.

## Repository development model

The checked-in `.codex/config.toml` selects `gpt-6-astra` with `ultra` reasoning
for repository development (including plan mode). This is separate from the
paper-verify application's `--judge` selection; the verifier remains provider-neutral.
The local Codex 0.153.4 model catalog lists Astra `ultra` as supported.

Open a Codex session rooted in this repository to load the project settings.
A running session or an explicit UI/CLI model override may take precedence;
editing this file does not prove that an existing conversation changed modes.
For an explicit CLI start, use:

```bash
codex -C /path/to/paper-verify -m gpt-6-astra -c 'model_reasoning_effort="ultra"'
```
