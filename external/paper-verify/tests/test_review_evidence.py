import copy

import pytest

from paperverify.offline import EvidenceError, report_from_evidence
from paperverify.models import Verdict


def evidence():
    return {'citations': [{'citation': {'id': 1, 'type': 'URL', 'ref': 'https://example.org'},
                           'fetched': {'id': 1, 'status': 200, 'abstract': 'evidence'},
                           'judgements': [{'judge': 'a', 'verdict': 'Match'}]}]}


@pytest.mark.parametrize('section,key,value', [
    ('citation', 'ref', 123), ('citation', 'context', []),
    ('citation', 'id', True), ('citation', 'id', 1.5),
    ('citation', 'id', float('inf')), ('citation', 'line', -1),
    ('citation', 'type', 'UNKNOWN'), ('citation', 'ref', '   '),
    ('fetched', 'id', 2), ('fetched', 'status', '200'),
    ('fetched', 'year', True), ('fetched', 'year', float('nan')),
    ('fetched', 'authors', 'Smith'), ('fetched', 'authors', [1]),
    ('fetched', 'title', []), ('fetched', 'abstract', None),
    ('fetched', 'via_archive', 'false'), ('fetched', 'soft_404_suspect', 0),
    ('fetched', 'landing_status', float('inf')),
])
def test_bad_fields_raise_indexed_evidence_error(section, key, value):
    data = evidence()
    data['citations'][0][section][key] = value
    with pytest.raises(EvidenceError, match=r'citations\[0\]'):
        report_from_evidence(data)


@pytest.mark.parametrize('bad', [True, 42, {}, 'Match'])
def test_judgements_must_be_a_list(bad):
    data = evidence()
    data['citations'][0]['judgements'] = bad
    with pytest.raises(EvidenceError, match=r'citations\[0\].judgements'):
        report_from_evidence(data)


def test_duplicate_ids_rejected():
    data = evidence()
    data['citations'].append(copy.deepcopy(data['citations'][0]))
    with pytest.raises(EvidenceError, match=r'citations\[1\].citation.id is duplicated'):
        report_from_evidence(data)


def test_tiebreak_survives_offline_round_trip():
    data = evidence()
    item = data['citations'][0]
    item['judgements'].append({'judge': 'b', 'verdict': 'Mismatch'})
    item['tiebreak_judgement'] = {'judge': 'arbiter', 'verdict': 'Match', 'reason': 'abstract supports claim'}
    report = report_from_evidence(data)
    assert report.scored[0].consensus is Verdict.MATCH
    assert report.scored[0].to_dict()['tiebreak_judgement'] == item['tiebreak_judgement']
    assert 'arbiter' in report.judges


def test_validation_does_not_mutate_evidence():
    data = evidence()
    original = copy.deepcopy(data)
    report_from_evidence(data)
    assert data == original
