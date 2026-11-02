"""Markdown report rendering (SKILL §5)."""

from __future__ import annotations

import datetime
import os

from .models import Report, ScoredCitation, Tier, Verdict

# Schema version for the machine-readable JSON surface (render_json / MCP).
# Bump on any breaking change to the output shape.
#   "1" -> "2": added optional top-level "profile" field (active harness
#   profile key, or null). Additive, but bumped per repo convention.
#   "2" -> "3": added per-citation Fetched fields "authors" (list), "year"
#   (int|null), "source" (str: crossref|arxiv|ncbi|http|archive|none), and
#   "soft_404_suspect" (bool); added the "Uncertain" verdict. Additive.
#   "3" -> "4": added per-citation consensus/effective_verdict fields so JSON
#   consumers can read the verdict used for scoring directly.
#   "4" -> "5": added top-level "keyword_only" (bool) — True when every judge
#   used was the dependency-free keyword heuristic (token overlap only, no
#   semantic verification), so consumers can flag the result for manual review.
#   "5" -> "6": added per-citation review_flags and evidence_quality.
#   "6" -> "7": retain tie-break evidence and per-field abstract provenance.
SCHEMA_VERSION = "7"


def _is_keyword_only(report: Report) -> bool:
    """True when every judge used is the dependency-free keyword heuristic.

    The keyword judge does token overlap only — it cannot confirm that a source
    *semantically supports* a claim (it is blind to direction/negation; see the
    H2 clamp in judge.py). A keyword-only run therefore carries no semantic
    verification and its scores must be read as "needs manual spot-check".
    """
    if not report.judges:
        return False
    return all(j.strip().lower() == "keyword" for j in report.judges)

_TIER_ORDER = [Tier.F, Tier.C, Tier.B, Tier.A]
_TIER_LABEL = {
    Tier.F: "F (verification failed — review evidence)",
    Tier.C: "C (must re-check)",
    Tier.B: "B (minor fixes)",
    Tier.A: "A (high rubric score — review scope)",
}


def _excerpt(text: str, n: int = 140) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _detail_line(sc: ScoredCitation) -> str:
    c = sc.citation
    v = sc.consensus
    vsym = v.symbol if v else "⚫"
    head = f"{vsym} **{c.type}** `{_excerpt(c.ref, 80)}` (line {c.line}, {sc.score:.0f}/100)"
    body = f"   - claim: {_excerpt(c.context)}"
    if sc.fetched and sc.fetched.abstract_source:
        body += f"\n   - text source: {sc.fetched.abstract_source}"
    if v:
        body += f"\n   - effective verdict: {v.value}"
    for j in sc.judgements:
        body += f"\n   - {j.judge}: {j.verdict.value}; reason: {_excerpt(j.reason)}"
    if sc.tiebreak_judgement:
        j = sc.tiebreak_judgement
        body += f"\n   - tie-break {j.judge}: {j.verdict.value}; reason: {_excerpt(j.reason)}"
    return head + "\n" + body


def _consensus_reason(sc: ScoredCitation) -> str:
    """Identify each opinion instead of presenting the first as the consensus."""
    judgements = list(sc.judgements)
    if sc.tiebreak_judgement:
        judgements.append(sc.tiebreak_judgement)
    return "; ".join(f"{j.judge}: {j.verdict.value} — {j.reason}" for j in judgements)


def render_markdown(report: Report) -> str:
    """Render a :class:`Report` to the SKILL §5 Markdown format."""
    today = datetime.date.today().isoformat()
    basename = os.path.basename(report.source_file)
    overall = report.overall_score
    otier = report.overall_tier

    lines: list[str] = []
    lines.append(f"# Citation Verification Report — {basename}")
    lines.append("")
    if report.has_failure:
        lines.append(
            "> ⚠️ **DOCUMENT-LEVEL WARNING** — at least one citation is tier 🔴 F "
            "(not reliably verified). A low score can reflect missing or inaccessible "
            "evidence and does not establish fabrication. Review the reasons below."
        )
        lines.append("")
    lines.append(f"- Date: {today}")
    lines.append(f"- Source: `{report.source_file}`")
    lines.append(f"- Level: {report.level}")
    lines.append(f"- Judges: {', '.join(report.judges) if report.judges else '(none)'}")
    lines.append(f"- Citations: {len(report.scored)}")
    lines.append(f"- Overall score: **{overall}/100 {otier.symbol} {otier.value}**")
    lines.append("")
    if not report.scored:
        lines.append("> **No citations found.** This run did not verify any claims; the zero score is not a document-quality assessment.")
        lines.append("")
    if report.level == "L1":
        lines.append("> **Reachability only.** L1 scores do not establish that a source supports a claim.")
        lines.append("")
    if _is_keyword_only(report):
        lines.append(
            "> ℹ️ **Keyword-only run — not a semantic verification.** Every judge "
            "was the dependency-free `keyword` heuristic (token overlap only, no "
            "API key). It cannot confirm a source *supports* a claim — claim "
            "scores here need a manual spot-check. Re-run with an LLM judge "
            "(`--judge anthropic|openai|gemini`) for semantic verification."
        )
        lines.append("")

    weak = [sc for sc in report.scored if sc.evidence_quality == "weak"]
    if weak:
        lines.append(
            f"> ⚠️ **Agent review gate** — {len(weak)} citation(s) carry weak "
            "evidence flags. Do not auto-approve them from the aggregate score."
        )
        lines.append("")

    # Tier distribution
    dist = report.tier_distribution()
    total = len(report.scored) or 1
    lines.append("## Tier distribution")
    lines.append("")
    lines.append("| Tier | Count | Share |")
    lines.append("|---|---|---|")
    for t in [Tier.A, Tier.B, Tier.C, Tier.F]:
        n = dist.get(t, 0)
        lines.append(f"| {t.symbol} {t.value} | {n} | {round(100 * n / total)}% |")
    lines.append("")

    # Sections, worst first
    by_tier: dict[Tier, list[ScoredCitation]] = {t: [] for t in Tier}
    for sc in report.scored:
        by_tier[sc.tier].append(sc)

    for t in _TIER_ORDER:
        items = by_tier[t]
        if not items:
            continue
        lines.append(f"## {t.symbol} {_TIER_LABEL[t]} — {len(items)}")
        lines.append("")
        if t is Tier.A:
            # A tier: list only (details omitted per spec).
            for sc in items:
                c = sc.citation
                lines.append(f"- `{_excerpt(c.ref, 80)}` (line {c.line}, {sc.score:.0f}/100)")
        else:
            for i, sc in enumerate(items, 1):
                lines.append(f"{i}. {_detail_line(sc)}")
                if sc.review_flags:
                    lines.append(
                        f"   - review flags: {', '.join(sc.review_flags)} "
                        f"(evidence: {sc.evidence_quality})"
                    )
        lines.append("")

    # Needs re-check — citations whose consensus verdict is Uncertain (the judge
    # abstained / judges split without a tie-break). Surfaced for human review.
    uncertain = [sc for sc in report.scored if sc.consensus is Verdict.UNCERTAIN]
    if uncertain:
        lines.append(f"## ❓ Needs re-check (Uncertain) — {len(uncertain)}")
        lines.append("")
        lines.append(
            "> Source was insufficient to decide, or judges split with no tie-break. "
            "Spot-check these by hand."
        )
        lines.append("")
        for sc in uncertain:
            c = sc.citation
            reason = _consensus_reason(sc)
            line = f"- `{_excerpt(c.ref, 80)}` (line {c.line}, {sc.score:.0f}/100)"
            if reason:
                line += f" — {_excerpt(reason)}"
            lines.append(line)
        lines.append("")

    # Inaccessible summary
    inaccessible = [
        sc for sc in report.scored
        if (sc.fetched is None or not sc.fetched.ok or sc.fetched.soft_404_suspect
            or sc.fetched.via_archive or (sc.fetched.landing_status is not None
                                         and not 200 <= sc.fetched.landing_status < 300))
    ]
    if inaccessible:
        archived = sum(1 for sc in inaccessible if sc.fetched and sc.fetched.via_archive)
        lines.append(f"## Inaccessible / fetch issues — {len(inaccessible)}")
        lines.append("")
        lines.append(f"- served via web.archive.org fallback: {archived}")
        lines.append("")
        for sc in inaccessible:
            c = sc.citation
            f = sc.fetched
            status = f.status if f else 0
            err = (f.error if f else "") or f"HTTP {status}"
            if f and f.soft_404_suspect:
                err += "; possible error/placeholder page"
            if f and f.landing_status is not None:
                err += f"; original landing HTTP {f.landing_status}"
            arch = " (archive)" if (f and f.via_archive) else ""
            lines.append(f"- `{_excerpt(c.ref, 80)}` — {err}{arch}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_json(report: Report) -> dict:
    """Render a :class:`Report` to a machine-readable dict (SSoT for JSON output).

    This is the single serializer shared by the CLI ``--json`` flag and the MCP
    server, so the structured shape never diverges between the two surfaces.

    Keys:
        schema_version     str   — output contract version (see SCHEMA_VERSION)
        source_file        str
        level              str   — "L1" | "L2" | "L3"
        profile            str   — active harness profile key, or null
        judges             list  — judge names actually used
        overall_score      float
        overall_tier       str   — "A" | "B" | "C" | "F"
        has_failure        bool  — any citation is tier F
        tier_distribution  dict  — {"A": int, "B": int, "C": int, "F": int}
        "citations          list  — each item is ScoredCitation.to_dict()
                           (including review_flags and evidence_quality)
    """
    dist = report.tier_distribution()
    return {
        "schema_version": SCHEMA_VERSION,
        "source_file": report.source_file,
        "level": report.level,
        "profile": report.profile,
        "judges": list(report.judges),
        "overall_score": report.overall_score,
        "overall_tier": report.overall_tier.value,
        "has_failure": report.has_failure,
        "keyword_only": _is_keyword_only(report),
        "tier_distribution": {t.value: dist.get(t, 0) for t in Tier},
        "citations": [sc.to_dict() for sc in report.scored],
    }
