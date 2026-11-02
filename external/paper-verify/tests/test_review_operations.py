import json
import subprocess
from pathlib import Path

import pytest

from paperverify import cli, mcp_server
from tools import feature_gate


def test_cli_read_error_has_exit_2(monkeypatch, tmp_path, capsys):
    path = tmp_path / 'doc.md'
    path.write_text('text')
    def fail(*a, **k):
        raise PermissionError('cannot read input')
    monkeypatch.setattr(Path, 'read_text', fail)
    assert cli.main([str(path), '--json']) == 2
    assert capsys.readouterr().out == ''


def test_invalid_utf8_evidence_has_exit_2(tmp_path, capsys):
    path = tmp_path / 'bad.json'
    path.write_bytes(b'\xff')
    assert cli.main(['--from-evidence', str(path), '--json']) == 2
    assert capsys.readouterr().out == ''


def test_output_directory_failure_has_exit_2(tmp_path, capsys):
    path = tmp_path / 'doc.md'
    path.write_text('no citations')
    out = tmp_path / 'existing-file'
    out.write_text('preserve me')
    assert cli.main([str(path), '--level', 'L1', '--out', str(out), '--json']) == 2
    assert out.read_text() == 'preserve me'
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('flags', [['--extract-only'], ['--level', 'L1', '--json']])
def test_offline_and_l1_skip_provider_probes(monkeypatch, tmp_path, flags):
    path = tmp_path / 'doc.md'
    path.write_text('no citations')
    monkeypatch.setattr(cli, '_resolve_profile_judges', lambda r: pytest.fail('unexpected provider probe'))
    assert cli.main([str(path), '--profile', 'gemini'] + flags) == 0


def test_conflicting_modes_fail():
    assert cli.main(['--extract-only', '--from-evidence', 'x']) == 2


@pytest.mark.parametrize('workers', [0, -1, 100000, True, '4'])
def test_workers_are_bounded_before_fetch(workers):
    with pytest.raises(ValueError, match='workers'):
        cli.run_pipeline('', workers=workers)


def test_mcp_oversized_text_rejected(monkeypatch):
    monkeypatch.setattr(mcp_server, '_MAX_FILE_BYTES', 4)
    assert 'error' in mcp_server.verify_text_impl('가나다')


def test_mcp_invalid_path_and_input_are_structured():
    assert 'error' in mcp_server.verify_file_impl(None)
    assert 'error' in mcp_server.verify_text_impl(None)
    assert 'error' in mcp_server.verify_text_impl('', judges='keyword')


def feature(ident='a', **over):
    return dict(id=ident, name=ident, behavior='works', verification='exit 0',
                state='not-started', dependencies=[], **over)


def test_unknown_gate_selection_fails(monkeypatch, tmp_path):
    path = tmp_path / 'features.json'
    path.write_text(json.dumps({'features': [feature()]}))
    monkeypatch.setattr(feature_gate, 'FEATURE_FILE', path)
    assert feature_gate.main(['--only', 'typo']) == 2


@pytest.mark.parametrize('features', [[], [feature(), feature()], [dict(feature(), state='imaginary')],
    [dict(feature(), state='active'), dict(feature('b'), state='active')],
    [dict(feature(), dependencies=['missing'])], [dict(feature(), dependencies=['a'])]])
def test_gate_rejects_invalid_feature_graph(features):
    with pytest.raises(ValueError):
        feature_gate._validated_features({'features': features})


def test_gate_timeout_is_failure(monkeypatch):
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired('verify', 600)
    monkeypatch.setattr(feature_gate.subprocess, 'run', timeout)
    ok, reason = feature_gate.run_verification('verify')
    assert not ok
    assert 'TimeoutExpired' in reason


def test_gate_cannot_promote_past_failed_dependency(monkeypatch, tmp_path):
    path = tmp_path / 'features.json'
    path.write_text(json.dumps({'features': [feature(), dict(feature('b'), dependencies=['a'])]}))
    monkeypatch.setattr(feature_gate, 'FEATURE_FILE', path)
    monkeypatch.setattr(feature_gate, 'run_verification', lambda cmd: (True, 'ok'))
    assert feature_gate.main(['--only', 'b', '--sync']) == 0
    assert json.loads(path.read_text())['features'][1]['state'] == 'not-started'
