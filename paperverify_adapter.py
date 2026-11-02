#!/usr/bin/env python3
"""Oryx adapter: audit JSON -> paper-verify --from-evidence scoring (offline, no keys).

Input: an audit file with rows {brief, ref, url, http_status, page_title, verdict}
plus an optional Q2 file with rows {brief, ref, q2, reason}.
Q2 verdicts map to paper-verify verdicts: Supports->Match, Partially supports->Partial,
Does not support->Mismatch, Unassessable->Uncertain. Rows without Q2 map from the
audit verdict: Verified->Uncertain (resolved but claim not read), Broken->Inaccessible,
Title mismatch->Mismatch, No URL->Inaccessible, Unverifiable->Uncertain.
Output: evidence JSON + paper-verify report (score, tiers) in the output dir.
The scorer is a REPORTING layer only: it never fetches, never judges, never
replaces Oryx's own audit verdicts. Vendored scorer: external/paper-verify (MIT).
"""
import json, subprocess, sys, os
from pathlib import Path

Q2_MAP = {"Supports": "Match", "Partially supports": "Partial",
          "Does not support": "Mismatch", "Unassessable": "Uncertain"}
AUDIT_MAP = {"Verified": "Uncertain", "Broken": "Inaccessible",
             "Title mismatch": "Mismatch", "No URL": "Inaccessible",
             "Unverifiable": "Uncertain"}

def build_evidence(audit_path, q2_path, source_file):
    rows = json.load(open(audit_path))
    q2 = {}
    if q2_path and Path(q2_path).exists():
        for r in json.load(open(q2_path)):
            q2[(r["brief"], r["ref"])] = r
    cits = []
    for r in rows:
        key = (r.get("brief"), r.get("ref"))
        q = q2.get(key)
        if q:
            verdict, reason = Q2_MAP[q["q2"]], q["reason"]
        else:
            verdict = AUDIT_MAP.get(r.get("verdict"), "Uncertain")
            reason = f"Audit verdict {r.get('verdict')} — no Q2 judgement (claim not content-read)"
        try: status = int(r.get("http_status"))
        except (TypeError, ValueError): status = 0
        cits.append({
            "citation": {"type": "URL", "ref": r.get("url", ""),
                         "context": (r.get("cited_text") or "")[:200], "line": r.get("ref", 0)},
            "fetched": {"status": status, "title": r.get("page_title", ""),
                        "abstract": "", "url_final": r.get("final_url") or r.get("url", ""),
                        "source": "http"},
            "judgements": [{"judge": "oryx-audit", "verdict": verdict, "reason": reason}],
        })
    return {"source_file": source_file, "level": "L2", "citations": cits}

def main():
    audit, outdir = sys.argv[1], sys.argv[2]
    q2 = sys.argv[3] if len(sys.argv) > 3 else None
    out = Path(outdir); out.mkdir(parents=True, exist_ok=True)
    ev = build_evidence(audit, q2, Path(audit).name)
    ev_path = out / "evidence.json"; ev_path.write_text(json.dumps(ev, indent=2))
    here = Path(__file__).parent
    env = dict(os.environ, PYTHONPATH=str(here / "external" / "paper-verify"))
    r = subprocess.run([sys.executable, "-m", "paperverify", "--from-evidence",
                        str(ev_path), "--json", "--out", str(out)],
                       capture_output=True, text=True, env=env)
    print(r.stdout[-2000:] if r.stdout else r.stderr[-2000:])
    if r.returncode != 0:
        print(r.stderr[-2000:], file=sys.stderr); sys.exit(r.returncode)
    print(f"Evidence: {ev_path}  Report: {out}")

if __name__ == "__main__":
    main()
