# Oryx AI — citation verification tool

Open-source citation-verification tooling from Oryx Centre for Strategic Futures (Noida, India, founded 2 October 2026).

The tool checks citations in AI-generated research: does the citation exist, does its title match, can it be found again, and does it support the claim made for it. Every output is a draft for human review — nothing is published, approved, or sent without a human gate.

This repository contains the verification code only. It does not contain the invite-only beta app, its allowlist, job data, or any credentials. API keys are read from environment variables only and are never committed.

## What it is based on

Working Paper v3.0 (DOI 10.5281/zenodo.23718472) and Paper 2 v2 (DOI 10.5281/zenodo.23731235). Study 2 audited 30 AI-generated research briefs, 194 citations: 50 verified (26%), 95 broken (49%), 17 title mismatches (9%), 1 with no URL, 31 unverifiable (16%).

## Running

See `public-config.example.json` for the configuration shape. Set the LLM base URL, model, and API key via environment variables (`ORYX_PUBLIC_LLM_BASE_URL`, `ORYX_PUBLIC_LLM_MODEL`, `ORYX_PUBLIC_LLM_API_KEY` for the public server; `ORYX_LLM_*` for the core tool). No key is included in this repository.

The scoring layer in `external/paper-verify` is third-party code by Duchan Jin, MIT-licensed, kept with its own licence and attribution.

## Licence

MIT — see `LICENSE`. Copyright (c) 2026 Amartya Kumar.

## Contact

Amartya Kumar, Founder, Oryx Centre for Strategic Futures — amartya@oryxai.in — https://oryxai.in
