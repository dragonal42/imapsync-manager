import asyncio
from unittest.mock import AsyncMock

import pytest
import requests

import ai_preprocessing as ai
import main
from test_ai_preprocessing import Mailbox
from test_tenants import env, client


@pytest.mark.parametrize('code, hint', [(401, 'clé API'), (403, 'droits'), (404, 'introuvable'), (429, 'quota'), (500, 'HTTP')])
def test_http_diagnostics_without_provider_body(monkeypatch, code, hint):
    response = requests.Response()
    response.status_code = code
    response._content = b'{"error":"SECRET email@example.com"}'
    monkeypatch.setattr(ai.requests, 'post', lambda *a, **kw: response)
    with pytest.raises(ai.PreprocessingError) as caught:
        ai.classify('Mistral', 'SECRET', {})
    assert f'HTTP {code}' in str(caught.value) and hint in str(caught.value)
    assert 'SECRET' not in str(caught.value) and 'email@example.com' not in str(caught.value)


@pytest.mark.parametrize('cause, hint', [(requests.Timeout('SECRET'), 'délai'),
    (requests.ConnectionError('SECRET'), 'DNS'), (requests.exceptions.SSLError('SECRET'), 'TLS')])
def test_network_diagnostics(monkeypatch, cause, hint):
    def fail(*a, **kw): raise cause
    monkeypatch.setattr(ai.requests, 'post', fail)
    with pytest.raises(ai.PreprocessingError) as caught:
        ai.classify('Mistral', 'SECRET', {})
    assert hint in str(caught.value) and 'SECRET' not in str(caught.value)


@pytest.mark.parametrize('content, finish, hint', [('not json SECRET', 'stop', 'non JSON'),
    ('{"classification":"spam"}', 'stop', 'schéma invalide'), ('{}', 'length', 'max_tokens')])
def test_invalid_generated_answer_retains_usage(monkeypatch, content, finish, hint):
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {'usage': {'total_tokens': 12}, 'choices': [{'finish_reason': finish, 'message': {'content': content}}]}
    monkeypatch.setattr(ai.requests, 'post', lambda *a, **kw: Response())
    with pytest.raises(ai.PreprocessingError) as caught:
        ai.classify('Mistral', 'SECRET', {})
    assert hint in str(caught.value) and 'SECRET' not in str(caught.value)
    assert caught.value.usage['total'] == 12


def test_manual_ai_source_only_private_debug_history(env, monkeypatch):
    async def fake(*args):
        account = args[0]
        assert account['manual_ai'] and not account['bActiverSynchro'] and account['ai_dry']
        assert account['source_folder'] == 'INBOX.Test' and 'host2' not in account
        args[-1]('Diagnostic IMAP détaillé')
        raise ai.PreprocessingError('Mistral | HTTP 401 | clé API invalide')
    monkeypatch.setattr(main, 'preprocess', fake)
    process = AsyncMock(side_effect=AssertionError('no transfer'))
    monkeypatch.setattr(main.asyncio, 'create_subprocess_exec', process)
    response = client('alice@example.com').post('/cgi-bin/imapsync', data={
        'action': 'ai', 'host1': 'mail.example.com', 'user1': 'alice', 'password1': 'SECRET',
        'source_folder': 'INBOX.Test', 'sMoteurIA': 'Mistral'})
    assert response.status_code == 200
    run = main.load_config()['runs'][-1]
    assert run['status'] == 'Erreur' and run['log_debug']
    assert 'Diagnostic IMAP détaillé' in run['log'] and 'HTTP 401' in run['log']
    assert 'SECRET' not in run['log'] and process.await_count == 0
    assert client('bob@example.com').get('/api/logs/' + response.json()['run_id']).status_code == 404
    assert not main.load_config()['log_debug']


@pytest.mark.parametrize('field, value', [('sMoteurIA', 'unknown'), ('nPeriodeJours', '0'),
    ('nPeriodeJours', 'bad'), ('source_folder', 'INBOX\r\n'), ('ai_mode', 'bad')])
def test_manual_ai_validation(env, field, value):
    response = client('alice@example.com').post('/cgi-bin/imapsync', data={
        'action': 'ai', 'host1': 'mail.example.com', 'user1': 'alice', 'password1': 'secret', field: value})
    assert response.status_code == 400


def test_manual_preview_custom_root_no_moves_or_state_writes(monkeypatch):
    mailbox = Mailbox()
    mailbox.list = lambda *a: ('OK', [b'() "." "Custom"', b'() "." "Custom.Child"'])
    monkeypatch.setattr(ai.imaplib, 'IMAP4_SSL', lambda *a, **kw: mailbox)
    monkeypatch.setattr(ai, 'classify', lambda *a: 'spam')
    writes = []
    events = []
    account = {'host1': 'mail', 'user1': 'alice', 'pass1': 'secret', 'owner': 'alice',
               'manual_ai': True, 'ai_dry': True, 'bPretraitementIA': True, 'sMoteurIA': 'Mistral', 'source_folder': 'Custom'}
    asyncio.run(ai.preprocess(account, '', 'key', {'cancelled': False}, lambda _: {},
                             lambda *a: writes.append(a), events.append))
    assert not writes and not any(cmd in ('COPY', 'STORE', 'EXPUNGE') for cmd, _ in mailbox.calls)
    assert any('simulation, aucun déplacement' in str(event) for event in events)
    assert mailbox.folder == 'Custom'


def test_custom_root_tri_uses_inbox_quarantine_separator(monkeypatch):
    mailbox = Mailbox()
    mailbox.list = lambda *a: ('OK', [b'() "." "Custom"', b'() "/" "INBOX"'])
    monkeypatch.setattr(ai.imaplib, 'IMAP4_SSL', lambda *a, **kw: mailbox)
    monkeypatch.setattr(ai, 'classify', lambda *a: 'spam')
    account = {'host1': 'mail', 'user1': 'alice', 'pass1': 'secret', 'owner': 'alice',
               'manual_ai': True, 'ai_dry': False, 'bPretraitementIA': True, 'sMoteurIA': 'Mistral', 'source_folder': 'Custom'}
    asyncio.run(ai.preprocess(account, '', 'key', {'cancelled': False}, lambda _: {}, lambda *a: None, lambda *a: None))
    assert ('COPY', (b'1', '"INBOX/_01-Arnaques"')) in mailbox.calls
    assert ('EXPUNGE', (b'1',)) in mailbox.calls


def test_mistral_text_parts_supported(monkeypatch):
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {'choices': [{'finish_reason': 'stop', 'message': {'content': [
            {'type': 'text', 'text': '{"verdict":"legitimate"}'}]}}]}
    monkeypatch.setattr(ai.requests, 'post', lambda *a, **kw: Response())
    assert ai.classify('Mistral', 'SECRET', {}) == 'legitimate'
