import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
import requests

import ai_preprocessing as ai
import main
import rspamd_client as rc
from run_logging import RunMetrics
from test_ai_preprocessing import Mailbox
from test_tenants import env, client
from test_mistral_rate import rate_clock


def response(score=10, **extra):
    return dict(is_skipped=False, score=score, action='add header',
                symbols={'PHISHING': {'score': 4, 'options': ['private@example.com']}}, **extra)


def fake_http(monkeypatch, body, status=200):
    calls = []
    class Response:
        status_code = status
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size): yield json.dumps(body).encode()
    class Session:
        trust_env = True
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            assert not self.trust_env
            calls.append((url, kwargs))
            return Response()
    monkeypatch.setattr(rc.requests, 'Session', Session)
    return calls


def test_http_transport_and_private_diagnostics(monkeypatch):
    calls = fake_http(monkeypatch, response())
    result = rc.scan(b'Subject: private\r\n\r\nbody', rc.DEFAULTS)
    assert result['verdict'] == 'rspamd_spam'
    assert result['symbols'] == [('PHISHING', 4)]
    assert 'private@example.com' not in str(result)
    url, sent = calls[0]
    assert url == 'http://rspamd:11333/checkv2'
    assert sent['data'].endswith(b'body')
    assert sent['allow_redirects'] is False and sent['stream'] is True
    assert sent['headers']['Flags'] == 'no_log'
    assert 'IP' not in sent['headers'] and 'From' not in sent['headers']
    assert set(json.loads(sent['headers']['Settings'])['symbols_disabled']) == {'SPF_CHECK', 'DMARC_CHECK'}


@pytest.mark.parametrize('body', [{}, {'is_skipped': True}, response(float('nan')),
                                   {**response(), 'action': 'soft reject'}, {**response(), 'symbols': []}])
def test_invalid_scan_does_not_classify(monkeypatch, body):
    fake_http(monkeypatch, body)
    with pytest.raises(rc.RspamdError): rc.scan(b'test', rc.DEFAULTS)


@pytest.mark.parametrize('status', [302, 401, 500])
def test_http_errors_are_safe(monkeypatch, status):
    fake_http(monkeypatch, {'secret': 'private'}, status)
    with pytest.raises(rc.RspamdError, match=f'HTTP {status}'):
        rc.scan(b'test', rc.DEFAULTS)


def test_admin_validation_and_permissions(env):
    admin = client('admin@example.com')
    data = {'rspamd_enabled': 'on', 'rspamd_url': 'http://rspamd:11333',
            'rspamd_clean_score': '0', 'rspamd_spam_score': '8', 'rspamd_timeout': '30', 'rspamd_simulation': 'on'}
    assert client('alice@example.com').post('/rspamd/settings', data=data).status_code == 403
    assert admin.post('/rspamd/settings', data=data).status_code == 303
    assert main.load_config()['rspamd_simulation'] is True
    assert 'http://rspamd:11333' in admin.get('/admin').text
    for change in ({'rspamd_url': 'file:///etc/passwd'}, {'rspamd_url': 'http://a:b@rspamd:11333'},
                   {'rspamd_url': 'http://rspamd:11333/?x=1'}, {'rspamd_clean_score': '8'},
                   {'rspamd_spam_score': 'nan'}, {'rspamd_timeout': '0'}):
        assert admin.post('/rspamd/settings', data={**data, **change}).status_code == 400
    task = {'task_options': '1', 'label': 'Local', 'host1': 'mail', 'user1': 'alice', 'pass1': 'secret',
            'bPretraitementIA': 'on', 'sMoteurIA': 'Rspamd', 'rspamd_url': 'http://attacker', 'rspamd_simulation': ''}
    assert client('alice@example.com').post('/account/add', data=task).status_code == 303
    account = main.load_config()['accounts'][-1]
    assert account['sMoteurIA'] == 'Rspamd' and not account['bActiverSynchro']
    assert 'rspamd_url' not in account and 'rspamd_simulation' not in account


class LocalMailbox(Mailbox):
    def response(self, name):
        if name == 'UIDVALIDITY' and '_03-Suspects' in self.folder: return name, [b'10']
        return super().response(name)


@pytest.mark.parametrize('score,engine,preview,ai_calls,moved', [
    (10, 'Rspamd', True, 0, False), (10, 'Rspamd', False, 0, True),
    (-1, 'Mistral', False, 0, False), (10, 'Mistral', False, 0, True),
    (3, 'Mistral', False, 1, False), (3, 'Rspamd', False, 0, False)])
def test_local_preprocessing_and_fallback(monkeypatch, rate_clock, score, engine, preview, ai_calls, moved):
    mailbox = LocalMailbox()
    monkeypatch.setattr(ai.imaplib, 'IMAP4_SSL', lambda *a, **kw: mailbox)
    fake_http(monkeypatch, response(score))
    called = []
    monkeypatch.setattr(ai, 'classify', lambda *args: called.append(args) or 'legitimate')
    account = dict(rc.DEFAULTS, owner='alice', host1='mail', user1='alice', pass1='secret',
                   bPretraitementIA=True, bActiverSynchro=False, sMoteurIA=engine,
                   rspamd_precheck=True, rspamd_enabled=True, rspamd_simulation=preview)
    state, events = {}, []
    metrics = RunMetrics(account)
    def progress(event):
        events.append(event)
        if isinstance(event, dict): metrics.event(event)
    asyncio.run(ai.preprocess(account, '', '' if engine == 'Rspamd' else 'key', {'cancelled': False},
                             lambda _: state, lambda *a: None, progress))
    assert len(called) == ai_calls
    assert any(cmd == 'COPY' for cmd, _ in mailbox.calls) == moved
    if moved:
        assert ('COPY', (b'1', '"INBOX._03-Suspects"')) in mailbox.calls
        assert ('EXPUNGE', (b'1',)) in mailbox.calls
    assert state == ({} if preview else {'1': 'done'})
    assert metrics.data['rspamd_analysed'] == 1
    assert metrics.data['ai_calls'] == ai_calls
    assert 'Rspamd local : 1' in metrics.summary('Succès')


def test_rspamd_failure_keeps_transfer_and_warning(env, monkeypatch):
    config = main.load_config()
    config.update(rspamd_enabled=True)
    main.save_config(config)
    mailbox = LocalMailbox()
    monkeypatch.setattr(ai.imaplib, 'IMAP4_SSL', lambda *a, **kw: mailbox)
    def fail(*args): raise rc.RspamdError('Rspamd : connexion impossible')
    monkeypatch.setattr(ai, 'scan_rspamd', fail)
    completed = type('Process', (), {'returncode': 0, 'wait': AsyncMock(),
                     'stdout': type('Output', (), {'read': AsyncMock(return_value=b'')})()})()
    process = AsyncMock(return_value=completed)
    monkeypatch.setattr(main.asyncio, 'create_subprocess_exec', process)
    account = config['accounts'][0]
    account.update(bPretraitementIA=True, sMoteurIA='Rspamd', bActiverSynchro=True)
    main.processes['a'] = {'cancelled': False, 'process': None}
    asyncio.run(main.execute(account, {'pseudo': 'Alice'}))
    assert process.await_count == 1
    assert '_03-Suspects' in process.call_args.args
    run = main.load_config()['runs'][-1]
    assert 'Rspamd : connexion impossible' in run['log']
    assert run['warnings']
    assert not any(cmd in ('COPY', 'STORE', 'EXPUNGE') for cmd, _ in mailbox.calls)


def test_manual_local_uses_global_settings(env, monkeypatch):
    config = main.load_config()
    config.update(rspamd_enabled=True, rspamd_url='http://rspamd:11333', rspamd_simulation=True)
    main.save_config(config)
    observed = []
    async def fake(account, *args): observed.append(copy.deepcopy(account))
    monkeypatch.setattr(main, 'preprocess', fake)
    result = client('alice@example.com').post('/cgi-bin/imapsync', data={
        'action': 'ai', 'host1': 'mail', 'user1': 'alice', 'password1': 'secret',
        'sMoteurIA': 'Rspamd', 'source_folder': 'INBOX', 'ai_mode': 'preview',
        'rspamd_url': 'http://attacker', 'rspamd_simulation': ''})
    assert result.status_code == 200
    assert observed[0]['rspamd_url'] == config['rspamd_url']
    assert observed[0]['rspamd_simulation'] and observed[0]['ai_dry']
    assert not observed[0]['bActiverSynchro']


@pytest.mark.parametrize('failure', ['cancel', 'copy', 'date'])
def test_local_results_keep_imap_safeguards(monkeypatch, failure):
    mailbox = LocalMailbox()
    mailbox.copy_failure = failure == 'copy'
    original = mailbox.uid
    def uid(command, *args):
        result = original(command, *args)
        if failure == 'date' and command == 'FETCH' and '_03-Suspects' in mailbox.folder:
            return 'OK', [b'9 (INTERNALDATE "01-Jan-2000 00:00:00 +0000")']
        return result
    mailbox.uid = uid
    monkeypatch.setattr(ai.imaplib, 'IMAP4_SSL', lambda *a, **kw: mailbox)
    active = {'cancelled': False}
    def scan(*args):
        if failure == 'cancel': active['cancelled'] = True
        return {'score': 10, 'verdict': 'rspamd_spam', 'action': 'reject', 'symbols': [], 'elapsed': 0.1}
    monkeypatch.setattr(ai, 'scan_rspamd', scan)
    account = dict(rc.DEFAULTS, owner='alice', host1='mail', user1='alice', pass1='secret',
                   bPretraitementIA=True, sMoteurIA='Rspamd', rspamd_enabled=True, rspamd_simulation=False)
    state = {}
    with pytest.raises(ai.PreprocessingError):
        asyncio.run(ai.preprocess(account, '', '', active, lambda _: state, lambda *a: None, lambda *a: None))
    assert not any(cmd in ('STORE', 'EXPUNGE') for cmd, _ in mailbox.calls)
    assert state == ({} if failure == 'cancel' else {'1': 'copying' if failure == 'copy' else 'copied'})
