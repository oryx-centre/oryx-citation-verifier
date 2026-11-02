#!/usr/bin/env python3
"""Explicit opt-in, repeatable CLI smoke tests against a local/public corpus.

No LLM calls. A passing run checks transport, identity and conservative output
contracts, not scientific validity or semantic claim accuracy. Input manifest:
[{"id": "case-01", "file": "/path/doc.md", "expected_title": "..."}]
Use "text" instead of "file" for portable examples. Outputs are local artifacts.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time


def normalize(text: str) -> str:
    return ''.join(c.casefold() for c in text if c.isalnum())


def assess(result: dict, expected_title: str) -> dict:
    citations = result.get('citations', [])
    matching = [c for c in citations if normalize((c.get('fetched') or {}).get('title', '')) == normalize(expected_title)]
    return {
        'citations_present': bool(citations),
        'expected_metadata_found': any((c.get('fetched') or {}).get('status') == 200 for c in matching),
        'keyword_scope_disclosed': result.get('keyword_only') is True,
        'no_heuristic_strong_evidence': all(c.get('evidence_quality') != 'strong' for c in citations),
        'missing_content_abstains': all(c.get('effective_verdict') in ('Uncertain', 'Inaccessible', None)
            for c in citations if not (c.get('fetched') or {}).get('abstract', '').strip()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--live', action='store_true', help='explicitly allow public source HTTP requests')
    parser.add_argument('--replay', type=Path, help='re-score an earlier output directory without network')
    args = parser.parse_args()
    if args.live == bool(args.replay):
        parser.error('choose exactly one of --live or --replay DIRECTORY')
    cases = json.loads(args.manifest.read_text(encoding='utf-8'))
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        parser.error('manifest must contain 1–100 cases')
    ids = set()
    for case in cases:
        cid = case.get('id', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', cid) or cid in ids:
            parser.error('case ids must be unique simple names')
        ids.add(cid)
        if bool(case.get('file')) == bool(case.get('text')) or not case.get('expected_title', '').strip():
            parser.error('each case requires expected_title and exactly one of file or text')
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out/'manifest.json').write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding='utf-8')
    summaries = []
    for case in cases:
        target = args.out/case['id']; target.mkdir()
        text = Path(case['file']).read_text(encoding='utf-8') if case.get('file') else case['text']
        doc = target/'input.md'; doc.write_text(text, encoding='utf-8')
        cmd = [sys.executable, '-m', 'paperverify.cli']
        if args.live:
            cmd += [str(doc), '--level', 'L2', '--judge', 'keyword', '--workers', '2']
        else:
            previous = json.loads((args.replay/case['id']/'result.json').read_text(encoding='utf-8'))
            evidence = {'source_file': str(doc), 'level': previous['level'], 'citations': previous['citations']}
            ev = target/'evidence.json'; ev.write_text(json.dumps(evidence, ensure_ascii=False), encoding='utf-8')
            cmd += ['--from-evidence', str(ev)]
        cmd += ['--json', '--out', str(target)]
        started = time.monotonic()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
            (target/'stderr.log').write_text(proc.stderr, encoding='utf-8')
            (target/'result.json').write_text(proc.stdout, encoding='utf-8')
            result = json.loads(proc.stdout) if proc.returncode == 0 else {}
            checks = assess(result, case['expected_title'])
            checks['cli_exit_zero'] = proc.returncode == 0
            checks['artifacts_written'] = bool(list(target.glob('*_report.md'))) and bool(list(target.glob('*_claims.jsonl')))
            summary = {'id':case['id'], 'checks':checks, 'pass':all(checks.values()),
                       'citation_count':len(result.get('citations', [])), 'score':result.get('overall_score'),
                       'verdicts':[c['effective_verdict'] for c in result.get('citations', [])]}
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            summary = {'id':case['id'], 'pass':False, 'error':str(exc)}
        summary.update(seconds=round(time.monotonic()-started, 2), input_sha256=hashlib.sha256(text.encode()).hexdigest())
        summaries.append(summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        (args.out/'summary.json').write_text(json.dumps({'mode':'live' if args.live else 'replay', 'cases':summaries,
            'passed':sum(s['pass'] for s in summaries), 'total':len(cases), 'semantic_accuracy_tested':False}, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if all(s['pass'] for s in summaries) else 1


if __name__ == '__main__':
    raise SystemExit(main())
