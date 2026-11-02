import json
import urllib.error

import pytest

from paperverify import fetch as f, sources, cli
from paperverify.models import Citation, Fetched, Verdict
from paperverify.score import score_citation


def cite(url='https://example.org/a'):
    return Citation(1, 'URL', url, 'claim')


@pytest.mark.parametrize('url', [
    'https://evil.example/arxiv.org/abs/1706.03762',
    'https://arxiv.org.evil.example/abs/1706.03762',
    'https://evil.example/?url=https://pubmed.ncbi.nlm.nih.gov/123/',
    'https://evil.example/10.1234/good',
    'https://evil.example/PMC123/?ncbi.nlm.nih.gov',
])
def test_unrelated_hosts_cannot_borrow_metadata(url):
    assert f._metadata_for(cite(url)) == (None, '')


def test_arxiv_version_is_preserved(monkeypatch):
    monkeypatch.setattr(sources, 'fetch_arxiv_metadata', lambda ident: {'id': ident})
    assert f._metadata_for(cite('https://arxiv.org/pdf/1706.03762v2.pdf')) == ({'id': '1706.03762v2'}, 'arxiv')


def test_l1_detects_soft_404_from_real_body(monkeypatch):
    monkeypatch.setattr(f, '_rate_limit', lambda u: None)
    monkeypatch.setattr(f, '_open', lambda u, m: (200, u, 'text/html', b'<title>Page not found</title>'))
    got = f._fetch_one(cite().ref, 'L1')
    assert got.soft_404_suspect
    assert score_citation(cite(), got, [], level='L1').score == 50


def test_l1_metadata_timeout_is_not_a_live_link(monkeypatch):
    monkeypatch.setattr(f, '_metadata_for', lambda c: ({'title': 'paper'}, 'crossref'))
    def fail(*a):
        raise urllib.error.URLError('timeout')
    monkeypatch.setattr(f, '_open', fail)
    got = f.fetch(cite(), 'L1')
    assert got.landing_status == 0
    assert score_citation(cite(), got, [], level='L1').score == 0


def test_metadata_without_abstract_does_not_treat_page_chrome_as_abstract(monkeypatch):
    monkeypatch.setattr(f, '_metadata_for', lambda c: ({'title': 'paper'}, 'ncbi'))
    body = ('<p>Actual abstract and supporting evidence. ' * 15 + '</p>').encode()
    monkeypatch.setattr(f, '_open', lambda u, m: (200, u, 'text/html', body))
    assert f.fetch(cite(), 'L2').abstract == ''
    assert 'supporting evidence' in f.fetch(cite(), 'L3').abstract
    assert f.fetch(cite(), 'L3').abstract_source == 'http_page'


def test_pdf_bytes_are_not_claim_evidence(monkeypatch):
    monkeypatch.setattr(f, '_rate_limit', lambda u: None)
    monkeypatch.setattr(f, '_open', lambda u, m: (200, u, 'application/pdf', b'%PDF-1.7 random text'))
    got = f._fetch_one(cite().ref, 'L2')
    assert not got.ok
    assert got.abstract == ''
    assert 'unsupported' in got.error


def test_blocked_private_target_is_not_sent_to_archive(monkeypatch):
    monkeypatch.setattr(f, '_metadata_for', lambda c: (None, ''))
    def blocked(*a):
        raise f._BlockedURL('blocked internal address')
    monkeypatch.setattr(f, '_fetch_one', blocked)
    monkeypatch.setattr(f, '_archive_url', lambda u: pytest.fail('private URL leaked to archive'))
    assert 'blocked' in f.fetch(cite('http://127.0.0.1/private')).error


@pytest.mark.parametrize('payload', [[], {'archived_snapshots': [1]}, {'archived_snapshots': {'closest': [1]}}])
def test_malformed_archive_json_does_not_crash(monkeypatch, payload):
    monkeypatch.setattr(f, '_open', lambda u, m: (200, u, 'application/json', json.dumps(payload).encode()))
    assert f._archive_url(cite().ref).startswith('https://web.archive.org/')


@pytest.mark.parametrize('get,ident,payload', [
    (sources.fetch_doi_metadata, '10.1234/x', {'message': {'title': [42], 'author': 5, 'abstract': []}}),
    (sources.fetch_pmid_metadata, '123', []),
    (sources.fetch_pmid_metadata, '123', {'result': {'123': {'title': 1, 'authors': 9}}}),
    (sources.fetch_pmc_metadata, 'PMC123', []),
])
def test_bad_metadata_shapes_return_none(monkeypatch, get, ident, payload):
    monkeypatch.setattr(sources, '_get', lambda *a, **k: json.dumps(payload).encode())
    assert get(ident) is None


def test_title_only_metadata_cannot_confirm_claim(monkeypatch):
    monkeypatch.setattr(cli, 'fetch_all', lambda cs, **k: {c.id: Fetched(c.id, status=200, title='supporting title') for c in cs})
    report = cli.run_pipeline('supporting title https://example.org/a')
    assert report.scored[0].consensus is Verdict.UNCERTAIN


def test_arxiv_requests_are_spaced_by_three_seconds(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sources.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(sources.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    sources._reset_host_clock()
    sources._rate_limit('https://export.arxiv.org/api/query')
    sources._rate_limit('https://export.arxiv.org/api/query')
    assert clock[0] >= 1003.0
