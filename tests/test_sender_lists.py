import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import main
import ai_preprocessing as ai
from run_logging import RunMetrics
from sender_rules import parse_import, sender_address, sender_decision
from test_tenants import env, client
from test_ai_preprocessing import Mailbox
from test_audit_logging import audit_output


def test_no_calls_no_usage_lines():
    text = RunMetrics({'bPretraitementIA': True}).summary('Succès')
    assert 'Emails analysés par IA Mistral : 0 | Nb frauduleux/spams détectés : 0 | Nb déplacés : 0' in text
    assert 'Consommation' not in text and 'Coût' not in text
    metrics = RunMetrics({'bPretraitementIA': True})
    metrics.event({'usage': {}})
    assert 'Consommation' not in metrics.summary('Erreur')


def test_import_separators_dedup_and_validation():
    assert parse_import(' A@example.com; b@example.com|C@example.com, d@example.com\te@example.com\na@example.com') == [
        'a@example.com', 'b@example.com', 'c@example.com', 'd@example.com', 'e@example.com']
    for value in ('', 'bad', 'Alice <alice@example.com>', 'ok@example.com;broken', 'a@example.com x@example.com'):
        with pytest.raises(ValueError): parse_import(value)


def test_import_preview_is_readonly_and_lists_are_private(env):
    alice, bob = client('alice@example.com'), client('bob@example.com')
    payload = {'kind': 'whitelist', 'emails': 'A@example.com;b@example.com', 'owner': 'bob@example.com'}
    assert alice.post('/sender-lists/preview', data=payload).json()['added'] == 2
    assert not any(u.get('sender_lists') for u in main.load_config()['users'])
    assert alice.post('/sender-lists/import', data=payload).json()['added'] == 2
    users = {u['email']: u for u in main.load_config()['users']}
    assert users['alice@example.com']['sender_lists']['whitelist'] == ['a@example.com', 'b@example.com']
    assert not users['bob@example.com'].get('sender_lists')
    assert 'a@example.com' in alice.get('/sender-lists').text
    assert 'a@example.com' not in bob.get('/sender-lists?owner=alice@example.com').text
    assert alice.post('/sender-lists/import', data=payload).json()['added'] == 0
    conflict = {**payload, 'kind': 'blacklist'}
    assert alice.post('/sender-lists/preview', data=conflict).status_code == 409
    assert alice.post('/sender-lists/import', data=conflict).status_code == 409
    assert 'a@example.com' not in alice.get('/sender-lists?q=b%40').text
    assert 'b@example.com' in alice.get('/sender-lists?q=B%40').text
    assert bob.post('/sender-lists/delete', data={'kind': 'whitelist', 'email': 'a@example.com', 'owner': 'alice@example.com'}).json()['removed'] is False
    assert alice.post('/sender-lists/delete', data={'kind': 'whitelist', 'email': 'a@example.com'}).json()['removed'] is True
    assert 'a@example.com' not in alice.get('/sender-lists').text
    assert alice.post('/sender-lists/import', data=payload, headers={'Origin': 'https://evil.example'}).status_code == 403


def test_sender_parsing_is_exact_and_unambiguous():
    assert sender_address(b'From: Alice <ALICE@example.com>\r\n\r\n') == 'alice@example.com'
    assert sender_address(b'From: a@example.com\r\nFrom: b@example.com\r\n\r\n') is None
    assert sender_address(b'From: a@example.com, b@example.com\r\n\r\n') is None
    assert sender_decision('evil-alice@example.com', {'whitelist': ['alice@example.com']}) is None
    assert sender_decision('alice@example.com', {'whitelist': ['alice@example.com'], 'blacklist': ['alice@example.com']}) == 'blacklist'


class RuleMailbox(Mailbox):
    def __init__(self):
        super().__init__()
        self.raw = b'From: Sender <sender@example.com>\r\n\r\n'
    def response(self, name):
        return (name, [b'10 1 9' if name == 'COPYUID' else (b'10' if '_02-BlackList' in self.folder or '_01-Arnaques' in self.folder else b'7')])
    def uid(self, command, *args):
        if command == 'FETCH' and args[1] == '(BODY.PEEK[HEADER.FIELDS (FROM)])':
            self.calls.append((command, args))
            return ('OK', [(b'1', self.raw)])
        return super().uid(command, *args)


@pytest.mark.parametrize('kind', ['whitelist', 'blacklist'])
def test_list_rule_bypasses_ai_and_body_fetch(monkeypatch, kind):
    mailbox = RuleMailbox()
    monkeypatch.setattr(ai.imaplib, 'IMAP4_SSL', lambda *a, **kw: mailbox)
    classifier = lambda *a: pytest.fail('Listed sender must never reach AI')
    monkeypatch.setattr(ai, 'classify', classifier)
    account = {'host1': 'mail', 'user1': 'alice', 'pass1': 'secret', 'owner': 'alice',
               'bPretraitementIA': True, 'sMoteurIA': 'Mistral', 'sender_lists': {kind: ['sender@example.com']}}
    events, state = [], {}
    asyncio.run(ai.preprocess(account, '', '', {'cancelled': False}, lambda _: state, lambda *a: None, events.append))
    assert state['1'] == 'done'
    assert not any(command == 'FETCH' and args[1] == '(BODY.PEEK[])' for command, args in mailbox.calls)
    if kind == 'blacklist':
        assert ('COPY', (b'1', '"INBOX._02-BlackList"')) in mailbox.calls
        assert ('EXPUNGE', (b'1',)) in mailbox.calls
        assert {'blacklist_moved': True} in events
    else:
        assert not any(command in ('STORE', 'COPY', 'EXPUNGE') for command, _ in mailbox.calls)
        assert {'whitelisted': True} in events


def test_blacklist_copy_failure_preserves_original(monkeypatch):
    mailbox = RuleMailbox()
    mailbox.copy_failure = True
    monkeypatch.setattr(ai.imaplib, 'IMAP4_SSL', lambda *a, **kw: mailbox)
    monkeypatch.setattr(ai, 'classify', lambda *a: pytest.fail('No AI'))
    account = {'host1': 'mail', 'user1': 'alice', 'pass1': 'secret', 'owner': 'alice',
               'bPretraitementIA': True, 'sender_lists': {'blacklist': ['sender@example.com']}}
    with pytest.raises(ai.PreprocessingError):
        asyncio.run(ai.preprocess(account, '', '', {'cancelled': False}, lambda _: {}, lambda *a: None, lambda *a: None))
    assert not any(command in ('STORE', 'EXPUNGE') for command, _ in mailbox.calls)


def test_rules_use_configuration_owner_not_admin_actor(env, monkeypatch):
    config = main.load_config()
    for user in config['users']:
        user['sender_lists'] = {'whitelist': [user['email']]}
    main.save_config(config)
    account = config['accounts'][0]
    account.update(bActiverSynchro=False, bPretraitementIA=True)
    async def preprocess(account, *args):
        assert account['sender_lists']['whitelist'] == ['alice@example.com']
    monkeypatch.setattr(main, 'preprocess', preprocess)
    main.processes['a'] = {'process': None, 'cancelled': False}
    asyncio.run(main.execute(account, {'pseudo': 'Admin', 'email': 'admin@example.com'}))
    assert main.load_config()['runs'][-1]['status'] == 'Succès'


@pytest.mark.parametrize('peer,forwarded,expected', [
    ('10.0.0.2', '198.51.100.23', '198.51.100.23'),
    ('198.51.100.4', '203.0.113.99', '198.51.100.4'),
    ('10.0.0.2', '203.0.113.99, 198.51.100.23', '198.51.100.23'),
    ('10.0.0.2', '2001:db8::42', '2001:db8::42')])
def test_ip_trusted_proxy_and_spoof_resistance(env, monkeypatch, audit_output, peer, forwarded, expected):
    monkeypatch.setattr(main, 'deliver_link', AsyncMock())
    wrapped = ProxyHeadersMiddleware(main.app, trusted_hosts=['10.0.0.2'])
    browser = TestClient(wrapped, base_url=main.PUBLIC_URL, client=(peer, 1234))
    browser.post('/login', data={'email': 'alice@example.com'}, headers={'Origin': main.PUBLIC_URL, 'X-Forwarded-For': forwarded})
    assert '[MAGIC_LINK_REQUEST] [' + expected + ']' in audit_output.getvalue()


def test_manual_background_completion_retains_client_ip(env, monkeypatch, audit_output):
    from test_tenants import form
    class Process:
        returncode = 0
        stdout = type('Output', (), {'read': AsyncMock(side_effect=[b'Messages transferred : 1\n', b''])})()
        wait = AsyncMock()
    monkeypatch.setattr(main.asyncio, 'create_subprocess_exec', AsyncMock(return_value=Process()))
    alice = client('alice@example.com')
    wrapped = ProxyHeadersMiddleware(main.app, trusted_hosts=['10.0.0.2'])
    browser = TestClient(wrapped, base_url=main.PUBLIC_URL, client=('10.0.0.2', 1234))
    browser.cookies.update(alice.cookies)
    result = browser.post('/cgi-bin/imapsync', data=form(password1='secret', password2='secret'),
                          headers={'Origin': main.PUBLIC_URL, 'X-Forwarded-For': '198.51.100.44'})
    assert result.status_code == 200
    text = audit_output.getvalue()
    assert '[MANUAL_SYNC_REQUESTED] [198.51.100.44]' in text
    assert '[MANUAL_SYNC_FINISHED] [198.51.100.44]' in text
