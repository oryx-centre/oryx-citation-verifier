"""Academic metadata APIs — Crossref / arXiv / NCBI E-utilities (stdlib only).

For academic identifiers (DOI / arXiv / PMID / PMC) these free, official APIs
return **structured metadata** (title, authors, year, abstract) directly. That
bypasses publisher paywalls (you still get the abstract + authors + year even
when the HTML landing page is a paywall stub) and makes author/year scoring
real instead of a fuzzy string match against scraped HTML.

Pure stdlib: ``urllib`` + ``json`` + ``xml.etree``. No third-party deps.

Each ``fetch_*_metadata`` function returns a normalized dict::

    {"title": str, "authors": list[str], "year": int | None, "abstract": str}

or ``None`` when the lookup fails (network error, parse error, or the record
does not exist). Callers treat ``None`` as "fall through to the next source"
(see :mod:`paperverify.fetch` for the explicit fallback chain). These functions
never raise for network / parse problems — they swallow and return ``None`` so a
single bad identifier can never crash a run (No Silent Fallback: the *caller*
records which path actually produced data via ``Fetched.source``).

API contracts relied on (confirmed against live responses, 2026-06):
    Crossref  GET https://api.crossref.org/works/{doi}
              message.title[] · message.author[].{family,given}
              message.issued.date-parts[[YYYY,M,D]] (or .published) · message.abstract (JATS)
    arXiv     GET http://export.arxiv.org/api/query?id_list={id}
              Atom: feed/entry/{title, author/name, summary, published}
    NCBI      GET .../esummary.fcgi?db=pubmed&id={pmid}&retmode=json
              result.{id}.{title, authors[].name, pubdate}
"""

from __future__ import annotations

import json
import html
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional
from xml.etree import ElementTree as ET

# A descriptive User-Agent with a contact mailto puts Crossref/NCBI requests in
# the "polite pool" and satisfies their fair-use guidance.
CONTACT_EMAIL = "paper-verify@users.noreply.github.com"
API_USER_AGENT = f"paper-verify/0.1 (https://github.com/nolainjin/paper-verify; mailto:{CONTACT_EMAIL})"
API_TIMEOUT = 8  # seconds — short, so a slow API does not stall the whole run
# Upper bound on bytes read from any metadata API response. These endpoints
# return small JSON / Atom records (a few KB); a 2 MB cap is generous and stops
# a hostile or misbehaving endpoint from streaming an unbounded body into memory
# (audit P1-8 / SEC-04, DoS). XML parsing below stays on stdlib ElementTree; the
# input is now size-bounded and comes only from the fixed official-API hosts, so
# the classic entity-expansion / billion-laughs vector is mitigated by the cap
# (a defusedxml dependency was considered but rejected to keep the core stdlib-only).
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}

# Per-host throttle for the metadata APIs. NCBI E-utilities allow <=3 req/s
# without a key and arXiv asks for ~3s between calls; the metadata path
# previously had no throttle, so parallel workers could burst past those limits
# and trip rate-limit timeouts (audit P1-9 / SA-02). The default is 0.34s;
# arXiv uses 3s, including retries.
MIN_HOST_INTERVAL = 0.34  # seconds between calls to the same host
ARXIV_HOST_INTERVAL = 3.0
_host_lock = threading.Lock()
_host_last: dict[str, float] = {}


def _reset_host_clock() -> None:
    """Clear the per-host throttle state (test hook)."""
    with _host_lock:
        _host_last.clear()


def _rate_limit(url: str) -> None:
    """Block until ``MIN_HOST_INTERVAL`` has elapsed for this URL's host."""
    host = urllib.parse.urlsplit(url).netloc.lower()
    interval = max(MIN_HOST_INTERVAL, ARXIV_HOST_INTERVAL) if host == "export.arxiv.org" else MIN_HOST_INTERVAL
    if host == "api.semanticscholar.org":
        interval = max(interval, 1.1)
    while True:
        with _host_lock:
            now = time.monotonic()
            wait = interval - (now - _host_last.get(host, 0.0))
            if wait <= 0:
                _host_last[host] = now
                return
        time.sleep(wait)

# JATS / HTML tags that may wrap a Crossref abstract.
_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9:_-]*(?:\s[^<>]*?)?\s*/?>")
_WS_RE = re.compile(r"\s+")


class _NotFound(Exception):
    """The API responded but the record does not exist (HTTP 404) — no retry."""


def _clean_abstract(raw: str) -> str:
    """Strip JATS/HTML tags and collapse whitespace from a Crossref abstract."""
    if not isinstance(raw, str) or not raw:
        return ""
    text = raw
    for _ in range(2):
        text = _TAG_RE.sub(" ", html.unescape(text))
    return _WS_RE.sub(" ", text).strip()


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _get(url: str, *, accept: str = "*/*") -> bytes:
    """GET ``url`` with one retry/backoff on transient errors.

    Raises:
        _NotFound: the API returned HTTP 404 (record absent — do not retry).
        Exception: any other failure after the retry is exhausted; callers
            catch broadly and treat it as "this source produced nothing".
    """
    req = urllib.request.Request(
        url, headers={"User-Agent": API_USER_AGENT, "Accept": accept}
    )
    last_exc: Optional[BaseException] = None
    for attempt in range(2):  # 1 initial try + 1 retry
        _rate_limit(url)  # retries must also honour per-host pacing
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                # Cap the read so a hostile / runaway endpoint cannot exhaust
                # memory (P1-8 / SEC-04). Keep at most the byte cap;
                # truncated JSON/XML may fail parsing.
                return resp.read(MAX_RESPONSE_BYTES + 1)[:MAX_RESPONSE_BYTES]
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise _NotFound(url) from exc
            last_exc = exc
            if exc.code not in _RETRY_STATUSES:
                raise
        except (urllib.error.URLError, OSError) as exc:
            last_exc = exc  # transient: timeout / connection refused
        if attempt == 0:
            time.sleep(0.5)  # small backoff before the single retry
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Crossref
# ---------------------------------------------------------------------------


def fetch_doi_metadata(doi: str) -> Optional[dict]:
    """Fetch structured metadata for a DOI from the Crossref REST API."""
    doi = doi.strip()
    if not doi:
        return None
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="/")
    try:
        raw = _get(url, accept="application/json")
        msg = json.loads(raw.decode("utf-8", errors="replace")).get("message", {})
    except _NotFound:
        return None
    except Exception:  # network / decode / JSON error — fall through
        return None
    if not isinstance(msg, dict):
        return None

    titles = msg.get("title") or []
    title = titles[0] if isinstance(titles, list) and titles else (titles if isinstance(titles, str) else "")

    authors: list[str] = []
    for a in _list(msg.get("author")):
        if not isinstance(a, dict):
            continue
        family = _string(a.get("family"))
        given = _string(a.get("given"))
        name = (f"{given} {family}".strip()) or _string(a.get("name"))
        if name:
            authors.append(name)

    year = _crossref_year(msg.get("issued")) or _crossref_year(msg.get("published"))
    abstract = _clean_abstract(msg.get("abstract") or "")

    title = _string(title)
    if not (title or authors or abstract):
        return None
    metadata = {"title": title.strip(), "authors": authors, "year": year, "abstract": abstract}
    if not abstract and title:
        fallback = fetch_doi_abstract(doi, title)
        if fallback:
            metadata.update(fallback)
    return metadata


def fetch_doi_abstract(doi: str, expected_title: str) -> Optional[dict]:
    """Find a missing abstract, requiring both DOI and title identity.

    Only fixed public API hosts are queried. A failed/ambiguous lookup leaves
    the Crossref metadata intact and the caller abstains if content is missing.
    """
    def normalized(text: str) -> str:
        return ''.join(c.casefold() for c in _clean_abstract(text) if c.isalnum())

    def accept(ident: object, title: object, abstract: object, source: str, url: str) -> Optional[dict]:
        if not isinstance(ident, str) or ident.casefold() != doi.casefold():
            return None
        if not isinstance(title, str) or normalized(title) != normalized(expected_title):
            return None
        text = _clean_abstract(abstract)
        if not text:
            return None
        return {"abstract": text[:50000], "abstract_source": source, "abstract_url": url}

    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urllib.parse.urlencode(
        {"query": 'DOI:"' + doi + '"', "format": "json", "resultType": "core", "pageSize": "5"})
    try:
        data = json.loads(_get(url, accept="application/json"))
        for item in data.get("resultList", {}).get("result", []):
            if isinstance(item, dict):
                result = accept(item.get("doi"), item.get("title"), item.get("abstractText"), "europepmc", url)
                if result:
                    return result
    except Exception:
        pass
    url = "https://api.semanticscholar.org/graph/v1/paper/DOI:" + urllib.parse.quote(doi, safe="") + "?fields=title,abstract,externalIds"
    try:
        data = json.loads(_get(url, accept="application/json"))
        return accept(data.get("externalIds", {}).get("DOI"), data.get("title"), data.get("abstract"), "semantic_scholar", url)
    except Exception:
        return None


def _crossref_year(node: object) -> Optional[int]:
    """Extract the year from a Crossref date node: {"date-parts": [[YYYY,...]]}."""
    if not isinstance(node, dict):
        return None
    parts = node.get("date-parts")
    if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
        try:
            return int(parts[0][0])
        except (TypeError, ValueError, OverflowError):
            return None
    return None


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------


def fetch_arxiv_metadata(arxiv_id: str) -> Optional[dict]:
    """Fetch structured metadata for an arXiv id from the arXiv Atom API."""
    ident = arxiv_id.strip()
    # Accept "arXiv:1706.03762" / "arxiv:1706.03762" / bare id.
    if ":" in ident:
        ident = ident.split(":", 1)[-1].strip()
    if not ident:
        return None
    url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode({"id_list": ident})
    try:
        raw = _get(url, accept="application/atom+xml")
        root = ET.fromstring(raw)
    except _NotFound:
        return None
    except Exception:  # network / XML parse error — fall through
        return None

    entry = root.find("atom:entry", _ARXIV_NS)
    if entry is None:
        return None

    # arXiv returns HTTP 200 with an error <entry> for unknown/malformed ids
    # (id -> arxiv.org/api/errors, title "Error"). Do not treat as a real paper.
    id_el = entry.find("atom:id", _ARXIV_NS)
    if id_el is not None and id_el.text and "arxiv.org/api/errors" in id_el.text:
        return None

    title_el = entry.find("atom:title", _ARXIV_NS)
    title = (title_el.text or "").strip() if title_el is not None else ""

    authors: list[str] = []
    for author_el in entry.findall("atom:author", _ARXIV_NS):
        name_el = author_el.find("atom:name", _ARXIV_NS)
        if name_el is not None and name_el.text:
            authors.append(name_el.text.strip())

    summary_el = entry.find("atom:summary", _ARXIV_NS)
    abstract = _WS_RE.sub(" ", (summary_el.text or "")).strip() if summary_el is not None else ""

    year: Optional[int] = None
    published_el = entry.find("atom:published", _ARXIV_NS)
    if published_el is not None and published_el.text and len(published_el.text) >= 4:
        try:
            year = int(published_el.text[:4])
        except ValueError:
            year = None

    if not (title or authors or abstract):
        return None
    return {"title": title, "authors": authors, "year": year, "abstract": abstract}


# ---------------------------------------------------------------------------
# NCBI E-utilities (PubMed / PMC)
# ---------------------------------------------------------------------------


def fetch_pmid_metadata(pmid: str) -> Optional[dict]:
    """Fetch structured metadata for a PMID from NCBI E-utilities (esummary)."""
    digits = "".join(c for c in pmid if c.isdigit())
    if not digits:
        return None
    params = {
        "db": "pubmed",
        "id": digits,
        "retmode": "json",
        "tool": "paper-verify",
        "email": CONTACT_EMAIL,
    }
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?" + urllib.parse.urlencode(params)
    try:
        raw = _get(url, accept="application/json")
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except _NotFound:
        return None
    except Exception:  # network / decode / JSON error — fall through
        return None

    result = data.get("result") if isinstance(data, dict) else None
    rec = result.get(digits) if isinstance(result, dict) else None
    if not isinstance(rec, dict) or rec.get("error"):
        return None

    title = _string(rec.get("title"))
    authors = [
        _string(a.get("name"))
        for a in _list(rec.get("authors"))
        if isinstance(a, dict) and _string(a.get("name"))
    ]

    year: Optional[int] = None
    m = re.search(r"\b(1[89]\d{2}|20\d{2})\b", _string(rec.get("pubdate")))
    if m:
        year = int(m.group(1))

    # esummary carries no abstract; efetch would, but title+authors+year is
    # enough to make author/year scoring real. Leave abstract empty (the HTML
    # fetch fallback may still populate it for L2/L3 claim matching).
    if not (title or authors):
        return None
    return {"title": title, "authors": authors, "year": year, "abstract": ""}


def fetch_pmc_metadata(pmc_id: str) -> Optional[dict]:
    """Fetch metadata for a PMC id by converting it to a PMID via NCBI idconv."""
    ident = pmc_id.strip()
    if not ident.upper().startswith("PMC"):
        ident = "PMC" + "".join(c for c in ident if c.isdigit())
    if ident == "PMC":
        return None
    params = {
        "ids": ident,
        "format": "json",
        "tool": "paper-verify",
        "email": CONTACT_EMAIL,
    }
    url = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?" + urllib.parse.urlencode(params)
    try:
        raw = _get(url, accept="application/json")
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except _NotFound:
        return None
    except Exception:
        return None
    records = _list(data.get("records")) if isinstance(data, dict) else []
    pmid = None
    for r in records:
        if isinstance(r, dict) and r.get("pmid"):
            pmid = r["pmid"]
            break
    if not pmid:
        return None
    return fetch_pmid_metadata(str(pmid))
