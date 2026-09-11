import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import main
import ai_preprocessing as ai
from run_logging import RunMetrics, token_usage
from test_tenants import env, client


def test_defaults_and_admin_only_settings(env):
    assert main.load_config()['log_debug'] is False
    assert main.load_config()['log_retention_days'] == 90
    assert client('alice@example.com').post('/logs/settings', data={'log_debug': 'on', 'log_retention_days': '1'}).status_code == 403
    admin = client('admin@example.com')
    for days in ('0', '-1', '3651', 'x'):
        assert admin.post('/logs/settings', data={'log_retention_days': days}).status_code == 400
    assert main.load_config()['log_retention_days'] == 90
    assert admin.post('/logs/settings', data={'log_debug': 'on', 'log_retention_days': '30'}).status_code == 303
    assert main.load_config()['log_debug'] is True
    assert main.load_config()['log_retention_days'] == 30
    assert admin.post('/logs/settings', data={'log_retention_days': '90'}).status_code == 303
    assert main.load_config()['log_debug'] is False


def test_usage_missing_is_not_zero_and_no_double_count():
    assert token_usage('Mistral', {}) == {}
    assert token_usage('Mistral', {'usage': {'prompt_tokens': True, 'completion_tokens': -1, 'total_tokens': '12'}}) == {}
    assert token_usage('Gemini', {'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 10,
        'thoughtsTokenCount': 20, 'cachedContentTokenCount': 40, 'totalTokenCount': 130}}) == {
            'input': 100, 'output': 10, 'reasoning': 20, 'cached': 40, 'total': 130}
    metrics = RunMetrics({'bPretraitementIA': True})
    metrics.event({'usage': {'input': 100, 'total': 130}, 'verdict': 'spam'})
    metrics.event({'usage': {}, 'verdict': 'legitimate'})
    metrics.event({'quarantined': True})
    text = metrics.summary('Succès')
    assert 'total : 130 (partiel : 1/2 appels)' in text
    assert 'sortie : 0' not in text
    assert 'Nb frauduleux/spams détectés : 1' in text and 'Nb déplacés : 1' in text


@pytest.mark.parametrize('engine', ['Mistral', 'Gemini'])
def test_provider_usage_survives_invalid_verdict(monkeypatch, engine):
    envelope = ({'usage': {'prompt_tokens': 12, 'completion_tokens': 3, 'total_tokens': 15},
                 'choices': [{'finish_reason': 'stop', 'message': {'content': '{"verdict":"spam"}'}}]}
                if engine == 'Mistral' else {'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 3, 'totalTokenCount': 15},
                    'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '{"verdict":"spam"}'}]}}]})
    class Response:
        def raise_for_status(self): pass
        def json(self): return envelope
    monkeypatch.setattr(ai.requests, 'post', lambda *a, **kw: Response())
    result = ai.classify(engine, 'secret', {})
    assert result == 'spam' and result.usage['total'] == 15
    if engine == 'Mistral':
        envelope['choices'] = []
    else:
        envelope['candidates'] = []
    with pytest.raises(ai.PreprocessingError) as error:
        ai.classify(engine, 'secret', {})
    assert error.value.usage['total'] == 15


def test_stream_counter_survives_chunking_and_tail_truncation():
    metrics = RunMetrics({})
    metrics.feed(b'noise\nMessages trans')
    metrics.feed(b'ferred       : 42 (1.2 msg/s)\n' + b'x' * 100000)
    metrics.feed(b'\n')
    assert metrics.data['transferred'] == 42
    metrics.feed(b'Messages transferred : 0')
    metrics.feed(b'', final=True)
    assert metrics.data['transferred'] == 0


@pytest.mark.parametrize('debug', [False, True])
def test_execution_log_levels_always_include_tokens_and_counts(env, monkeypatch, debug):
    config = main.load_config()
    config['log_debug'] = debug
    main.save_config(config)
    account = config['accounts'][0]
    account.update(bPretraitementIA=True, sMoteurIA='Mistral')
    async def preprocess(*args):
        progress = args[-1]
        progress({'usage': {'input': 20, 'output': 5, 'total': 25}, 'verdict': 'spam'})
        progress({'quarantined': True})
        progress('UID 1 : spam')
        live = main.load_config()['runs'][-1]
        assert 'total : 25' in live['log']
        assert ('UID 1' in live['log']) is debug
    class Process:
        returncode = 0
        stdout = type('Output', (), {'read': AsyncMock(side_effect=[b'DETAILED IMAP noise\nsecret\nMessages transferred : 8\n', b''])})()
        wait = AsyncMock()
    monkeypatch.setattr(main, 'preprocess', preprocess)
    monkeypatch.setattr(main.asyncio, 'create_subprocess_exec', AsyncMock(return_value=Process()))
    main.processes['a'] = {'process': None, 'cancelled': False}
    asyncio.run(main.execute(account, {'pseudo': 'Alice'}))
    run = main.load_config()['runs'][-1]
    assert run['status'] == 'Succès'
    assert 'Emails transférés par imapsync : 8.' in run['log']
    assert 'Nb frauduleux/spams détectés : 1' in run['log'] and 'total : 25' in run['log']
    assert ('DETAILED IMAP noise' in run['log']) is debug
    assert 'secret' not in run['log']
    assert run['metrics']['transferred'] == 8


def test_failed_ai_usage_in_minimal_and_daily_error_logs(env, monkeypatch):
    account = main.load_config()['accounts'][0]
    account.update(bPretraitementIA=True)
    async def fail(*args):
        args[-1]({'usage': {'total': 19}})
        raise ai.PreprocessingError('Réponse IA invalide')
    monkeypatch.setattr(main, 'preprocess', fail)
    main.processes['a'] = {'process': None, 'cancelled': False}
    asyncio.run(main.execute(account, {'pseudo': 'Alice'}))
    run = main.load_config()['runs'][-1]
    assert run['status'] == 'Erreur' and 'total : 19' in run['log']
    assert 'Réponse IA invalide' in run['log']
    assert 'total : 19' in main.CONFIG_FILE.with_name('daily_errors.log').read_text(encoding='utf-8')


def test_rotation_by_finish_date_preserves_active_and_other_data(env):
    now = datetime(2026, 9, 11, tzinfo=timezone.utc)
    old = (now - timedelta(days=91)).isoformat()
    cutoff = (now - timedelta(days=90)).isoformat()
    config = main.load_config()
    config['runs'] = [
        {'id': 'expired', 'status': 'Succès', 'started': old},
        {'id': 'boundary', 'status': 'Succès', 'started': cutoff},
        {'id': 'long', 'status': 'Succès', 'started': old, 'finished': now.isoformat()},
        {'id': 'active', 'status': 'Synchronisation...', 'started': old},
        {'id': 'unknown', 'status': 'Succès', 'started': 'invalid'},
        {'id': 'reserved', 'status': 'Erreur', 'started': old}]
    main.processes['a'] = {'run_id': 'reserved'}
    main.save_config(config)
    state = main.CONFIG_FILE.with_name('ai_state.json')
    state.write_text('{"private-state": {"1":"done"}}', encoding='utf-8')
    report = main.CONFIG_FILE.with_name('daily_errors.log')
    report.write_text(f'[{old}] expired\n[{now.isoformat()}] fresh\n', encoding='utf-8')
    assert main.rotate_logs(now) == 1
    fresh = main.load_config()
    assert len(fresh['runs']) == 5
    assert fresh['accounts'] == config['accounts'] and fresh['users'] == config['users']
    assert state.read_text(encoding='utf-8') == '{"private-state": {"1":"done"}}'
    assert 'expired' not in report.read_text(encoding='utf-8')
    assert main.rotate_logs(now) == 0


def test_settings_purges_immediately(env):
    config = main.load_config()
    config['runs'].append({'id': 'expired', 'status': 'Succès', 'started': '2000-01-01T00:00:00+00:00'})
    main.save_config(config)
    client('admin@example.com').post('/logs/settings', data={'log_retention_days': '90'})
    assert not any(run['id'] == 'expired' for run in main.load_config()['runs'])


def test_hourly_rotation_independent_of_sync(monkeypatch):
    calls = []
    monkeypatch.setattr(main, 'rotate_logs', lambda: calls.append('purge'))
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError])
    monkeypatch.setattr(main.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.log_rotation_loop())
    assert calls == ['purge']
    assert sleep.call_args.args == (3600,)


@pytest.mark.parametrize('debug', [False, True])
def test_mistral_multiple_responses_accumulate_in_log(env, monkeypatch, debug):
    config = main.load_config()
    config['log_debug'] = debug
    main.save_config(config)
    account = config['accounts'][0]
    account.update(bActiverSynchro=False, bPretraitementIA=True, sMoteurIA='Mistral')
    responses = iter([
        {'prompt_tokens': 25, 'completion_tokens': 10, 'total_tokens': 35},
        {'prompt_tokens': 40, 'completion_tokens': 15, 'total_tokens': 55}])
    def post(url, **kwargs):
        assert url == 'https://api.mistral.ai/v1/chat/completions'
        assert kwargs['json']['stream'] is False
        usage = next(responses)
        class Response:
            def raise_for_status(self): pass
            def json(self):
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"verdict":"legitimate"}'}}], 'usage': usage}
        return Response()
    monkeypatch.setattr(ai.requests, 'post', post)
    async def preprocess(*args):
        for index in range(2):
            result = await asyncio.to_thread(ai.classify, 'Mistral', 'test-key', {'subject': 'test'})
            args[-1]({'usage': result.usage, 'verdict': str(result)})
    monkeypatch.setattr(main, 'preprocess', preprocess)
    main.processes['a'] = {'process': None, 'cancelled': False}
    asyncio.run(main.execute(account, {'pseudo': 'Alice'}))
    run = main.load_config()['runs'][-1]
    assert run['status'] == 'Succès'
    assert run['metrics']['usage'] == {'input': 65, 'output': 25, 'total': 90}
    assert 'cumul de cette exécution (2 appels)' in run['log']
    assert 'entrée : 65 ; sortie : 25 ; total : 90' in run['log']
    assert 'partiel' not in run['log']
