import subprocess

import pytest

from paperverify.judge import CLIJudge, ensure_judge, parse_response, KeywordJudge
from paperverify.models import Citation, Fetched, Judgement, Verdict
from paperverify.score import score_citation


@pytest.mark.parametrize('text', ['', 'Authentication failed', 'I cannot determine a match.', 'REASON: may match'])
def test_unusable_judge_response_abstains(text):
    verdict, reason = parse_response(text)
    assert verdict is Verdict.UNCERTAIN
    assert reason


def test_cli_failure_cannot_supply_positive_verdict(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess([], 1, 'VERDICT: Match', ''))
    assert CLIJudge().evaluate('claim', 'source').verdict is Verdict.UNCERTAIN


def test_cli_diagnostics_are_not_verdicts(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess([], 0, '', 'VERDICT: Match'))
    assert CLIJudge().evaluate('claim', 'source').verdict is Verdict.UNCERTAIN


def test_missing_parent_sdk_is_actionable(monkeypatch):
    def missing(name):
        raise ModuleNotFoundError('google')
    monkeypatch.setattr('paperverify.judge.importlib.util.find_spec', missing)
    with pytest.raises(RuntimeError, match="install the 'gemini' extra"):
        ensure_judge('gemini')


def score(js, tb=None):
    return score_citation(Citation(1, 'URL', 'https://example.org', 'Smith 2020'),
                          Fetched(1, status=200, title='Smith 2020'), js,
                          tiebreak_judgement=tb)


def test_duplicate_judges_cannot_claim_cross_check():
    result = score([Judgement('keyword', Verdict.MATCH), Judgement('keyword', Verdict.MATCH)])
    assert result.breakdown['cross_check'] == 0


@pytest.mark.parametrize('v', [Verdict.INACCESSIBLE, Verdict.UNCERTAIN])
def test_shared_abstention_is_not_corroboration(v):
    assert score([Judgement('a', v), Judgement('b', v)]).breakdown['cross_check'] == 0


def test_failed_arbiter_cannot_resolve_substantive_tie():
    result = score([Judgement('a', Verdict.MATCH), Judgement('b', Verdict.MISMATCH)],
                   Judgement('t', Verdict.INACCESSIBLE))
    assert result.consensus is Verdict.UNCERTAIN


def test_tiebreak_evidence_is_serialized():
    tb = Judgement('t', Verdict.MATCH, 'support found in abstract')
    result = score([Judgement('a', Verdict.MATCH), Judgement('b', Verdict.MISMATCH)], tb)
    assert result.to_dict()['tiebreak_judgement'] == tb.to_dict()


def test_non_supported_claim_tokens_abstain():
    assert KeywordJudge().evaluate('학습 효과가 높아졌다', '학습 효과를 확인하지 못했다').verdict is Verdict.UNCERTAIN
