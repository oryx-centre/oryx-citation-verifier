"""Score externally-gathered evidence with the standard rubric.

An external agent (a web chat, another harness) does fetch + judging with its
own tools, writes an *evidence JSON* (see ``examples/evidence-sample.json``),
and this module converts it to the shared dataclasses and reuses
``score_citation`` + ``Report`` — so external results get exactly the same
100-point rubric as the CLI pipeline. No new scoring logic lives here.
"""

from __future__ import annotations

from .models import Citation, Fetched, Judgement, Report, Verdict
from .score import score_citation

VALID_LEVELS = {"L1", "L2", "L3"}

_VERDICT_NAMES = " | ".join(v.value for v in Verdict)


class EvidenceError(ValueError):
    """Evidence JSON is malformed; the message names the offending citation index."""


def _strings(data: dict, fields: tuple[str, ...], path: str) -> None:
    for key in fields:
        if key in data and not isinstance(data[key], str):
            raise EvidenceError(f"{path}.{key} must be a string")


def _integer(data: dict, key: str, path: str, minimum: int = 0,
             maximum: int | None = None, nullable: bool = False) -> None:
    value = data.get(key)
    if key not in data or (nullable and value is None):
        return
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise EvidenceError(f"{path}.{key} must be an integer in the valid range")


def _judgement(data: object, path: str) -> Judgement:
    if not isinstance(data, dict):
        raise EvidenceError(f"{path} must be an object")
    _strings(data, ("judge", "verdict", "reason"), path)
    if not data.get("judge", "external").strip():
        raise EvidenceError(f"{path}.judge must not be empty")
    raw = data.get("verdict")
    if raw is None:
        raise EvidenceError(f"{path}.verdict is required")
    try:
        verdict = Verdict(raw)
    except ValueError:
        raise EvidenceError(f"{path}.verdict {raw!r} unknown (expected {_VERDICT_NAMES})") from None
    return Judgement(data.get("judge", "external"), verdict, data.get("reason", ""))


def report_from_evidence(data: dict) -> Report:
    """Convert an evidence dict to a scored :class:`Report`.

    Tolerant on optional fields (missing ids are assigned 1..N, missing
    ``fetched`` fields take the dataclass defaults); strict with clear,
    index-named errors on anything that would otherwise be guessed.
    """
    if not isinstance(data, dict):
        raise EvidenceError("evidence root must be a JSON object")
    _strings(data, ("source_file", "level"), "evidence")
    if data.get("profile") is not None and not isinstance(data["profile"], str):
        raise EvidenceError("evidence.profile must be a string or null")
    level = str(data.get("level", "L2")).upper()
    if level not in VALID_LEVELS:
        raise EvidenceError(f"unknown level: {level!r} (expected L1 | L2 | L3)")
    items = data.get("citations")
    if not isinstance(items, list):
        raise EvidenceError("'citations' must be a list")

    scored = []
    seen_ids: set[int] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise EvidenceError(f"citations[{i}] must be an object")

        cd = item.get("citation")
        if not isinstance(cd, dict):
            raise EvidenceError(f"citations[{i}].citation must be an object")
        cd = dict(cd)
        cd.setdefault("id", i + 1)
        cd.setdefault("type", "URL")
        _strings(cd, ("type", "ref", "context"), f"citations[{i}].citation")
        _integer(cd, "id", f"citations[{i}].citation", minimum=1)
        _integer(cd, "line", f"citations[{i}].citation")
        if cd["type"] not in {"URL", "DOI", "arXiv", "PMC", "PMID"}:
            raise EvidenceError(f"citations[{i}].citation.type is unknown")
        if not cd.get("ref"):
            raise EvidenceError(f"citations[{i}].citation.ref is required")
        try:
            citation = Citation.from_dict(cd)
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceError(f"citations[{i}].citation invalid: {exc}") from None
        if not citation.ref.strip():
            raise EvidenceError(f"citations[{i}].citation.ref is required")
        if citation.id in seen_ids:
            raise EvidenceError(f"citations[{i}].citation.id is duplicated: {citation.id}")
        seen_ids.add(citation.id)

        fd = item.get("fetched")
        fetched = None
        if fd is not None:
            if not isinstance(fd, dict):
                raise EvidenceError(f"citations[{i}].fetched must be an object or null")
            fd = dict(fd)
            fd.setdefault("id", citation.id)
            path = f"citations[{i}].fetched"
            _strings(fd, ("title", "abstract", "url_final", "error", "source", "abstract_source", "abstract_url"), path)
            _integer(fd, "id", path, minimum=1)
            _integer(fd, "status", path, maximum=599)
            _integer(fd, "landing_status", path, maximum=599, nullable=True)
            _integer(fd, "year", path, minimum=1, maximum=9999, nullable=True)
            for key in ("via_archive", "soft_404_suspect"):
                if key in fd and type(fd[key]) is not bool:
                    raise EvidenceError(f"{path}.{key} must be a boolean")
            if "authors" in fd and (not isinstance(fd["authors"], list) or
                                     not all(isinstance(a, str) for a in fd["authors"])):
                raise EvidenceError(f"{path}.authors must be a list of strings")
            if fd["id"] != citation.id:
                raise EvidenceError(f"{path}.id must match citation.id")
            try:
                fetched = Fetched.from_dict(fd)
            except (KeyError, TypeError, ValueError) as exc:
                raise EvidenceError(f"citations[{i}].fetched invalid: {exc}") from None

        raw_judgements = item.get("judgements")
        if raw_judgements is None:
            raw_judgements = []
        if not isinstance(raw_judgements, list):
            raise EvidenceError(f"citations[{i}].judgements must be a list")
        judgements = [_judgement(jd, f"citations[{i}].judgements[{k}]")
                      for k, jd in enumerate(raw_judgements)]
        tb = item.get("tiebreak_judgement")
        tiebreak = _judgement(tb, f"citations[{i}].tiebreak_judgement") if tb is not None else None
        scored.append(score_citation(citation, fetched, judgements, level=level,
                                     tiebreak_judgement=tiebreak))

    judges = sorted({j.judge for sc in scored for j in
                     sc.judgements + ([sc.tiebreak_judgement] if sc.tiebreak_judgement else [])})
    return Report(
        source_file=str(data.get("source_file") or "<evidence>"),
        level=level,
        scored=scored,
        judges=judges,
        profile=data.get("profile"),
    )
