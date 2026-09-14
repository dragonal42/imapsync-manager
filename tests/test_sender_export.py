import asyncio
from unittest.mock import AsyncMock

import pytest

import main
import sender_export as export
from ai_preprocessing import PreprocessingError
from test_tenants import env, client


class Mailbox:
    def __init__(self): self.calls = []; self.folder = ''
    def login(self, *args): self.calls.append(('LOGIN', args)); return 'OK', []
    def authenticate(self, mech, callback):
        self.calls.append(('AUTH', (mech, callback(None))))
        return 'OK', []
    def logout(self): self.calls.append(('LOGOUT', ())); return 'BYE', []
    def list(self, *args):
        return 'OK', [b'(\\Noselect) "." "Parent"', b'() "." "INBOX"', b'() "." "INBOX.Sent"', b'() "." "INBOX.spam"']
    def select(self, folder, readonly=False):
        assert readonly
        self.folder = folder
        self.calls.append(('EXAMINE', (folder,)))
        return 'OK', [b'3']
    def uid(self, command, *args):
        self.calls.append((command, args))
        if command == 'SEARCH':
            assert args == (None, 'ALL')
            return 'OK', [b'1 2 3']
        assert command == 'FETCH'
        assert args[1] == '(BODY.PEEK[HEADER.FIELDS (FROM)])'
        return 'OK', [(b'1', b'From: Alice <ALICE@example.com>\r\n\r\n'),
                      (b'2', b'From: Bob <bob@example.com>\r\n\r\n'),
                      (b'3', b'From: not-an-address\r\n\r\n')]


@pytest.mark.parametrize('oauth', [False, True])
def test_inventory_is_read_only_all_folders_deduplicated(monkeypatch, oauth):
    mailbox = Mailbox()
    monkeypatch.setattr(export.imaplib, 'IMAP4_SSL', lambda *a, **kw: mailbox)
    account = {'host1': 'mail', 'user1': 'user', 'pass1': 'secret', 'token1': 'oauth-token',
               'authmech1': 'XOAUTH2' if oauth else 'PLAIN'}
    addresses, count = asyncio.run(export.extract_senders(account, {'bob@example.com'}, {'cancelled': False}, lambda *a: None))
    assert addresses == ['alice@example.com'] and count == 9
    assert [args[0] for cmd, args in mailbox.calls if cmd == 'EXAMINE'] == ['"INBOX"', '"INBOX.Sent"', '"INBOX.spam"']
    assert not any(cmd in ('COPY', 'STORE', 'EXPUNGE') for cmd, _ in mailbox.calls)
    assert mailbox.calls[-1][0] == 'LOGOUT'
    if oauth: assert mailbox.calls[0] == ('AUTH', ('XOAUTH2', b'user=user\x01auth=Bearer oauth-token\x01\x01'))


def test_listing_literals_and_escaped_names():
    assert export.folders_from_list([(b'() "/" {9}', b'INBOX.Foo'), b'() "/" "a\\"b"']) == ['INBOX.Foo', 'a"b']
    assert export.addresses_from_header(b'From: A <a@example.com>, B <b@example.com>\r\n\r\n') == {'a@example.com', 'b@example.com'}


def test_cancel_logs_out_without_fetch(monkeypatch):
    mailbox = Mailbox()
    active = {'cancelled': False}
    monkeypatch.setattr(export.imaplib, 'IMAP4_SSL', lambda *a, **kw: mailbox)
    def progress(*args): active['cancelled'] = True
    with pytest.raises(PreprocessingError, match='arrêtée'):
        asyncio.run(export.extract_senders({'host1': 'mail', 'user1': 'user', 'pass1': 'secret'}, set(), active, progress))
    assert not any(cmd == 'FETCH' for cmd, _ in mailbox.calls)
    assert mailbox.calls[-1][0] == 'LOGOUT'


@pytest.mark.parametrize('excluded', [False, True])
def test_manual_source_only_export_owner_filter_and_output(env, monkeypatch, excluded):
    config = main.load_config()
    alice = next(user for user in config['users'] if user['email'] == 'alice@example.com')
    alice['sender_lists'] = {'whitelist': ['known@example.com']}
    main.save_config(config)
    async def extract(account, whitelist, active, progress):
        assert account['owner'] == 'alice@example.com' and 'host2' not in account
        assert whitelist == ({'known@example.com'} if excluded else set())
        progress('Lecture en cours')
        return ['a@example.com', 'b@example.com'], 25
    monkeypatch.setattr(main, 'extract_senders', extract)
    monkeypatch.setattr(main, 'preprocess', AsyncMock(side_effect=AssertionError('No AI')))
    monkeypatch.setattr(main.asyncio, 'create_subprocess_exec', AsyncMock(side_effect=AssertionError('No transfer')))
    data = {'action': 'senders', 'host1': 'mail', 'user1': 'alice', 'password1': 'secret',
            'owner': 'admin@example.com', 'exclude_whitelist': 'on' if excluded else ''}
    response = client('alice@example.com').post('/cgi-bin/imapsync', data=data)
    assert response.status_code == 200
    run_id = response.json()['run_id']
    result = client('alice@example.com').get('/api/logs/' + run_id).json()
    assert result['finished'] and result['sender_export'] and result['sender_count'] == 2
    assert result['log'] == 'a@example.com;\nb@example.com'
    assert client('bob@example.com').get('/api/logs/' + run_id).status_code in (403, 404)
    assert main.load_config()['users'] == config['users']
    assert not main.processes


def test_failed_export_not_presented_as_complete(env, monkeypatch):
    async def fail(*args): raise PreprocessingError('Extraction incomplète')
    monkeypatch.setattr(main, 'extract_senders', fail)
    result = client('alice@example.com').post('/cgi-bin/imapsync', data={
        'action': 'senders', 'host1': 'mail', 'user1': 'alice', 'password1': 'secret'})
    run = client('alice@example.com').get('/api/logs/' + result.json()['run_id']).json()
    assert run['status'] == 'Erreur' and run['finished']
    assert 'secret' not in run['log']
