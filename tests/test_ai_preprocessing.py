import asyncio
import copy
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

import ai_preprocessing as ai
import main
from test_tenants import env, client, form


def test_settings_secret_preservation_and_isolation(env):
    assert client('alice@example.com').post('/ai/settings', data={'sApiKeyMistral': 'secret-ai'}).status_code == 403
    admin = client('admin@example.com')
    assert admin.post('/ai/settings', data={'sApiKeyMistral': 'secret-ai', 'sApiKeyGemini': 'secret-gemini'}).status_code == 303
    assert admin.post('/ai/settings', data={'sApiKeyMistral': ''}).status_code == 303
    assert main.load_config()['sApiKeyMistral'] == 'secret-ai'
    for browser, path in [(admin, '/admin'), (client('alice@example.com'), '/dashboard')]:
        page = browser.get(path).text
        assert 'secret-ai' not in page and 'secret-gemini' not in page
    admin.post('/ai/settings', data={'clear_sApiKeyMistral': 'on'})
    assert 'sApiKeyMistral' not in main.load_config()


def test_source_only_form_validation_and_defaults(env):
    browser = client('alice@example.com')
    source = {'task_options': '1', 'label': 'Check', 'host1': 'mail.example.com', 'user1': 'alice', 'pass1': 'secret'}
    assert browser.post('/account/add', data=source).status_code == 303
    account = main.load_config()['accounts'][-1]
    assert account['bActiverSynchro'] is False and account['bPretraitementIA'] is False
    assert 'host2' not in account
    assert browser.post('/account/add', data={**source, 'bPretraitementIA': 'on'}).status_code == 400
    assert browser.post('/account/add', data={**source, 'nPeriodeJours': '0'}).status_code == 400
    assert browser.post('/account/add', data={**source, 'source_folder': 'INBOX"\r\n'}).status_code == 400
    assert browser.post('/account/add', data=form()).status_code == 303
    assert main.load_config()['accounts'][-1]['bActiverSynchro'] is True
    page = browser.get('/account/new').text
    assert 'name="bActiverSynchro" id="bActiverSynchro" checked' in page
    assert 'name="bPretraitementIA" id="bPretraitementIA" checked' not in page


def test_compact_data_omits_body_and_irrelevant_headers():
    data = ai.compact_message(b'Subject: Test\r\nFrom: sender@example.org\r\nX-Secret: private\r\nContent-Type: text/html\r\n\r\nPRIVATE BODY <a href="https://example.org/?a=1&amp;b=2">link</a>')
    assert data['subject'] == 'Test'
    assert data['urls'] == ['https://example.org/?a=1&b=2']
    assert 'private' not in str(data).lower()
    assert list(data['headers']) == sorted(data['headers'])


@pytest.mark.parametrize('engine', ['Mistral', 'Gemini'])
def test_provider_requests_and_strict_response(monkeypatch, engine):
    calls = []
    class Response:
        def raise_for_status(self): pass
        def json(self):
            if engine == 'Mistral':
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"verdict":"spam"}'}}]}
            return {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '{"verdict":"spam"}'}]}}]}
    monkeypatch.setattr(ai.requests, 'post', lambda url, **kwargs: calls.append((url, kwargs)) or Response())
    assert ai.classify(engine, 'api-secret', {'subject': 'test'}) == 'spam'
    assert 'api-secret' not in calls[0][0]
    assert calls[0][1]['timeout'] == (10, 45)
    monkeypatch.setattr(Response, 'json', lambda self: {})
    with pytest.raises(ai.PreprocessingError, match='réponse invalide'):
        ai.classify(engine, 'api-secret', {})


class Mailbox:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.folder = 'INBOX'
        self.date = datetime.now(timezone.utc).strftime('%d-%b-%Y %H:%M:%S +0000').encode()
        self.bad_date = False
        self.copy_failure = False
        self.seen = False
    def login(self, *args): return ('OK', [])
    def logout(self): return ('BYE', [])
    def select(self, folder):
        self.folder = folder.strip('"')
        return ('OK', [b'1'])
    def capability(self): return ('OK', [b'IMAP4rev1 UIDPLUS'])
    def response(self, name):
        return (name, [b'10 1 9' if name == 'COPYUID' else (b'10' if '_01-Arnaques' in self.folder else b'7')])
    def list(self, *args): return ('OK', [b'(\\HasChildren) "." "INBOX"'])
    def uid(self, command, *args):
        self.calls.append((command, args))
        if command == 'SEARCH': return ('OK', [b'1'])
        if command == 'COPY' and self.copy_failure: return ('NO', [b'refused'])
        if command == 'FETCH':
            if args[1] == '(BODY.PEEK[])': return ('OK', [(b'1', b'Subject: test\r\n\r\nhttps://spam.example')])
            date = b'01-Jan-2000 00:00:00 +0000' if self.bad_date and '_01-Arnaques' in self.folder else self.date
            return ('OK', [b'1 (FLAGS (' + (b'\\Seen' if self.seen else b'') + b') INTERNALDATE "' + date + b'" RFC822.SIZE 100)'])
        return ('OK', [])


def run_preprocess(monkeypatch, mailbox, state=None, verdict='spam', active=None):
    state = {} if state is None else state
    monkeypatch.setattr(ai.imaplib, 'IMAP4_SSL', lambda *a, **kw: mailbox)
    monkeypatch.setattr(ai, 'classify', lambda *a: verdict)
    account = {'owner': 'alice', 'host1': 'mail', 'user1': 'alice', 'pass1': 'secret', 'bPretraitementIA': True, 'sMoteurIA': 'Mistral', 'nPeriodeJours': 5}
    asyncio.run(ai.preprocess(account, '', 'key', active or {'cancelled': False}, lambda _: state,
                             lambda identity, data: None, lambda text: None))
    return state


def test_copy_date_verification_and_targeted_delete(monkeypatch):
    mailbox = Mailbox()
    state = run_preprocess(monkeypatch, mailbox)
    assert state == {'1': 'done'}
    commands = [command for command, _ in mailbox.calls]
    assert commands.index('COPY') < commands.index('STORE') < commands.index('EXPUNGE')
    assert ('EXPUNGE', (b'1',)) in mailbox.calls
    assert ('SEARCH', (None, 'UNSEEN', 'UNDELETED', 'SINCE', mailbox.calls[0][1][-1])) in mailbox.calls
    assert ('COPY', (b'1', '"INBOX._01-Arnaques"')) in mailbox.calls
    before = len(mailbox.calls)
    run_preprocess(monkeypatch, mailbox, state)
    assert not any(command == 'COPY' for command, _ in mailbox.calls[before:])


@pytest.mark.parametrize('failure', ['bad_date', 'copy_failure'])
def test_failed_or_unverified_copy_never_deletes(monkeypatch, failure):
    mailbox = Mailbox()
    setattr(mailbox, failure, True)
    state = {}
    with pytest.raises(ai.PreprocessingError):
        run_preprocess(monkeypatch, mailbox, state)
    assert not any(command in ('STORE', 'EXPUNGE') for command, _ in mailbox.calls)
    assert state['1'] in ('copying', 'copied')
    with pytest.raises(ai.PreprocessingError, match='interrompu'):
        run_preprocess(monkeypatch, mailbox, state)


@pytest.mark.parametrize('verdict', ['legitimate', 'uncertain'])
def test_non_spam_kept_unread(monkeypatch, verdict):
    mailbox = Mailbox()
    run_preprocess(monkeypatch, mailbox, verdict=verdict)
    assert not any(command in ('STORE', 'COPY', 'EXPUNGE') for command, _ in mailbox.calls)


def test_read_messages_skipped(monkeypatch):
    mailbox = Mailbox()
    mailbox.seen = True
    run_preprocess(monkeypatch, mailbox)
    assert not any(command == 'COPY' for command, _ in mailbox.calls)


def test_source_only_execute_never_starts_imapsync(env, monkeypatch):
    account = copy.deepcopy(main.load_config()['accounts'][0])
    account.update(bActiverSynchro=False)
    check = AsyncMock()
    process = AsyncMock(side_effect=AssertionError('must not run'))
    monkeypatch.setattr(main, 'preprocess', check)
    monkeypatch.setattr(main.asyncio, 'create_subprocess_exec', process)
    main.processes['a'] = {'cancelled': False, 'process': None}
    asyncio.run(main.execute(account, {'pseudo': 'Alice'}))
    assert check.await_count == 1
    assert process.await_count == 0
    run = main.load_config()['runs'][-1]
    assert run['status'] == 'Succès' and run['finished']
    assert client('alice@example.com').get('/api/logs/' + run['id']).json()['finished'] is True
    assert 'OK' in client('alice@example.com').get('/logs/' + run['id']).text
    assert client('bob@example.com').get('/api/logs/' + run['id']).status_code == 404
    assert [a['id'] for a in client('alice@example.com').get('/api/task-status').json()['accounts']] == ['a']


@pytest.mark.parametrize('debug', [False, True])
@pytest.mark.parametrize('returncode', [0, 64])
def test_preprocessing_failure_keeps_sync_and_reports_warning(env, monkeypatch, debug, returncode):
    config = main.load_config()
    config['log_debug'] = debug
    main.save_config(config)
    account = copy.deepcopy(main.load_config()['accounts'][0])
    account.update(bPretraitementIA=True, sMoteurIA='Mistral')
    monkeypatch.setattr(main, 'preprocess', AsyncMock(side_effect=ai.PreprocessingError('Analyse indisponible')))
    completed = type('Process', (), {'returncode': returncode, 'wait': AsyncMock(),
                     'stdout': type('Output', (), {'read': AsyncMock(side_effect=[b'Messages transferred : 2\n', b''])})()})()
    process = AsyncMock(return_value=completed)
    monkeypatch.setattr(main.asyncio, 'create_subprocess_exec', process)
    main.processes['a'] = {'cancelled': False, 'process': None}
    asyncio.run(main.execute(account, {'pseudo': 'Alice'}))
    assert process.await_count == 1
    run = main.load_config()['runs'][-1]
    assert run['status'] == ('Succès avec avertissement' if returncode == 0 else 'Erreur')
    assert 'Analyse indisponible' in run['log'] and 'Emails transférés par imapsync : 2' in run['log']
    assert run['warnings'] and 'Analyse indisponible' in main.CONFIG_FILE.with_name('daily_errors.log').read_text(encoding='utf-8')
    assert client('alice@example.com').get('/api/logs/' + run['id']).json()['finished'] is True
    args = process.call_args.args
    assert '--automap' in args
    assert '--nofoldersizes' in args and '--nofoldersizesatend' in args
    assert 'INBOX.spam' not in args and 'INBOX.Sent' not in args


def test_delete1_once_and_successful_info_messages(env, monkeypatch):
    config = main.load_config()
    config["log_debug"] = True
    main.save_config(config)
    account = copy.deepcopy(main.load_config()['accounts'][0])
    account.update(delete1='on', bPretraitementIA=True, sMoteurIA='Mistral')
    order = []
    async def preprocess(*args): order.append('ai')
    async def create(*args, **kwargs):
        order.append('sync')
        assert args.count('--delete1') == 1
        assert '--noexpunge1' not in args
        assert args[args.index('--exclude') + 1] == '_01-Arnaques'
        class Process:
            returncode = 0
            stdout = type('Output', (), {'read': AsyncMock(side_effect=[b'Info: turning on --expunge1 because --delete1 --noexpunge1 is very dangerous on the second run.\n', b''])})()
            wait = AsyncMock()
        return Process()
    monkeypatch.setattr(main, 'preprocess', preprocess)
    monkeypatch.setattr(main.asyncio, 'create_subprocess_exec', create)
    main.processes['a'] = {'cancelled': False, 'process': None}
    asyncio.run(main.execute(account, {'pseudo': 'Alice'}))
    run = main.load_config()['runs'][-1]
    assert run['status'] == 'Succès' and 'Info: turning on' in run['log']
    assert order == ['ai', 'sync']


def test_old_internal_date_skipped(monkeypatch):
    mailbox = Mailbox()
    mailbox.date = b'01-Jan-2000 00:00:00 +0000'
    assert run_preprocess(monkeypatch, mailbox) == {}
    assert not any(command == 'COPY' for command, _ in mailbox.calls)


def test_uidplus_required_before_any_copy(monkeypatch):
    mailbox = Mailbox()
    mailbox.capability = lambda: ('OK', [b'IMAP4rev1'])
    with pytest.raises(ai.PreprocessingError, match='UIDPLUS'):
        run_preprocess(monkeypatch, mailbox)
    assert not mailbox.calls


def test_cancel_during_classification_never_moves(monkeypatch):
    mailbox = Mailbox()
    active = {'cancelled': False}
    monkeypatch.setattr(ai.imaplib, 'IMAP4_SSL', lambda *a, **kw: mailbox)
    def classify(*args):
        active['cancelled'] = True
        return 'spam'
    monkeypatch.setattr(ai, 'classify', classify)
    account = {'owner': 'alice', 'host1': 'mail', 'user1': 'alice', 'pass1': 'secret', 'bPretraitementIA': True, 'sMoteurIA': 'Mistral'}
    with pytest.raises(ai.PreprocessingError, match='Arrêt demandé'):
        asyncio.run(ai.preprocess(account, '', 'key', active, lambda _: {}, lambda *a: None, lambda text: None))
    assert not any(command in ('COPY', 'STORE', 'EXPUNGE') for command, _ in mailbox.calls)
