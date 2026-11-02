import urllib.error

from paperverify import cli, sources
from paperverify.extract import extract
from paperverify.judge import KeywordJudge
from paperverify.models import Citation, Fetched, Judgement, Verdict
from paperverify.score import _author_year_credit


def test_reference_url_does_not_change_keyword_verdict():
    claim = 'retention improved across the study cohort'
    source = 'retention improved across the study cohort'
    judge = KeywordJudge()
    assert judge.evaluate(claim, source).verdict is Verdict.MATCH
    assert judge.evaluate(claim + ' https://example.org/10.1234/not-evidence-1980', source).verdict is Verdict.MATCH


def test_adjacent_paragraph_cannot_supply_claim_context():
    text = ('Smith 2020 found improvement. https://example.org/first\n\n'
            'Jones 1999 found decline. https://example.org/second')
    first, second = extract(text)
    assert 'Jones' not in first.context
    assert 'Smith' not in second.context
    assert '1999' not in first.context


def test_url_year_is_not_author_year_evidence():
    citation = Citation(1, 'URL', 'https://example.org/2020', 'Smith https://example.org/2020')
    points, _ = _author_year_credit(citation, Fetched(1, status=200, authors=['Smith'], year=2020))
    assert points == 10


def test_numeric_only_or_korean_claim_does_not_become_match():
    judge = KeywordJudge()
    assert judge.evaluate('2020년 학습이 개선됐다', '2020년 학습이 악화됐다').verdict is Verdict.UNCERTAIN
    assert judge.evaluate('2020 12345', '2020 12345').verdict is Verdict.UNCERTAIN


def test_judges_receive_structured_publication_metadata(monkeypatch):
    monkeypatch.setattr(cli, 'fetch_all', lambda cs, **k: {
        c.id: Fetched(c.id, status=200, title='Study', authors=['Smith'], year=2020, abstract='The intervention improved retention.')
        for c in cs})
    class Judge:
        name = 'test'
        def evaluate(self, claim, source):
            assert 'Authors: Smith' in source
            assert 'Publication year: 2020' in source
            assert 'intervention improved retention' in source
            return Judgement(self.name, Verdict.MATCH)
    monkeypatch.setattr(cli, 'make_judge', lambda spec: Judge())
    assert cli.run_pipeline('Smith 2020 https://example.org').scored[0].consensus is Verdict.MATCH


def test_arxiv_retry_respects_actual_request_spacing(monkeypatch):
    clock, times = [1000.0], []
    monkeypatch.setattr(sources.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(sources.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def fail(req, **kwargs):
        times.append(clock[0])
        raise urllib.error.HTTPError(req.full_url, 503, 'busy', {}, None)
    monkeypatch.setattr(sources.urllib.request, 'urlopen', fail)
    sources._reset_host_clock()
    try:
        sources._get('https://export.arxiv.org/api/query')
    except urllib.error.HTTPError:
        pass
    assert len(times) == 2
    assert times[1] - times[0] >= 3


def test_internal_capital_surname_matches_metadata():
    citation = Citation(1, 'DOI', '10.1038/nature14539', 'LeCun et al. (2015) discuss deep learning.')
    points, _ = _author_year_credit(citation, Fetched(1, status=200, authors=['Yann LeCun'], year=2015))
    assert points == 20


def test_markdown_link_does_not_absorb_following_prose():
    cites = extract('[paper](https://example.org/Study_(2020)).Next sentence.')
    assert cites[0].ref == 'https://example.org/Study_(2020)'
