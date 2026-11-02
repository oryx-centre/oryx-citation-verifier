import json
import pytest
from paperverify import sources
from paperverify.models import Fetched
from tools.live_corpus_check import assess


def test_europepmc_abstract_requires_doi_and_title(monkeypatch):
    items=[{'doi':'10.1/wrong','title':'Study','abstractText':'Wrong text'},
           {'doi':'10.1/right','title':'Other paper','abstractText':'Wrong title'},
           {'doi':'10.1/right','title':'Study.','abstractText':'<p>p &lt; .05; n &gt; 20</p>'}]
    monkeypatch.setattr(sources,'_get',lambda *a,**k: json.dumps({'resultList':{'result':items}}).encode())
    result=sources.fetch_doi_abstract('10.1/right','Study')
    assert result['abstract']=='p < .05; n > 20'
    assert result['abstract_source']=='europepmc'
    assert result['abstract_url'].startswith('https://www.ebi.ac.uk/')


def test_s2_fallback_checks_identity_and_retains_provenance(monkeypatch):
    def get(url,**kwargs):
        if 'ebi.ac.uk' in url:return b'{}'
        return json.dumps({'externalIds':{'DOI':'10.1/RIGHT'},'title':'Study','abstract':'Evidence'}).encode()
    monkeypatch.setattr(sources,'_get',get)
    assert sources.fetch_doi_abstract('10.1/right','Study')['abstract_source']=='semantic_scholar'
    assert sources.fetch_doi_abstract('10.1/other','Study') is None
    assert sources.fetch_doi_abstract('10.1/right','Different study') is None


@pytest.mark.parametrize('payload',[{},[],{'resultList':None},{'externalIds':None}, {'abstract':'unverified'}])
def test_malformed_fallback_abstains(monkeypatch,payload):
    monkeypatch.setattr(sources,'_get',lambda *a,**k:json.dumps(payload).encode())
    assert sources.fetch_doi_abstract('10.1/right','Study') is None


def test_fallback_failure_preserves_crossref(monkeypatch):
    def get(url,**kwargs):
        if 'crossref.org' in url:return b'{"message":{"title":["Study"],"author":[{"family":"Smith"}]}}'
        raise OSError('offline')
    monkeypatch.setattr(sources,'_get',get)
    result=sources.fetch_doi_metadata('10.1/right')
    assert result['title']=='Study' and result['abstract']==''


def test_abstract_math_and_double_encoded_markup_survive():
    assert sources._clean_abstract('&lt;p&gt;p &lt; .001 and x &gt; 2&lt;/p&gt;')=='p < .001 and x > 2'


def test_provenance_round_trip():
    f=Fetched(1,abstract='Evidence',abstract_source='semantic_scholar',abstract_url='https://api.semanticscholar.org/example')
    assert Fetched.from_dict(f.to_dict()) == f


def test_live_checker_does_not_pass_empty_or_wrong_metadata():
    assert not all(assess({},'Study').values())
    result={'keyword_only':True,'citations':[{'fetched':{'status':200,'title':'Wrong','abstract':''},'effective_verdict':'Match','evidence_quality':'strong'}]}
    checks=assess(result,'Study')
    assert not checks['expected_metadata_found']
    assert not checks['no_heuristic_strong_evidence']
    assert not checks['missing_content_abstains']


@pytest.mark.parametrize('cited,registered',[
    ('G*Power 3: A flexible statistical power analysis program for the social, behavioral, and biomedical sciences',
     'Statistical power analyses using G*Power 3.1: Tests for correlation and regression analyses'),
    ('Research on attention networks as a model for the integration of cognitive neuroscience and clinical psychology',
     'Research on Attention Networks as a Model for the Integration of Psychological Science'),
])
def test_registered_doi_does_not_validate_a_different_cited_title(cited,registered):
    result={'keyword_only':True,'citations':[{'fetched':{'status':200,'title':registered,'abstract':'Metadata'},
              'effective_verdict':'Partial','evidence_quality':'review'}]}
    assert assess(result,cited)['expected_metadata_found'] is False


def test_complete_crossref_abstract_does_not_call_fallback(monkeypatch):
    monkeypatch.setattr(sources,'_get',lambda *a,**k:b'{"message":{"title":["Study"],"abstract":"Evidence"}}')
    monkeypatch.setattr(sources,'fetch_doi_abstract',lambda *a:pytest.fail('unnecessary fallback'))
    assert sources.fetch_doi_metadata('10.1/right')['abstract']=='Evidence'
