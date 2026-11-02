#!/usr/bin/env python3
"""Oryx AI v2 verification layers (stdlib + vendored/open tools, no keys, no spend).

Layer 1 — OneCite normalisation (HzaCode/OneCite, MIT): for sources carrying a
strong identifier (DOI / arXiv / PMID), resolve it through the documented
metadata route and compare the returned title/author/year with the title the
agent recorded. A mismatch is a FLAG, never an auto-correction; a failure is
"unresolved", never "fabricated"; plain-URL sources without an identifier skip
this layer entirely (OneCite does not fuzzy-promote them, and neither do we).

Layer 2 — paper-verify triage score (nolainjin/paper-verify, MIT, vendored in
external/paper-verify): our own mechanical evidence (evidence-table row checks
+ OneCite flags) is scored through its 100-point rubric via --from-evidence.
This score is labelled MECHANICAL TRIAGE SCORE in every surface that shows it:
it does NOT measure claim support (that stays a human Q2-style judgement, as
Paper 2 establishes), and it never replaces the Audit Certificate figures.

Usage: python3 verify_layers.py <run_dir>
Writes <run_dir>/09-layer-audit.json and <run_dir>/09-paperverify-report.md
(plus evidence.json). Exits 0 even if a layer partially fails; failures are
recorded in the audit file, never hidden. Never fails the job itself.
"""
import json, os, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).parent
ONECITE = Path.home() / "workspace" / "venvs" / "repo-eval" / "bin" / "onecite"
PV_DIR = HERE / "external" / "paper-verify"

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", re.I)
PMID_RE = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", re.I)


def extract_identifier(url):
    m = DOI_RE.search(url or "")
    if m:
        return "doi", m.group(0).rstrip(".,)")
    m = ARXIV_RE.search(url or "")
    if m:
        return "arxiv", m.group(1)
    m = PMID_RE.search(url or "")
    if m:
        return "pmid", m.group(1)
    return None, None


def parse_bibtex(text):
    def field(name):
        m = re.search(rf"{name}\s*=\s*\{{(.*?)\}}", text, re.S | re.I)
        return m.group(1).strip() if m else ""
    return {"title": field("title"), "author": field("author"), "year": field("year")}


def titles_agree(a, b):
    def norm(t):
        return re.sub(r"[^a-z0-9 ]", "", (t or "").lower())
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    return na in nb or nb in na or na[:60] == nb[:60]


def onecite_normalize(identifier):
    """Returns (status, metadata_dict, note). status: match/mismatch/unresolved."""
    if not ONECITE.exists():
        return "unresolved", {}, "onecite binary not installed"
    try:
        r = subprocess.run([str(ONECITE), "process", identifier],
                           capture_output=True, text=True, timeout=90)
        if "Successfully processed: 1" not in r.stdout:
            return "unresolved", {}, "onecite could not resolve identifier"
        return "resolved", parse_bibtex(r.stdout), ""
    except Exception as e:  # noqa: BLE001 - recorded, never fatal
        return "unresolved", {}, f"onecite error: {e}"


def build(run_dir):
    run_dir = Path(run_dir)
    sources = json.loads((run_dir / "02-sources.json").read_text())
    table_ver = []
    tv_path = run_dir / "05-table-verification.json"
    if tv_path.exists():
        table_ver = json.loads(tv_path.read_text())
    ver = {}
    v_path = run_dir / "06-verification.json"
    if v_path.exists():
        ver = json.loads(v_path.read_text())

    rows, evidence = [], []
    for i, src in enumerate(sources, start=1):
        url = src.get("url", "")
        kind, ident = extract_identifier(url)
        mech = next((t for t in table_ver if t.get("source_index") == i), {})
        mech_ok = mech.get("status") == "VERIFIED"
        oc_status, meta, note = ("skipped", {}, "no strong identifier")
        if ident:
            oc_status, meta, note = onecite_normalize(ident)
            if oc_status == "resolved":
                oc_status = "match" if titles_agree(meta.get("title"), src.get("title")) else "mismatch"
        # Triage verdict (NOT claim support): mechanical row + metadata agreement.
        if oc_status == "mismatch" or not mech_ok:
            verdict, reason = "Mismatch", "evidence-table row fails its own-source check or metadata title disagrees"
        elif oc_status == "match":
            verdict, reason = "Partial", "mechanical row OK and metadata title agrees; claim content not read"
        elif oc_status == "unresolved":
            verdict, reason = "Uncertain", "identifier could not be resolved through the metadata route"
        else:
            verdict, reason = "Partial", "mechanical row OK; no strong identifier to normalise"
        rows.append({
            "ref": i, "url": url, "title": src.get("title", ""),
            "identifier": ident, "identifier_kind": kind,
            "onecite": oc_status, "onecite_meta": meta, "onecite_note": note,
            "mechanical": mech.get("status", "unknown"),
            "triage_verdict": verdict,
        })
        evidence.append({
            "citation": {"type": "URL", "ref": url, "context": src.get("title", "")[:200], "line": i},
            "fetched": {"status": 200 if mech_ok else 0, "title": src.get("title", ""),
                        "abstract": "", "url_final": url, "source": "http"},
            "judgements": [{"judge": "oryx-mechanical", "verdict": verdict, "reason": reason}],
        })

    audit = {"layer": "v2", "sources": rows,
             "sourced_claim_rate_pct": ver.get("sourced_claim_rate_pct"),
             "mechanical_status": ver.get("status"),
             "note": "Mechanical triage only. Claim support is a human judgement; see Audit Certificate."}
    (run_dir / "09-layer-audit.json").write_text(json.dumps(audit, indent=2))
    ev_path = run_dir / "09-evidence.json"
    ev_path.write_text(json.dumps({"source_file": "report", "level": "L2", "citations": evidence}, indent=2))
    # Layer 2: paper-verify scoring, offline, no network.
    score = None
    try:
        env = dict(os.environ, PYTHONPATH=str(PV_DIR))
        r = subprocess.run([sys.executable, "-m", "paperverify", "--from-evidence",
                            str(ev_path), "--out", str(run_dir / "09-paperverify")],
                           capture_output=True, text=True, env=env, timeout=120)
        rep = run_dir / "09-paperverify" / "09-evidence_report.md"
        if rep.exists():
            m = re.search(r"Overall score: \*\*([\d.]+)/100", rep.read_text())
            score = float(m.group(1)) if m else None
            (run_dir / "09-paperverify-report.md").write_text(rep.read_text())
        audit["paperverify_triage_score"] = score
        audit["paperverify_error"] = "" if score is not None else (r.stdout + r.stderr)[-500:]
    except Exception as e:  # noqa: BLE001
        audit["paperverify_triage_score"] = None
        audit["paperverify_error"] = str(e)
    (run_dir / "09-layer-audit.json").write_text(json.dumps(audit, indent=2))
    return audit


if __name__ == "__main__":
    a = build(sys.argv[1])
    print(f"layers done: {len(a['sources'])} sources, triage score {a.get('paperverify_triage_score')}")
    for s in a["sources"]:
        print(f"  [S{s['ref']}] onecite={s['onecite']} mech={s['mechanical']} triage={s['triage_verdict']} {s['url'][:60]}")
