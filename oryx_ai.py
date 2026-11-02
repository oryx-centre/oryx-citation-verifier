#!/usr/bin/env python3
"""Oryx AI — MVP v0.1 (auditable research agent, command line).

Pipeline: question -> source retrieval -> synthesis (optional) -> verification
-> auditable report in Markdown.

Honesty rules built into the code:
- The report may cite ONLY sources in its source list, as [S1], [S2], ...
- A mechanical verification pass checks every citation marker resolves.
- If live retrieval is rate-limited, the run says so and (only with --demo)
  falls back to clearly labelled demo sources. Demo sources are never
  presented as retrieved evidence.
- If no model is configured, the output is a sources-only brief: zero
  generated claims. Synthesis activates only when the operator sets:
    ORYX_LLM_BASE_URL  (OpenAI-compatible base URL, e.g. a local router)
    ORYX_LLM_API_KEY   (never printed, never committed, env only)
    ORYX_LLM_MODEL     (model id served at that base URL)

Free retrieval backends (no key required, shared-IP budgets):
- Semantic Scholar Graph API (rate-limited on shared IPs; 429 is handled).
- OpenAlex (free key available from OpenAlex; set OPENALEX_API_KEY to
  raise the budget above the shared pool).

Usage:
  python3 oryx_ai.py --question "your research question" --out report.md
  python3 oryx_ai.py --question "..." --demo --out demo-report.md
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

SYSTEM_PROMPT = """You are Oryx AI, an auditable research agent. Rules:
1. Every material claim carries a source marker [Sn] from the source list.
2. Never cite a source that is not in the list. Never invent statistics,
   people, organisations, dates, or quotations.
3. State confidence per section (High/Medium/Low) and why.
4. State what the evidence does NOT show. 'Evidence insufficient' is valid.
5. Separate facts from inference from recommendation.
6. End with a metrics box. Do not estimate the metrics; report what you did.
"""

# Clearly labelled demo sources. [S1] is real (the Oryx working paper).
# [S2]-[S3] are placeholders, labelled as such, never cited as evidence.
DEMO_SOURCES = [
    {
        "title": "From Answers to Auditable Intelligence: A Governance-First Framework "
                 "for Agentic AI in Strategic Research and Public Policy",
        "authors": "Kumar, Amartya; Nalwa, Riya",
        "year": 2026,
        "url": "https://doi.org/10.5281/zenodo.23601643",
        "abstract": "Conceptual working paper proposing a six-layer governance-first "
                   "framework and evaluation protocol for agentic AI in "
                   "strategic research and public policy. Reports no empirical "
                   "results, describes no deployed product, not peer reviewed.",
    },
    {
        "title": "PLACEHOLDER — replace with a retrieved source in live mode",
        "authors": "PLACEHOLDER",
        "year": None,
        "url": "",
        "abstract": "PLACEHOLDER abstract. This entry exists only to exercise the "
                   "citation-verification mechanics in demo mode.",
    },
    {
        "title": "PLACEHOLDER — replace with a retrieved source in live mode",
        "authors": "PLACEHOLDER",
        "year": None,
        "url": "",
        "abstract": "PLACEHOLDER abstract. This entry exists only to exercise the "
                   "citation-verification mechanics in demo mode.",
    },
]


class RateLimited(Exception):
    pass


def http_get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "OryxAI-MVP/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(url)
        raise


def fetch_semantic_scholar(query, limit=10):
    params = urllib.parse.urlencode({
        "query": query,
        "fields": "title,abstract,year,authors,externalIds,url",
        "limit": limit,
    })
    data = http_get_json(
        "https://api.semanticscholar.org/graph/v1/paper/search?" + params)
    out = []
    for p in data.get("data", []):
        authors = ", ".join(a.get("name", "") for a in p.get("authors", []))
        doi = (p.get("externalIds") or {}).get("DOI", "")
        out.append({
            "title": p.get("title") or "Untitled",
            "authors": authors,
            "year": p.get("year"),
            "url": ("https://doi.org/" + doi) if doi else (p.get("url") or ""),
            "abstract": (p.get("abstract") or "")[:600],
        })
    return out


def reconstruct_abstract(inverted_index):
    """Rebuild readable text from OpenAlex's abstract_inverted_index
    ({word: [positions]}). Returns "" when the index is absent."""
    if not inverted_index:
        return ""
    placed = []
    for word, positions in inverted_index.items():
        for pos in positions:
            placed.append((pos, word))
    placed.sort()
    return " ".join(w for _, w in placed)


def fetch_openalex_abstract(openalex_id, key):
    """Fetch one work's abstract via its OpenAlex record. Returns ""
    on any failure — a missing abstract must never kill retrieval."""
    if not openalex_id:
        return ""
    # The canonical id (https://openalex.org/W...) serves an HTML page,
    # not JSON — use the API endpoint with the bare work id instead.
    wid = openalex_id.rstrip("/").rsplit("/", 1)[-1]
    if not wid.startswith("W"):
        return ""
    params = {}
    if key:
        params["api_key"] = key
    url = "https://api.openalex.org/works/" + wid
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        data = http_get_json(url)
    except Exception:  # noqa: BLE001 - per-work failure is non-fatal
        return ""
    return reconstruct_abstract(data.get("abstract_inverted_index"))[:600]


def fetch_openalex(query, per_page=10):
    key = os.environ.get("OPENALEX_API_KEY", "")
    if not key:
        # Fallback: key file stored 0600 by the operator (never in code).
        try:
            with open(os.path.expanduser(
                    "~/workspace/routers/openalex-api-key.txt")) as _f:
                key = _f.read().strip()
        except OSError:
            key = ""
    # OpenAlex treats * and ? as wildcards, which the default (stemmed)
    # search rejects with HTTP 400. A research question ends in "?" by
    # nature; strip both so a natural question never becomes a wildcard
    # query. (Found 25 Oct 2026: full-question searches failed 400 while
    # keyword searches passed 200 with the same key.)
    query = query.replace("*", " ").replace("?", " ")
    params = {"search": query, "per-page": per_page}
    if key:
        params["api_key"] = key
    data = http_get_json(
        "https://api.openalex.org/works?" + urllib.parse.urlencode(params))
    out = []
    for w in data.get("results", []):
        authors = ", ".join(
            a.get("author", {}).get("display_name", "")
            for a in w.get("authorships", []))
        # Search results carry no abstract text, and agent screening
        # mechanically excludes candidates with no evidence text — so a
        # bare search result could never pass screening (found 25 Oct
        # 2026: 11/11 excluded). Fetch each work's abstract from its
        # record; a failed fetch leaves "" and that candidate may still
        # be excluded, but the rest proceed.
        abstract = fetch_openalex_abstract(w.get("id"), key)
        out.append({
            "title": w.get("display_name") or "Untitled",
            "authors": authors,
            "year": w.get("publication_year"),
            "url": w.get("doi") or w.get("id") or "",
            "abstract": abstract,
        })
    return out


def retrieve(question, limit=10):
    """Return (sources, retrieval_note). Raises RateLimited only if BOTH
    backends are rate-limited; otherwise returns what succeeded."""
    sources, notes = [], []
    try:
        sources.extend(fetch_semantic_scholar(question, limit))
    except RateLimited:
        notes.append("Semantic Scholar: rate-limited (shared-IP budget).")
    except Exception as e:  # noqa: BLE001 - report, don't crash the run
        notes.append("Semantic Scholar: error (%s)." % e)
    try:
        sources.extend(fetch_openalex(question, limit))
    except RateLimited:
        notes.append("OpenAlex: rate-limited (shared-IP budget; resets "
                     "midnight UTC, or set OPENALEX_API_KEY).")
    except Exception as e:  # noqa: BLE001
        notes.append("OpenAlex: error (%s)." % e)
    # Dedupe by normalised title, keep first.
    seen, deduped = set(), []
    for s in sources:
        key = (s["title"] or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(s)
    return deduped, " ".join(notes)


def synthesise(question, sources):
    """Optional LLM synthesis. Returns None when no model is configured,
    in which case the caller emits a sources-only brief.

    Backends (ORYX_LLM_BACKEND):
    - "omniroute" (default when set): POST to the local OmniRoute server's
      OpenAI-compatible endpoint. The server holds the provider credentials;
      no key is handled by this script. Model via ORYX_LLM_MODEL
      (default gemini-3.1-flash-lite). Generous max_tokens: the routed
      model spends ~160 tokens on reasoning before answering.
    - "openai": OpenAI-compatible endpoint from ORYX_LLM_BASE_URL +
      ORYX_LLM_API_KEY + ORYX_LLM_MODEL (env only, never printed)."""
    backend = os.environ.get("ORYX_LLM_BACKEND", "")
    src_text = "\n".join(
        "[S%d] %s (%s). %s. %s" % (i + 1, s["title"], s["year"],
                                   s["authors"], s["abstract"])
        for i, s in enumerate(sources))
    user = ("Research question: %s\n\nSources (cite ONLY these, as [Sn]):\n%s"
            % (question, src_text))
    if backend == "omniroute":
        base = os.environ.get("ORYX_LLM_BASE_URL",
                              "http://localhost:20128").rstrip("/")
        model = os.environ.get("ORYX_LLM_MODEL", "gemini-3.1-flash-lite")
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": 3000,
        }).encode("utf-8")
        req = urllib.request.Request(
            base + "/v1/chat/completions", data=payload,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - fall back to sources-only
            print("OmniRoute synthesis failed (%s); using sources-only brief." % e)
            return None
        text = data["choices"][0]["message"].get("content") or ""
        return text.strip() or None
    base = os.environ.get("ORYX_LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("ORYX_LLM_API_KEY", "")
    model = os.environ.get("ORYX_LLM_MODEL", "")
    if not (base and key and model):
        return None
    src_text = "\n".join(
        "[S%d] %s (%s). %s. %s" % (i + 1, s["title"], s["year"],
                                   s["authors"], s["abstract"])
        for i, s in enumerate(sources))
    user = ("Research question: %s\n\nSources (cite ONLY these, as [Sn]):\n%s"
            % (question, src_text))
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions", data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def verify(report, n_sources):
    markers = [int(m) for m in re.findall(r"\[S(\d+)\]", report)]
    invalid = sorted({m for m in markers if m < 1 or m > n_sources})
    cited = sorted(set(markers))
    return {"markers_found": len(markers), "sources_cited": len(cited),
            "invalid_markers": invalid}


def build_report(question, sources, synthesis, retrieval_note, demo=False):
    lines = []
    if demo:
        lines.append("> DEMO RUN — live retrieval was rate-limited; demo "
                     "sources below. Do NOT send this report to a client.")
    lines.append("# Oryx AI — Auditable Research Report\n")
    lines.append("**Question:** %s\n" % question)
    if retrieval_note:
        lines.append("**Retrieval note:** %s\n" % retrieval_note)
    if synthesis:
        # The model sometimes writes its own metrics box (e.g. an invented
        # processing time). The mechanical metrics box below is the only
        # authoritative one, so strip any model-written metrics section.
        m = re.search(r"^#{1,4}\s*Metrics Box\s*$", synthesis, re.MULTILINE)
        if m:
            synthesis = (synthesis[:m.start()].rstrip()
                         + "\n\n*(Model-written metrics removed — the "
                         "mechanical metrics box below is authoritative.)*\n")
        lines.append("## Findings\n\n%s\n" % synthesis)
    else:
        lines.append("## Findings (sources-only brief — no model configured, "
                     "zero generated claims)\n")
        for i, s in enumerate(sources):
            lines.append("### [S%d] %s (%s)\n\n%s\n\n%s\n"
                         % (i + 1, s["title"], s["year"], s["authors"],
                            s["abstract"] or "(No abstract returned.)"))
    lines.append("## What the evidence does NOT show\n\n")
    lines.append("Anything not covered by the sources above. Where evidence "
                 "is insufficient, that is stated here rather than filled "
                 "with a guess.\n")
    lines.append("## Sources\n")
    for i, s in enumerate(sources):
        lines.append("%d. %s (%s). %s. %s\n"
                     % (i + 1, s["title"], s["year"], s["authors"], s["url"]))
    body = "\n".join(lines)
    v = verify(body, len(sources))
    lines.append("## Metrics box\n")
    lines.append("- Sources retrieved: %d" % len(sources))
    lines.append("- Sources cited: %d" % v["sources_cited"])
    lines.append("- Citation markers found: %d" % v["markers_found"])
    lines.append("- Invalid citation markers: %s"
                 % (v["invalid_markers"] or "none"))
    lines.append("- Generated claims: %s"
                 % ("see synthesis above (human review required before "
                    "delivery)" if synthesis else "0 (sources-only brief)"))
    lines.append("- Human review: REQUIRED before delivery — not yet done.")
    return "\n".join(lines) + "\n", v


def main(argv=None):
    ap = argparse.ArgumentParser(description="Oryx AI MVP v0.1")
    ap.add_argument("--question", required=True)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--out", default="oryx-ai-report.md")
    ap.add_argument("--demo", action="store_true",
                    help="Use labelled demo sources (never for clients).")
    ap.add_argument("--sources-json", default="",
                    help="JSON file with a list of source objects "
                    "(title/authors/year/url/abstract) gathered from "
                    "verified search results; skips live API retrieval.")
    args = ap.parse_args(argv)

    demo = False
    if args.sources_json:
        with open(args.sources_json) as f:
            sources = json.load(f)
        note = ("Sources operator-curated from verified web search results; "
                "live API retrieval bypassed (shared-IP rate limits).")
    else:
        try:
            sources, note = retrieve(args.question, args.limit)
        except Exception as e:  # noqa: BLE001
            sources, note = [], "Retrieval failed: %s" % e
        if not sources and args.demo:
            sources, note = DEMO_SOURCES, (note + " Demo sources used.").strip()
            demo = True
    if not sources:
        print("No sources retrieved and --demo not set. " + note)
        print("Retry after the shared budgets reset (midnight UTC) or set "
              "OPENALEX_API_KEY.")
        return 2
    synthesis = synthesise(args.question, sources) if not demo else None
    report, v = build_report(args.question, sources, synthesis, note, demo)
    with open(args.out, "w") as f:
        f.write(report)
    print("Wrote %s" % args.out)
    print("Sources: %d | cited: %d | invalid markers: %s | synthesis: %s"
          % (len(sources), v["sources_cited"], v["invalid_markers"] or "none",
             "yes" if synthesis else "no (sources-only)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
