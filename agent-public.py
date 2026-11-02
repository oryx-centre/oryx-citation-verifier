#!/usr/bin/env python3
"""Oryx AI — dedicated agent v0.4 (staged, auditable, human-gated).

PUBLIC VARIANT (agent-public.py) — a copy of agent.py v0.4 plus:
  * token-usage persistence (usage.json per run: per-stage calls +
    totals) for the spend cap and cost-per-report;
  * backend override from environment only (ORYX_PUBLIC_LLM_BASE_URL /
    ORYX_PUBLIC_LLM_MODEL / ORYX_PUBLIC_LLM_API_KEY); the key is never
    written to disk, logged, or printed.
DUPLICATION DEBT: this file duplicates agent.py. Before launch, port
the token logging + env backend back into agent.py and delete this
copy, so the product runs on one agent, not two. Until then, any fix
to agent.py must be mirrored here.

v0.4 adds three Elicit-inspired stages WITHOUT diluting the v0.3 audit
core (see elicit-teardown.md for what was adopted vs rejected):

  1 framing      -> sub-questions, scope, exclusions, false-premise check
  2 evidence     -> candidates: live retrieval, or operator-curated list
  3 screening    -> per-source include/exclude + one-line reason (logged)
  4 synthesis    -> model draft citing ONLY the included source list
  5 evidence tbl -> one structured row per included source (model)
  6 table verify -> MECHANICAL: each row's cells checked; failures marked
                    UNVERIFIED in place, never dropped or fixed
  7 verification -> mechanical: markers resolve? claim sentences cited?
  8 adversarial  -> separate model pass; FLAGS problems, never fixes them
  9 human gate   -> report stays DRAFT until `agent.py approve` is run
  + export-pdf   -> FINAL (post-approval) reports only, weasyprint PDF
                    with a page-1 Audit Certificate (mechanical figures
                    only, computed from the run's verification files)

Honesty rules (see agent-definition.md):
- Only mechanically computed metrics appear in a report. Any model-written
  "Metrics Box" is stripped before assembly.
- Citation precision is NOT mechanically computable; the metrics box says
  so, and the adversarial flags + human reviewer carry that judgement.
- A stage failure fails the run loudly (non-zero exit, stage log written).
  Nothing is ever faked to complete a run.
- No email, no publishing, no external sends. Model: gemini-3.1-flash-lite
  via the local OmniRoute server only (config holds no secrets).

Usage:
  python3 agent.py ask --question "..." [--sources-json sources.json]
  python3 agent.py approve --run-dir runs/<dir> --reviewer "Riya Nalwa"
  python3 agent.py export-pdf --run-dir runs/<dir> [--out report.pdf]
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "agent-config.json")
import sys as _sys
VENV_PY = os.path.expanduser("~/workspace/.venv-pdf/bin/python")
if not os.path.isfile(VENV_PY):
    VENV_PY = _sys.executable  # Render / any host without the local venv: use system python + requirements.txt


class AgentError(Exception):
    pass


def load_config():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    # Public-variant backend override: environment only. At launch the
    # billed backend (base URL / model / key) comes from the operator's
    # environment, never from a file. Unset in staging, where the local
    # config backend (local router) applies.
    backend = cfg.setdefault("backend", {})
    if os.environ.get("ORYX_PUBLIC_LLM_BASE_URL"):
        backend["base_url"] = os.environ["ORYX_PUBLIC_LLM_BASE_URL"]
        # Z.ai's completions path is /chat/completions under the v4 base
        # (api.z.ai/api/paas/v4), not the OmniRoute-style /v1/chat/completions.
        if "api.z.ai" in backend["base_url"]:
            backend["chat_endpoint"] = "/chat/completions"
    if os.environ.get("ORYX_PUBLIC_LLM_MODEL"):
        backend["model"] = os.environ["ORYX_PUBLIC_LLM_MODEL"]
    if os.environ.get("ORYX_PUBLIC_LLM_API_KEY"):
        backend["api_key"] = os.environ["ORYX_PUBLIC_LLM_API_KEY"]
    return cfg


def _chat_primary(cfg, system, user, max_tokens, stage=""):
    """One model call via the local OmniRoute server. Raises AgentError
    on any failure — the caller logs it and the run fails loudly.

    Transient provider overload (HTTP 429/503) is retried up to 3
    attempts with backoff, honouring the provider's retry_after when
    present (capped at 60s). Same model, same prompt, same parameters
    on every attempt — a retry never substitutes a model or changes
    the request. Added 25 Oct 2026 after a screening-stage 503
    ("high demand, reset after 12s") killed an otherwise healthy run.
    """
    backend = cfg["backend"]
    _body = {
        "model": backend["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": cfg["generation"]["temperature"],
        "max_tokens": max_tokens,
    }
    # GLM flash models: thinking disabled (study-2 lesson — thinking tokens
    # consumed the budget and left no room for output). Free tier, $0.
    if backend["model"].startswith("glm-"):
        _body["thinking"] = {"type": "disabled"}
    payload = json.dumps(_body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    # Billed-key wiring (public variant): the key comes from the
    # environment only (ORYX_PUBLIC_LLM_API_KEY), never from a file,
    # never logged, never printed. Absent in staging, where the local
    # router routes itself with no key header.
    api_key = backend.get("api_key")
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(
        backend["base_url"].rstrip("/") + backend["chat_endpoint"],
        data=payload, headers=headers)
    # Free-tier Z.ai backend: auth via the registered custom.zai credential
    # surrogate (same mechanism as custom.zai/bin/zai.py). No key is read,
    # stored, logged or printed by this code; the surrogate supplies the
    # header at request time. Used only when no env key is set.
    if not api_key and "api.z.ai" in backend["base_url"]:
        try:
            sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
            from dynamic_credentials import add_surrogate_to_request
            add_surrogate_to_request(req, "custom.zai", allowed_hosts=["api.z.ai"])
        except Exception as e:  # noqa: BLE001 - fail loudly, never unauthenticated
            raise AgentError("z.ai credential surrogate unavailable: %s" % e)
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(
                    req, timeout=cfg["generation"]["request_timeout_seconds"]
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            last_error = ("model call failed: HTTP %s: %s"
                          % (e.code, body[:300]))
            # 401 on the free tier is intermittent (surrogate/token flap):
            # retry once with a freshly minted surrogate, same request.
            if e.code == 401 and attempt < 2 and "api.z.ai" in backend["base_url"]:
                try:
                    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
                    from dynamic_credentials import add_surrogate_to_request as _surr
                    req = urllib.request.Request(
                        backend["base_url"].rstrip("/") + backend["chat_endpoint"],
                        data=payload, headers={"Content-Type": "application/json"})
                    _surr(req, "custom.zai", allowed_hosts=["api.z.ai"])
                    time.sleep(10)
                    continue
                except Exception:
                    pass
            if e.code in (429, 503) and attempt < 2:
                wait = 15
                try:
                    hint = json.loads(body).get("error", {}).get("retry_after")
                    if hint:
                        wait = min(int(hint), 60)
                except (ValueError, TypeError):
                    pass
                time.sleep(wait)
                continue
            raise AgentError(last_error)
        except Exception as e:  # noqa: BLE001 - fail loudly, with the cause
            # Transient connection drops (free-tier overload closing the
            # connection) are retried like 429/503: same model, same prompt,
            # same parameters — never a substitution. Anything else fails.
            if attempt < 2 and ("closed connection" in str(e) or
                                "timed out" in str(e).lower() or
                                "RemoteDisconnected" in type(e).__name__):
                time.sleep(20)
                continue
            raise AgentError("model call failed: %s" % e)
    else:
        raise AgentError(last_error or "model call failed")
    _record_usage(stage, backend["model"], data.get("usage"))
    text = data["choices"][0]["message"].get("content") or ""
    if not text.strip():
        raise AgentError("model call returned empty content")
    return text.strip()



# ---- Provider fallback (added 30 Oct 2026, MVP best-version work) ----
# When the primary backend fails on overload (429/503/connection drop),
# try operator-configured fallback backends in order, instead of failing
# the whole run. Rules, mirroring provider_fallback.py:
# - Fallbacks come ONLY from the environment variable
#   ORYX_PUBLIC_LLM_FALLBACKS, a JSON list of
#   {"base_url", "chat_endpoint", "model", "api_key_env"} objects.
#   Keys are never in a file: api_key_env names an environment variable.
# - A run is labelled with the provider that actually produced it, via
#   _record_usage (the usage.json model field shows the last provider).
# - Google AI Studio (googleapis) backends are SKIPPED unless
#   ORYX_PUBLIC_LLM_ALLOW_GOOGLE=1 is set, so client data can never
#   fall through to a free tier that may use content for training.
# - Non-overload failures are NOT retried on a fallback: a bad request
#   stays a loud failure. Fallbacks are for provider limits only.
# - This wrapper lives in agent-public.py only. agent.py (local,
#   OmniRoute-routed) does not need it; see the duplication-debt note
#   at the top of this file.

def _fallback_backends():
    raw = os.environ.get("ORYX_PUBLIC_LLM_FALLBACKS", "")
    if not raw:
        return []
    try:
        lst = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [b for b in lst if isinstance(b, dict)
            and b.get("base_url") and b.get("model")]


def _fallback_call(cfg, backend, system, user, max_tokens, stage):
    body = {
        "model": backend["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": cfg["generation"]["temperature"],
        "max_tokens": max_tokens,
    }
    if str(backend["model"]).startswith("glm-"):
        body["thinking"] = {"type": "disabled"}
    payload = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key_env = backend.get("api_key_env")
    if key_env and os.environ.get(key_env):
        headers["Authorization"] = "Bearer " + os.environ[key_env]
    endpoint = backend.get("chat_endpoint", "/v1/chat/completions")
    req = urllib.request.Request(
        backend["base_url"].rstrip("/") + endpoint,
        data=payload, headers=headers)
    last_error = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(
                    req, timeout=cfg["generation"]["request_timeout_seconds"]
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode("utf-8", "replace")
            last_error = "fallback %s failed: HTTP %s: %s" % (
                backend["model"], e.code, body_txt[:300])
            if e.code in (429, 503) and attempt < 1:
                time.sleep(15)
                continue
            raise AgentError(last_error)
        except Exception as e:  # noqa: BLE001 - transient drops retry once
            last_error = "fallback %s failed: %s" % (backend["model"], e)
            if attempt < 1 and ("closed connection" in str(e)
                                or "timed out" in str(e).lower()
                                or "RemoteDisconnected" in type(e).__name__):
                time.sleep(20)
                continue
            raise AgentError(last_error)
    else:
        raise AgentError(last_error or "fallback call failed")
    _record_usage(stage, backend["model"], data.get("usage"))
    text = data["choices"][0]["message"].get("content") or ""
    if not text.strip():
        raise AgentError("fallback %s returned empty content" % backend["model"])
    return text.strip()


def chat(cfg, system, user, max_tokens, stage=""):
    """Primary backend first; on provider overload only, fall back.

    Identical signature to the original chat(). Every other caller is
    unchanged. A fallback result is labelled by its provider in
    usage.json, so a report never silently presents one provider's
    output as another's.
    """
    try:
        return _chat_primary(cfg, system, user, max_tokens, stage)
    except AgentError as e:
        msg = str(e)
        if not any(k in msg for k in ("429", "503", "closed connection",
                                      "timed out", "RemoteDisconnected")):
            raise
        for backend in _fallback_backends():
            if "googleapis" in backend["base_url"] and \
                    os.environ.get("ORYX_PUBLIC_LLM_ALLOW_GOOGLE") != "1":
                continue
            try:
                return _fallback_call(cfg, backend, system, user,
                                      max_tokens, stage)
            except AgentError:
                continue
        raise


def slugify(text, max_len=40):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len] or "question"


def log_line(path, line):
    with open(path, "a") as f:
        f.write(line + "\n")


# ---- Token-usage persistence (PUBLIC VARIANT ONLY — not in agent.py) --
# agent.py v0.4 discards the model response's usage field. This copy
# records every successful model call so cost can be computed per job
# (spend cap + cost-per-report). Failed calls are not recorded: a
# 429/503 returns no completion and is not billable usage on the
# backends this agent targets; if a future backend bills failed calls,
# this must be revisited.
_USAGE_CALLS = []
_USAGE_RUN_DIR = None


def _set_usage_run_dir(run_dir):
    global _USAGE_RUN_DIR
    _USAGE_RUN_DIR = run_dir
    _USAGE_CALLS.clear()


def _record_usage(stage, model, usage):
    """Append one model call's token usage; persist usage.json at once
    so a later stage failure still leaves partial usage on disk."""
    rec = {
        "stage": stage,
        "model": model,
        "prompt_tokens": (usage or {}).get("prompt_tokens"),
        "completion_tokens": (usage or {}).get("completion_tokens"),
        "total_tokens": (usage or {}).get("total_tokens"),
        "reported": bool(usage),
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    _USAGE_CALLS.append(rec)
    if _USAGE_RUN_DIR:
        _write_usage_file(_USAGE_RUN_DIR)


def _usage_totals():
    pin = sum(c["prompt_tokens"] for c in _USAGE_CALLS
              if isinstance(c["prompt_tokens"], int))
    cout = sum(c["completion_tokens"] for c in _USAGE_CALLS
               if isinstance(c["completion_tokens"], int))
    return {
        "calls": len(_USAGE_CALLS),
        "calls_with_usage": sum(1 for c in _USAGE_CALLS if c["reported"]),
        "prompt_tokens": pin,
        "completion_tokens": cout,
        "total_tokens": pin + cout,
    }


def _write_usage_file(run_dir):
    data = {"model": None, "calls": _USAGE_CALLS,
            "totals": _usage_totals()}
    if _USAGE_CALLS:
        data["model"] = _USAGE_CALLS[-1]["model"]
    tmp = os.path.join(run_dir, "usage.json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, os.path.join(run_dir, "usage.json"))


def strip_metrics_box(text):
    """Remove any model-written metrics section. The mechanical metrics
    box in the report is the only authoritative one."""
    m = re.search(r"^#{1,4}\s*Metrics Box\s*$", text, re.MULTILINE)
    if m:
        return (text[:m.start()].rstrip()
                + "\n\n*(Model-written metrics removed — the mechanical "
                "metrics box below is authoritative.)*\n")
    return text


def split_claim_sentences(text):
    """Strict mechanical rule, stated in every report: every sentence in
    the synthesis body counts as a claim sentence, except headings and
    the metrics-removal note. A sentence is 'cited' if it contains at
    least one valid [Sn] marker."""
    sentences = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("*(") and "metrics removed" in line:
            continue
        for s in re.split(r"(?<=[.!?])\s+", line):
            s = s.strip().strip("-* ").strip()
            if s:
                sentences.append(s)
    return sentences


def verify_synthesis(synthesis, n_sources):
    markers = [int(m) for m in re.findall(r"\[S(\d+)\]", synthesis)]
    invalid = sorted({m for m in markers if m < 1 or m > n_sources})
    sentences = split_claim_sentences(synthesis)
    cited, uncited = [], []
    for s in sentences:
        ms = [int(m) for m in re.findall(r"\[S(\d+)\]", s)]
        valid = [m for m in ms if 1 <= m <= n_sources]
        (cited if valid else uncited).append(s)
    rate = (round(100 * len(cited) / len(sentences)) if sentences else None)
    status = "PASS"
    if invalid or not markers or not sentences:
        status = "FAIL"
    return {
        "markers_found": len(markers),
        "sources_cited": len(sorted(set(markers))),
        "invalid_markers": invalid,
        "claim_sentences": len(sentences),
        "cited_claim_sentences": len(cited),
        "sourced_claim_rate_pct": rate,
        "uncited_claim_sentences": uncited[:100],
        "uncited_truncated": len(uncited) > 100,
        "status": status,
    }


def parse_evidence_table(text, n_sources):
    """Parse the model's pipe table leniently (whitespace, extra pipes
    folded into the last cell). Returns a list of rows, each a list of
    5 cell strings. Raises AgentError if the row count does not match
    the included source count — the raw output is preserved in the run
    directory, and the run fails loudly instead of faking a table."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or all(not c for c in cells):
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue  # separator row
        if cells[0].lower() == "source":
            continue  # header row
        if len(cells) < 5:
            raise AgentError(
                "evidence table row has %d cells, expected 5: %r"
                % (len(cells), line[:120]))
        if len(cells) > 5:
            cells = cells[:4] + [" | ".join(cells[4:])]
        rows.append(cells)
    if len(rows) != n_sources:
        raise AgentError(
            "evidence table has %d rows, expected %d (one per included "
            "source); raw output preserved in 05-evidence-table.md"
            % (len(rows), n_sources))
    return rows


def verify_evidence_table(rows):
    """Mechanical per-row check. Row i must cite its own source: its
    cells must contain [S(i+1)]. Its evidence-pointer cell (column 3)
    must be non-empty and not a bare N/A. Failures are marked
    UNVERIFIED — never dropped, never fixed."""
    out = []
    for i, cells in enumerate(rows):
        expected = i + 1
        markers = [int(m) for m in re.findall(r"\[S(\d+)\]",
                                              " ".join(cells))]
        pointer = cells[2].strip()
        pointer_ok = bool(pointer) and pointer.lower() not in (
            "n/a", "na", "none", "-")
        marker_ok = expected in markers
        status = "VERIFIED" if (marker_ok and pointer_ok) else "UNVERIFIED"
        out.append({
            "row": i + 1,
            "source_index": expected,
            "markers_found": sorted(set(markers)),
            "marker_points_at_own_source": marker_ok,
            "evidence_pointer_present": pointer_ok,
            "status": status,
        })
    return out


FRAMING_SYSTEM = """You are Oryx AI, an auditable research agent, at the question-framing
stage. Decompose the research question. Output Markdown with exactly
these sections: ## Sub-questions, ## Scope, ## Exclusions,
## False-premise check. Rules: never invent facts; if the question rests
on a false premise, say so plainly and reframe it; state what would count
as adequate evidence for each sub-question. Do not write a metrics box."""

SCREENING_SYSTEM = """You are Oryx AI, an auditable research agent, at the source-screening
stage. Given the research question and the candidate source list, judge
each source's relevance. Output EXACTLY one line per source, in order,
in this format and no other text:
[S#] | High/Medium/Low | <one-line reason>
Rules: judge from the title and abstract only; never invent facts about
a source; if the abstract is thin, say so and rate Low. Do not write a
metrics box."""

SYNTHESIS_SYSTEM = """You are Oryx AI, an auditable research agent. Rules:
1. Every material claim carries a source marker [Sn] from the source list.
2. Never cite a source that is not in the list. Never invent statistics,
   people, organisations, dates, or quotations.
3. State confidence per section (High/Medium/Low) and why.
4. State what the evidence does NOT show. 'Evidence insufficient' is valid.
5. Separate facts from inference from recommendation.
6. Start with '## Executive answer' (5 lines max), then one section per
   sub-question, then '## What the evidence does NOT show'.
7. Do NOT write a metrics box — metrics are computed mechanically."""

EVIDENCE_TABLE_SYSTEM = """You are Oryx AI, an auditable research agent, at the evidence-table
stage. Build ONE Markdown table row per source, in source order, with
EXACTLY these columns and no other text before or after the table:
| Source | Key finding | Evidence pointer | Limitation / gap | Relevance |
|---|---|---|---|---|
| [S1] Short title | ... | ... | ... | ... |
Rules:
- The Source cell is exactly '[Sn] Short title' for that row's source.
- The Key finding cell states the source's main finding for this
  question, and MUST carry that row's [Sn] marker.
- The Evidence pointer cell quotes or closely paraphrases the supporting
  text from the abstract, with the [Sn] marker; never invent a quotation.
- The Limitation / gap cell states what this source does NOT establish,
  or its stated limitation. Do not leave it empty.
- The Relevance cell is one line on how this source bears on the question.
- Do NOT use the '|' character inside any cell. Do NOT add a summary or
  totals row. Do NOT write a metrics box."""

ADVERSARIAL_SYSTEM = """You are an adversarial reviewer of an AI-generated research draft.
Your job is to break it, not to praise it. Given the draft and its source
list, output a numbered list of flags, one per line, in three groups:
(a) claims whose cited source does NOT support them, (b) material claims
with NO citation, (c) overstatements, hedged claims presented as facts,
or numbers/dates/quotations not traceable to a cited source. Quote the
offending claim briefly with each flag. Do NOT rewrite the draft, do NOT
fix anything, do NOT add new claims. If a group is empty, write 'None.'
Do not write a metrics box."""


def screen_sources(cfg, question, candidates, curated, run_log):
    """Returns (included, log_text). Mechanical screens apply in both
    modes (no title / no evidence text / duplicate title). The model's
    relevance rating excludes ONLY in live mode; in curated mode the
    operator's decision stands and relevance is annotation only.
    Unparsed model output never excludes anything (fail-safe to
    inclusion)."""
    gen = cfg["generation"]
    src_text = "\n".join(
        "[S%d] %s (%s). %s. %s" % (i + 1, s.get("title", "Untitled"),
                                   s.get("year"), s.get("authors", ""),
                                   s.get("abstract", ""))
        for i, s in enumerate(candidates))
    raw = chat(cfg, SCREENING_SYSTEM,
               "Research question: %s\n\nCandidate sources:\n%s"
               % (question, src_text),
               gen.get("max_tokens_screening", 1000), stage="screening")
    ratings = {}
    for line in raw.splitlines():
        m = re.match(r"\[S(\d+)\]\s*\|\s*(High|Medium|Low)\s*\|\s*(.+)",
                     line.strip())
        if m:
            ratings[int(m.group(1))] = (m.group(2), m.group(3).strip())
    seen_titles = set()
    included, log_lines = [], []
    for i, s in enumerate(candidates):
        idx = i + 1
        title = (s.get("title") or "").strip()
        abstract = (s.get("abstract") or "").strip()
        norm = title.lower()
        rel, reason = ratings.get(idx, ("unparsed", "model output did not "
                                                 "parse for this row"))
        if not title:
            decision, why = "EXCLUDED", "mechanical: no title — cannot cite"
        elif not abstract:
            decision, why = ("EXCLUDED", "mechanical: no evidence text "
                                       "(no abstract)")
        elif norm in seen_titles:
            decision, why = ("EXCLUDED", "mechanical: duplicate title")
        elif not curated and rel == "Low":
            decision, why = ("EXCLUDED", "model relevance Low: %s" % reason)
        elif curated:
            decision, why = ("INCLUDED", "operator-curated; model relevance "
                                       "%s: %s" % (rel, reason))
        else:
            decision, why = ("INCLUDED", "model relevance %s: %s"
                                       % (rel, reason))
        seen_titles.add(norm)
        log_lines.append("[S%d] %s — %s" % (idx, decision, why))
        if decision == "INCLUDED":
            included.append(s)
    log_text = ("# Screening log\n\nMode: %s\n\n" % (
        "operator-curated (operator decision stands; model relevance is "
        "annotation only)" if curated else
        "live retrieval (model relevance Low excludes)"))
    log_text += "\n".join(log_lines)
    log_text += ("\n\n## Raw model screening output\n\n%s\n" % raw)
    log_text += ("\nCandidates: %d | Included: %d | Excluded: %d\n"
                 % (len(candidates), len(included),
                    len(candidates) - len(included)))
    log_line(run_log, "screening: %d candidates -> %d included, %d "
             "excluded" % (len(candidates), len(included),
                           len(candidates) - len(included)))
    return included, log_text


def render_verified_table(rows, table_ver):
    """Render the evidence table with a Verification column appended.
    Pipe characters inside cells are replaced for table formatting; the
    raw model output in 05-evidence-table.md is preserved verbatim."""
    def clean(c):
        return c.replace("|", "/").strip()
    out = ["| Source | Key finding | Evidence pointer | Limitation / "
           "gap | Relevance | Verification |",
           "|---|---|---|---|---|---|"]
    for cells, v in zip(rows, table_ver):
        out.append("| %s | **%s** |" % (
            " | ".join(clean(c) for c in cells), v["status"]))
    return "\n".join(out)


PDF_CSS = """
@page { size: A4; margin: 20mm 19mm 17mm 19mm;
  @bottom-center { content: "Oryx AI — Auditable Research Report  |  page " counter(page) " of " counter(pages); font-family: Helvetica, Arial, sans-serif; font-size: 8pt; color: #52607A; } }
body { font-family: Georgia, 'Noto Serif', serif; font-size: 10.5pt; line-height: 1.55; color: #16233C; }
h1, h2, h3 { font-family: Helvetica, Arial, 'Noto Sans', sans-serif; color: #0B2239; }
h1 { font-size: 26pt; line-height: 1.2; border-bottom: 3px solid #C9A227; padding-bottom: 10px; }
h2 { font-size: 15pt; border-bottom: 1px solid #C9A227; padding-bottom: 4px; margin-top: 22px; }
h3 { font-size: 12pt; }
.cover-meta { font-family: Helvetica, Arial, sans-serif; font-size: 10pt; color: #52607A; border: 1px solid #C9A227; padding: 12px 16px; margin: 18px 0; }
table { border-collapse: collapse; width: 100%; font-family: Helvetica, Arial, sans-serif; font-size: 8.5pt; }
th { background: #0B2239; color: #fff; padding: 6px; text-align: left; }
td { border-bottom: 1px solid #ccc; padding: 6px; vertical-align: top; }
tr:nth-child(even) td { background: #F2F0EA; }
blockquote { border-left: 3px solid #C9A227; margin-left: 0; padding-left: 12px; color: #52607A; }
code { font-size: 9pt; }
"""


def cmd_ask(args):
    cfg = load_config()
    gen = cfg["generation"]
    question = args.question.strip()
    if not question:
        raise AgentError("empty question")
    if len(question) > cfg["limits"]["max_question_chars"]:
        raise AgentError("question exceeds %d characters"
                         % cfg["limits"]["max_question_chars"])
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = args.run_dir or os.path.join(
        HERE, "runs", "%s-%s" % (slugify(question), stamp))
    os.makedirs(run_dir, exist_ok=True)
    run_log = os.path.join(run_dir, "run-log.md")
    _set_usage_run_dir(run_dir)
    log_line(run_log, "# Oryx AI agent run (v0.4) — %s" % stamp)
    log_line(run_log, "Question: %s" % question)
    log_line(run_log, "Model: %s (via %s)"
             % (cfg["backend"]["model"], cfg["backend"]["base_url"]))

    # ---- Stage 1: framing ---------------------------------------------
    log_line(run_log, "\n## Stage 1: framing")
    framing = chat(cfg, FRAMING_SYSTEM,
                   "Research question: %s" % question,
                   gen["max_tokens_framing"], stage="framing")
    with open(os.path.join(run_dir, "01-framing.md"), "w") as f:
        f.write(framing + "\n")
    log_line(run_log, "framing written (chars: %d)" % len(framing))

    # ---- Stage 2: evidence acquisition (candidates) --------------------
    log_line(run_log, "\n## Stage 2: evidence acquisition")
    curated = bool(args.sources_json)
    if curated:
        with open(args.sources_json) as f:
            candidates = json.load(f)
        note = ("Sources operator-curated from verified web search results; "
                "live API retrieval bypassed (shared-IP rate limits).")
    else:
        sys.path.insert(0, HERE)
        from oryx_ai import retrieve  # v0.2 retrieval, reused not rewritten
        candidates, note = retrieve(question, args.limit)
        if not candidates:
            log_line(run_log, "FAIL: no sources retrieved. " + note)
            raise AgentError("no sources retrieved. " + note)
    if len(candidates) > cfg["limits"]["max_sources_per_run"]:
        candidates = candidates[:cfg["limits"]["max_sources_per_run"]]
        note += " Truncated to %d candidates (config limit)." % len(candidates)
    with open(os.path.join(run_dir, "02-candidates.json"), "w") as f:
        json.dump(candidates, f, indent=2)
    log_line(run_log, "candidates: %d. %s" % (len(candidates), note))

    # ---- Stage 3: screening --------------------------------------------
    log_line(run_log, "\n## Stage 3: screening")
    sources, screening_log = screen_sources(
        cfg, question, candidates, curated, run_log)
    with open(os.path.join(run_dir, "03-screening-log.md"), "w") as f:
        f.write(screening_log)
    if len(sources) < cfg["limits"]["min_sources_per_run"]:
        raise AgentError("no sources survived screening (%d candidates)"
                         % len(candidates))
    with open(os.path.join(run_dir, "02-sources.json"), "w") as f:
        json.dump(sources, f, indent=2)
    log_line(run_log, "included sources: %d" % len(sources))

    src_text = "\n".join(
        "[S%d] %s (%s). %s. %s" % (i + 1, s.get("title", "Untitled"),
                                   s.get("year"), s.get("authors", ""),
                                   s.get("abstract", ""))
        for i, s in enumerate(sources))

    # ---- Stage 4: synthesis -------------------------------------------
    log_line(run_log, "\n## Stage 4: synthesis")
    synthesis_raw = chat(
        cfg, SYNTHESIS_SYSTEM,
        "Research question: %s\n\nFraming:\n%s\n\nSources (cite ONLY "
        "these, as [Sn]):\n%s" % (question, framing, src_text),
        gen["max_tokens_synthesis"], stage="synthesis")
    with open(os.path.join(run_dir, "04-synthesis-raw.md"), "w") as f:
        f.write(synthesis_raw + "\n")
    synthesis = strip_metrics_box(synthesis_raw)
    if synthesis != synthesis_raw:
        log_line(run_log, "model-written metrics box stripped")

    # ---- Stage 5: structured evidence table ----------------------------
    log_line(run_log, "\n## Stage 5: structured evidence table")
    table_raw = chat(
        cfg, EVIDENCE_TABLE_SYSTEM,
        "Research question: %s\n\nSources (one row per source, in "
        "order):\n%s" % (question, src_text),
        gen.get("max_tokens_evidence_table", 2000), stage="evidence_table")
    with open(os.path.join(run_dir, "05-evidence-table.md"), "w") as f:
        f.write(table_raw + "\n")
    table_raw_stripped = strip_metrics_box(table_raw)
    table_rows = parse_evidence_table(table_raw_stripped, len(sources))
    log_line(run_log, "evidence table parsed: %d rows" % len(table_rows))

    # ---- Stage 6: mechanical table verification ------------------------
    log_line(run_log, "\n## Stage 6: table verification (mechanical)")
    table_ver = verify_evidence_table(table_rows)
    with open(os.path.join(run_dir, "05-table-verification.json"),
              "w") as f:
        json.dump(table_ver, f, indent=2)
    n_verified = sum(1 for v in table_ver if v["status"] == "VERIFIED")
    log_line(run_log, "table rows verified: %d/%d"
             % (n_verified, len(table_ver)))

    # ---- Stage 7: mechanical synthesis verification --------------------
    log_line(run_log, "\n## Stage 7: claim-level verification")
    ver = verify_synthesis(synthesis, len(sources))
    with open(os.path.join(run_dir, "06-verification.json"), "w") as f:
        json.dump(ver, f, indent=2)
    log_line(run_log, "verification: %s" % json.dumps(
        {k: v for k, v in ver.items() if k != "uncited_claim_sentences"}))

    # ---- Stage 8: adversarial review (flags only, never fixes) ---------
    log_line(run_log, "\n## Stage 8: adversarial review")
    review = chat(
        cfg, ADVERSARIAL_SYSTEM,
        "Draft:\n%s\n\nSource list:\n%s" % (synthesis, src_text),
        gen["max_tokens_adversarial"], stage="adversarial")
    with open(os.path.join(run_dir, "07-adversarial-review.md"), "w") as f:
        f.write(review + "\n")
    log_line(run_log, "adversarial review written (chars: %d)" % len(review))

    # ---- Stage 9: DRAFT report (human gate not yet passed) -------------
    log_line(run_log, "\n## Stage 9: DRAFT report (human review pending)")
    banner = ""
    if ver["status"] == "FAIL":
        banner = ("\n> **VERIFICATION FAILED — do not use this draft "
                  "without resolving the flags below.**\n")
    report = []
    report.append("# Oryx AI — Auditable Research Report\n")
    report.append("**Question:** %s\n" % question)
    report.append("**Status:** DRAFT — Human review: REQUIRED before "
                  "delivery. Do NOT send to a client.\n")
    report.append("**Retrieval note:** %s\n" % note)
    report.append("**Screening:** %d candidates screened, %d included, "
                  "%d excluded — see 03-screening-log.md.\n"
                  % (len(candidates), len(sources),
                     len(candidates) - len(sources)))
    report.append(banner)
    report.append("## Framing\n\n%s\n" % framing)
    report.append("## Findings\n\n%s\n" % synthesis)
    report.append("## Evidence table (verified — UNVERIFIED rows are "
                  "marked, never dropped)\n\n%s\n"
                  % render_verified_table(table_rows, table_ver))
    report.append("## Adversarial review flags (NOT fixes — reviewer must "
                  "resolve each)\n\n%s\n" % review)
    report.append("## UNSOURCED claims (quarantined — never findings)\n")
    if ver["uncited_claim_sentences"]:
        for s in ver["uncited_claim_sentences"]:
            report.append("- %s\n" % s)
        if ver["uncited_truncated"]:
            report.append("- (list truncated at 100 sentences; full list "
                          "in 06-verification.json)\n")
    else:
        report.append("None.\n")
    report.append("## Sources (included — cited as [Sn])\n")
    for i, s in enumerate(sources):
        report.append("%d. %s (%s). %s. %s\n"
                      % (i + 1, s.get("title", "Untitled"), s.get("year"),
                         s.get("authors", ""), s.get("url", "")))
    if len(candidates) - len(sources):
        report.append("\nExcluded candidates are listed with reasons in "
                      "03-screening-log.md; they are never cited.\n")
    report.append("## Metrics box (mechanical only — nothing estimated)\n")
    report.append("- Candidates screened: %d" % len(candidates))
    report.append("- Sources included: %d" % len(sources))
    report.append("- Sources cited: %d" % ver["sources_cited"])
    report.append("- Citation markers found: %d" % ver["markers_found"])
    report.append("- Invalid citation markers: %s"
                  % (ver["invalid_markers"] or "none"))
    report.append("- Evidence-table rows: %d" % len(table_rows))
    report.append("- Evidence-table rows verified: %d/%d"
                  % (n_verified, len(table_ver)))
    report.append("- Claim sentences: %d" % ver["claim_sentences"])
    report.append("- Cited claim sentences: %d"
                  % ver["cited_claim_sentences"])
    rate = ver["sourced_claim_rate_pct"]
    if rate is not None:
        report.append("- Sourced-claim rate: %d%% (mechanical sentence "
                      "rule: a sentence counts as cited if it contains "
                      "at least one valid [Sn] marker; headings "
                      "excluded)" % rate)
    else:
        report.append("- Sourced-claim rate: not computable "
                      "(no claim sentences)")
    report.append("- Citation precision: NOT mechanically computable — see "
                  "adversarial review flags above; human reviewer judges "
                  "whether each cited source supports its claim.")
    report.append("- Overall confidence: not computed mechanically — see "
                  "per-section confidence in Findings; reviewer confirms.")
    report.append("- Human review: REQUIRED — not yet done.")
    report.append("\n## Appendix: method note\n")
    report.append("Agent v0.4 staged run: framing, evidence acquisition, "
                  "screening, synthesis, structured evidence table, "
                  "mechanical table verification, mechanical claim-level "
                  "verification, adversarial review, human gate. Sources: "
                  "%s Model: %s. What would change the answer: additional "
                  "primary sources, or resolution of the adversarial flags "
                  "above.\n" % (note, cfg["backend"]["model"]))
    draft_path = os.path.join(run_dir, "08-report-draft.md")
    with open(draft_path, "w") as f:
        f.write("\n".join(report))
    if args.out:
        with open(args.out, "w") as f:
            f.write("\n".join(report))
    log_line(run_log, "DRAFT report written: %s" % draft_path)
    log_line(run_log, "token usage: %s" % json.dumps(_usage_totals()))
    print("Run directory: %s" % run_dir)
    print("Candidates: %d | included: %d | excluded: %d"
          % (len(candidates), len(sources), len(candidates) - len(sources)))
    print("Sources: %d | cited: %d | markers: %d | invalid: %s"
          % (len(sources), ver["sources_cited"], ver["markers_found"],
             ver["invalid_markers"] or "none"))
    print("Evidence-table rows: %d | verified: %d"
          % (len(table_rows), n_verified))
    print("Claim sentences: %d | cited: %d | sourced-claim rate: %s%%"
          % (ver["claim_sentences"], ver["cited_claim_sentences"],
             ver["sourced_claim_rate_pct"]
             if ver["sourced_claim_rate_pct"] is not None else "n/a"))
    print("Verification: %s" % ver["status"])
    print("Status: DRAFT — human review REQUIRED before delivery. "
          "Approve with: python3 agent.py approve --run-dir %s "
          "--reviewer \"<name>\"" % run_dir)
    return 0


def cmd_approve(args):
    run_dir = args.run_dir
    draft_path = os.path.join(run_dir, "08-report-draft.md")
    approval_path = os.path.join(run_dir, "approval.json")
    if not os.path.exists(draft_path):
        raise AgentError("no DRAFT report in %s" % run_dir)
    if os.path.exists(approval_path):
        raise AgentError("run already approved (see approval.json) — "
                         "refusing double approval")
    with open(draft_path) as f:
        draft = f.read()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M IST")
    approval = {"reviewer": args.reviewer, "approved_at": now,
                "run_dir": run_dir,
                "note": "Reviewer attests they read the adversarial flags "
                        "and the UNSOURCED quarantine. Flags stay visible "
                        "in the FINAL report; approval does not erase them."}
    with open(approval_path, "w") as f:
        json.dump(approval, f, indent=2)
    final = draft.replace(
        "**Status:** DRAFT — Human review: REQUIRED before delivery. "
        "Do NOT send to a client.",
        "**Status:** FINAL — reviewed by %s on %s."
        % (args.reviewer, now))
    final = final.replace("- Human review: REQUIRED — not yet done.",
                          "- Human review: done by %s on %s."
                          % (args.reviewer, now))
    final_path = os.path.join(run_dir, "report-final.md")
    with open(final_path, "w") as f:
        f.write(final)
    log_line(os.path.join(run_dir, "run-log.md"),
             "\n## Human gate: APPROVED by %s on %s" % (args.reviewer, now))
    print("Approved. FINAL report: %s" % final_path)
    return 0


def _first_existing(run_dir, names):
    for n in names:
        p = os.path.join(run_dir, n)
        if os.path.exists(p):
            return p
    return None


def build_audit_certificate(run_dir, approval):
    """Page-1 verification summary for the exported PDF. Every figure
    comes from the run's own verification files; anything missing is
    shown as 'not recorded', never filled in."""
    import html as _html

    def esc(v):
        return _html.escape(str(v))

    question = "not recorded"
    model = "not recorded"
    log_path = os.path.join(run_dir, "run-log.md")
    if os.path.exists(log_path):
        with open(log_path) as f:
            for line in f:
                if line.startswith("Question: "):
                    question = line[len("Question: "):].strip()
                elif line.startswith("Model: "):
                    model = line[len("Model: "):].strip()
    ver = {}
    ver_path = _first_existing(run_dir, ["06-verification.json",
                                         "04-verification.json"])
    if ver_path:
        with open(ver_path) as f:
            ver = json.load(f)
    n_sources = "not recorded"
    src_path = os.path.join(run_dir, "02-sources.json")
    if os.path.exists(src_path):
        try:
            with open(src_path) as f:
                src = json.load(f)
            n_sources = len(src) if isinstance(src, list) else "not recorded"
        except (ValueError, OSError):
            n_sources = "not recorded"
    table_ver = "not applicable to this run"
    tv_path = os.path.join(run_dir, "05-table-verification.json")
    if os.path.exists(tv_path):
        with open(tv_path) as f:
            rows = json.load(f)
        n_ok = sum(1 for r in rows if r.get("status") == "VERIFIED")
        table_ver = "%d of %d rows VERIFIED" % (n_ok, len(rows))
    flags = "not recorded"
    adv_path = _first_existing(run_dir, ["07-adversarial-review.md",
                                         "05-adversarial-review.md"])
    if adv_path:
        with open(adv_path) as f:
            flags = sum(1 for line in f
                        if re.match(r"^\d+[.\)]\s", line.strip()))

    def fig(key, suffix=""):
        v = ver.get(key)
        if v is None:
            return "not recorded"
        if isinstance(v, list):
            return ", ".join(str(x) for x in v) if v else "none"
        return "%s%s" % (v, suffix)

    rows = [
        ("Question", question),
        ("Report status", "FINAL — human-reviewed"),
        ("Reviewed by", approval.get("reviewer", "not recorded")),
        ("Review date", approval.get("approved_at", "not recorded")),
        ("Model used", model),
        ("Sources used", n_sources),
        ("Citation markers found", fig("markers_found")),
        ("Invalid markers", fig("invalid_markers")),
        ("Claim sentences", fig("claim_sentences")),
        ("Cited claims", fig("cited_claim_sentences")),
        ("Sourced-claim rate", fig("sourced_claim_rate_pct", "%")),
        ("Evidence-table rows", table_ver),
        ("Adversarial flags raised", flags),
        ("Mechanical verification", fig("status")),
    ]
    tr = "".join("<tr><th>%s</th><td>%s</td></tr>"
                 % (esc(k), esc(v)) for k, v in rows)
    return (
        '<div class="cert">'
        '<p class="cert-kicker">ORYX AI — AUDIT CERTIFICATE</p>'
        "<h1>Audit Certificate</h1>"
        "<p>This page certifies the mechanical verification performed "
        "on the attached report. It verifies citation mechanics: that "
        "markers resolve to listed sources, that claims carry citations, "
        "and that the evidence table checks out.</p>"
        "<table>%s</table>"
        '<p class="cert-scope">What this certificate does NOT certify: '
        "the truth of the underlying claims. A citation can resolve and "
        "still not support its claim — that judgement belongs to the "
        "adversarial flags above, to the human reviewer named here, and "
        "to the reader.</p>"
        '<p class="cert-foot">Figures are computed mechanically from the '
        "run's verification files. They are not written by the model, "
        "and nothing on this page is a claim about the report's subject "
        "matter.</p>"
        "</div>" % tr)


CERT_CSS = """
.cert { page-break-after: always; }
.cert-kicker { font-family: Helvetica, Arial, sans-serif; font-size: 9pt;
  letter-spacing: 2px; color: #C9A227; font-weight: bold; }
.cert-scope { font-size: 9.5pt; color: #52607A; border: 1px solid #C9A227;
  padding: 10px 14px; }
.cert-foot { font-size: 9pt; color: #52607A; }
"""


def cmd_export_pdf(args):
    run_dir = args.run_dir
    approval_path = os.path.join(run_dir, "approval.json")
    final_path = os.path.join(run_dir, "report-final.md")
    if not os.path.exists(approval_path):
        raise AgentError(
            "refusing PDF export: run is not approved (no approval.json). "
            "Only FINAL reports may be exported.")
    if not os.path.exists(final_path):
        raise AgentError("no FINAL report in %s" % run_dir)
    with open(approval_path) as f:
        approval = json.load(f)
    with open(final_path) as f:
        md_text = f.read()
    cert_path = os.path.join(run_dir, "audit-certificate.html")
    with open(cert_path, "w") as f:
        f.write(build_audit_certificate(run_dir, approval))
    out_path = args.out or os.path.join(run_dir, "report-final.pdf")
    html_path = os.path.join(run_dir, "report-final.html")
    build = (
        "import sys\n"
        "import markdown\n"
        "with open(sys.argv[1]) as f:\n"
        "    md_text = f.read()\n"
        "with open(sys.argv[5]) as f:\n"
        "    cert_html = f.read()\n"
        "body = cert_html + markdown.markdown(md_text, extensions="
        "['tables', 'sane_lists'])\n"
        "html = ('<!DOCTYPE html><html><head><meta charset=\"utf-8\">'\n"
        "        '<style>' + sys.argv[3] + '</style></head><body>' + body +\n"
        "        '</body></html>')\n"
        "with open(sys.argv[2], 'w') as f:\n"
        "    f.write(html)\n"
        "from weasyprint import HTML\n"
        "HTML(sys.argv[2]).write_pdf(sys.argv[4])\n")
    tmp = os.path.join(run_dir, "_build_pdf.py")
    with open(tmp, "w") as f:
        f.write(build)
    try:
        subprocess.run(
            [VENV_PY, tmp, final_path, html_path, PDF_CSS + CERT_CSS,
             out_path, cert_path],
            check=True, capture_output=True, text=True, timeout=300)
    except subprocess.CalledProcessError as e:
        raise AgentError("PDF build failed: %s"
                         % (e.stderr.strip()[-500:] or e.stdout.strip()[-500:]))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise AgentError("PDF build produced no output")
    log_line(os.path.join(run_dir, "run-log.md"),
             "\n## PDF export: %s (reviewer: %s)"
             % (out_path, approval.get("reviewer")))
    print("PDF exported: %s (%d bytes)"
          % (out_path, os.path.getsize(out_path)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Oryx AI agent v0.4")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_ask = sub.add_parser("ask", help="run a staged research job")
    p_ask.add_argument("--question", required=True)
    p_ask.add_argument("--sources-json", default="",
                       help="JSON file with operator-curated sources")
    p_ask.add_argument("--limit", type=int, default=10)
    p_ask.add_argument("--out", default="",
                       help="also copy the DRAFT report to this path")
    p_ask.add_argument("--run-dir", default="",
                       help="run directory (default: runs/<slug>-<stamp>)")
    p_ask.set_defaults(func=cmd_ask)
    p_ap = sub.add_parser("approve", help="human-gate approval of a DRAFT")
    p_ap.add_argument("--run-dir", required=True)
    p_ap.add_argument("--reviewer", required=True)
    p_ap.set_defaults(func=cmd_approve)
    p_pdf = sub.add_parser("export-pdf",
                           help="export an APPROVED run's FINAL report to "
                                "PDF (refuses unapproved runs)")
    p_pdf.add_argument("--run-dir", required=True)
    p_pdf.add_argument("--out", default="",
                       help="PDF path (default: <run-dir>/report-final.pdf)")
    p_pdf.set_defaults(func=cmd_export_pdf)
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except AgentError as e:
        print("AGENT FAILED: %s" % e, file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print("AGENT FAILED: missing file: %s" % e, file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - fail loudly, never silently
        print("AGENT FAILED (unexpected): %s: %s"
              % (type(e).__name__, e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
