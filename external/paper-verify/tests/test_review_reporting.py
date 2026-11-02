from paperverify.extract import extract
from paperverify.models import Citation, Fetched, Judgement, Report, Verdict
from paperverify.report import render_markdown
from paperverify.score import score_citation
from paperverify import fetch as f


def test_balanced_parentheses_in_markdown_url_preserved():
    cites = extract('See [paper](https://example.org/Study_(2020)).')
    assert len(cites) == 1
    assert cites[0].ref == 'https://example.org/Study_(2020)'


def test_doi_balanced_suffix_preserved_and_deduplicated():
    cites = extract('https://doi.org/10.1234/abc(def) and 10.1234/abc(def)')
    assert len(cites) == 1
    assert cites[0].ref.endswith('abc(def)')


def test_url_path_case_is_not_deduplicated():
    assert len(extract('https://example.org/Study https://example.org/study')) == 2


def test_url_host_case_still_deduplicates():
    assert len(extract('HTTPS://EXAMPLE.ORG/Study https://example.org/Study')) == 1


def test_bare_arxiv_version_retained():
    assert extract('arXiv:1706.03762v2')[0].ref == 'arXiv:1706.03762v2'


def scored(js, tb=None, fetched=None, level='L2'):
    return score_citation(Citation(1, 'URL', 'https://example.org', 'claim'),
                          fetched or Fetched(1, status=200, abstract='evidence'), js,
                          tiebreak_judgement=tb, level=level)


def test_report_exposes_both_opinions_and_arbiter():
    item = scored([Judgement('first', Verdict.MATCH, 'a' * 200), Judgement('second', Verdict.MISMATCH, 'opposed')],
                  Judgement('arbiter', Verdict.PARTIAL, 'some support'))
    text = render_markdown(Report('test.md', 'L2', [item]))
    assert 'effective verdict: Partial' in text
    assert 'first: Match' in text
    assert 'second: Mismatch' in text
    assert 'tie-break arbiter: Partial; reason: some support' in text


def test_failure_report_does_not_call_inaccessible_evidence_fabricated():
    item = scored([], fetched=Fetched(1, status=404, error='not found'))
    text = render_markdown(Report('test.md', 'L2', [item]))
    assert 'unsupported / fabricated' not in text
    assert 'does not establish fabrication' in text


def test_empty_and_l1_reports_explain_scope():
    text = render_markdown(Report('test.md', 'L1'))
    assert 'No citations found' in text
    assert 'Reachability only' in text


def test_successful_archive_is_visible_and_does_not_revive_l1_link(monkeypatch):
    monkeypatch.setattr(f, '_metadata_for', lambda c: (None, ''))
    def load(url, level):
        if 'archive.org' not in url:
            import urllib.error
            raise urllib.error.HTTPError(url, 404, 'gone', {}, None)
        return Fetched(0, status=200, abstract='archived evidence')
    monkeypatch.setattr(f, '_fetch_one', load)
    monkeypatch.setattr(f, '_archive_url', lambda u: 'https://web.archive.org/web/20200101/' + u)
    c = Citation(1, 'URL', 'https://example.org', 'claim')
    fetched = f.fetch(c, 'L1')
    item = score_citation(c, fetched, [], level='L1')
    assert item.score == 0
    text = render_markdown(Report('test.md', 'L1', [item]))
    assert 'served via web.archive.org fallback: 1' in text
    assert 'original landing HTTP 404' in text
