"""Citation extraction from Markdown / plain-text documents.

Ports the regex patterns from the original ``extract-citations.py`` into a
single :func:`extract` function that returns :class:`~paperverify.models.Citation`
objects, deduped by ``(type, ref)``, each carrying ~100 chars of surrounding
context and a 1-based line number.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from urllib.parse import urlsplit, urlunsplit

from .models import Citation

# Reference patterns, in priority order. The first pattern that owns a given
# substring wins; later patterns dedupe against earlier ones via (type, ref).
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("URL", re.compile(r"https?://[^\s\]\"<>]+(?<![.,;:!?])", re.I)),
    ("DOI", re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.I)),
    ("PMC", re.compile(r"\bPMC\d+\b")),
    ("PMID", re.compile(r"\bPMID:?\s*\d+\b", re.I)),
    ("arXiv", re.compile(r"\barXiv:\s*\d{4}\.\d{4,5}(?:v\d+)?", re.I)),
]

CTX_CHARS = 300  # enough surrounding text for modern long-context judges
MAX_CONTEXT_CHARS = 2400  # hard cap for agent/LLM prompts

# A DOI carried inside a (dx.)doi.org URL — used to dedupe a bare DOI elsewhere
# in the document against its own resolver URL (same physical source, CL-1/M2).
_DOI_IN_URL_RE = re.compile(
    r"^https?://(?:dx\.)?doi\.org/(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)$", re.I
)


def _line_index(text: str) -> list[int]:
    """Return the start offset of every line (offset 0 starts line 1)."""
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _trim_ref(ref: str) -> str:
    depth = 0
    for index, char in enumerate(ref):
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                ref = ref[:index]
                break
            depth -= 1
    return ref.rstrip(".,;:!?")


def _ref_key(type_name: str, ref: str) -> tuple[str, str]:
    if type_name == "URL":
        try:
            parts = urlsplit(ref)
            # Scheme/host are case-insensitive; resource path/query are not.
            return type_name, urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                                         parts.path, parts.query, parts.fragment))
        except ValueError:
            return type_name, ref
    return type_name, ref.lower()


def strip_references(text: str) -> str:
    """Remove reference syntax before treating context as claim content."""
    for _kind, pattern in PATTERNS:
        text = pattern.sub(" ", text)
    return text


def _pos_to_line(line_starts: list[int], pos: int) -> int:
    """Map a character offset to its 1-based line number (binary search)."""
    lo, hi = 0, len(line_starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if line_starts[mid] <= pos:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def extract(text: str) -> list[Citation]:
    """Extract all citations from ``text``, deduped by ``(type, ref)``.

    Args:
        text: full document contents.

    Returns:
        A list of :class:`Citation` in extraction order (URLs first, then
        DOI / PMC / PMID / arXiv), with ids assigned 1..N.
    """
    line_starts = _line_index(text)
    paragraph_breaks = list(re.finditer(r"\n\s*\n", text))
    break_starts = [b.start() for b in paragraph_breaks]
    break_ends = [b.end() for b in paragraph_breaks]
    seen: set[tuple[str, str]] = set()
    out: list[Citation] = []
    cid = 0
    url_spans: list[tuple[int, int]] = []  # spans claimed by URL matches

    for type_name, pat in PATTERNS:
        for m in pat.finditer(text):
            if type_name == "URL":
                url_spans.append((m.start(), m.end()))
            # A DOI/PMC/PMID/arXiv sitting inside an already-matched URL is the
            # same physical source (fetch._metadata_for routes URL-borne ids),
            # so don't count it as a second citation (CL-1).
            elif any(s <= m.start() and m.end() <= e for s, e in url_spans):
                continue
            ref = _trim_ref(m.group(0))
            key = _ref_key(type_name, ref)
            if key in seen:
                continue
            seen.add(key)
            # A (dx.)doi.org URL *is* a DOI resolver: pre-claim the embedded DOI
            # so the same DOI written bare elsewhere dedupes against this URL
            # rather than counting as a second citation (M2). A *different* bare
            # DOI keeps its own key and is unaffected.
            if type_name == "URL":
                doi_m = _DOI_IN_URL_RE.match(ref)
                if doi_m:
                    seen.add(("DOI", _trim_ref(doi_m.group(1)).lower()))
            cid += 1
            start = max(0, m.start() - CTX_CHARS)
            end = min(len(text), m.end() + CTX_CHARS)
            # Context must not borrow a different paragraph's claim. Keep the
            # established bounded window within the current paragraph.
            before = bisect_right(break_ends, m.start()) - 1
            after = bisect_left(break_starts, m.end())
            if before >= 0:
                start = max(start, break_ends[before])
            if after < len(break_starts):
                end = min(end, break_starts[after])
            context = text[start:end].replace("\n", " ").strip()
            if len(context) > MAX_CONTEXT_CHARS:
                context = context[:MAX_CONTEXT_CHARS].rstrip() + "…"
            out.append(
                Citation(
                    id=cid,
                    type=type_name,
                    ref=ref,
                    context=context,
                    line=_pos_to_line(line_starts, m.start()),
                )
            )
    return out
