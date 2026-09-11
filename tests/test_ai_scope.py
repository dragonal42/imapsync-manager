import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

import ai_preprocessing as ai
import main
from run_logging import RunMetrics
from test_ai_preprocessing import Mailbox
from test_tenants import env


@pytest.mark.parametrize('delimiter', ['.', '/'])
def test_scope_excludes_special_branches_and_other_roots(delimiter):
    allowed = ['INBOX', f'INBOX{delimiter}Clients', f'INBOX{delimiter}Clients{delimiter}2026',
               f'INBOX{delimiter}SpamInvoices']
    excluded = ['Other', 'Other.INBOX', 'INBOX2']
    for special in ['Sent', 'Trash', 'Junk', 'Drafts', 'Archive', 'sPaM', '_01-Arnaques', '_02-BlackList']:
        excluded.extend([f'INBOX{delimiter}{special}', f'INBOX{delimiter}{special}{delimiter}Child',
                         f'INBOX{delimiter}Clients{delimiter}{special}'])
    entries = [f'() "{delimiter}" "{name}"'.encode() for name in allowed + excluded]
    entries.append(f'(\\Noselect) "{delimiter}" "INBOX{delimiter}Virtual"'.encode())
    folders, actual = ai.inbox_folders(entries)
    assert set(folders) == set(allowed) and folders[0] == 'INBOX'
    assert actual == delimiter


def test_list_literals_escaping_and_modified_utf7():
    entries = [b'() "/" INBOX', (b'() "/" {12}', b'INBOX/Client'), b'',
               b'() "/" "INBOX/a\\"b"', b'() "/" "INBOX/&AOk-tudes"']
    folders, _ = ai.inbox_folders(entries)
    assert 'INBOX/Client' in folders and 'INBOX/a"b' in folders and 'INBOX/&AOk-tudes' in folders
    assert ai.quote_mailbox('INBOX/a"b') == '"INBOX/a\\"b"'
    with pytest.raises(ai.PreprocessingError):
        ai.quote_mailbox('INBOX\r\nBAD')


@pytest.mark.parametrize('entries', [[b'invalid'], [b'() "." Other']])
def test_bad_listing_is_an_explicit_error(entries):
    with pytest.raises(ai.PreprocessingError):
        ai.inbox_folders(entries)


def test_subfolders_processed_with_independent_uid_state(monkeypatch):
    class MultiMailbox(Mailbox):
        def __init__(self):
            super().__init__()
            self.visited = []
        def select(self, folder):
            self.visited.append(folder.strip('"'))
            return super().select(folder)
        def list(self, *args):
            return 'OK', [f'() "." "{name}"'.encode() for name in
                          ['INBOX', 'INBOX.Clients', 'INBOX.Sent', 'INBOX.spam', 'Other']]
    mailbox = MultiMailbox()
    monkeypatch.setattr(ai.imaplib, 'IMAP4_SSL', lambda *a, **kw: mailbox)
    classify = []
    monkeypatch.setattr(ai, 'classify', lambda *args: classify.append(args) or 'legitimate')
    account = {'owner': 'alice', 'host1': 'mail', 'user1': 'alice', 'pass1': 'secret',
               'bPretraitementIA': True, 'sMoteurIA': 'Mistral', 'source_folder': 'Other'}
    states = {}
    metrics = RunMetrics(account)
    def progress(event):
        if isinstance(event, dict):
            metrics.event(event)
    def run():
        asyncio.run(ai.preprocess(account, '', 'key', {'cancelled': False}, lambda key: states.get(key, {}),
                                 lambda key, state: states.update({key: copy.deepcopy(state)}), progress))
    run()
    assert mailbox.visited == ['INBOX', 'INBOX.Clients']
    assert len(classify) == 2 and len(states) == 2
    assert all(state == {'1': 'done'} for state in states.values())
    run()
    assert len(classify) == 2
    log = metrics.summary('Succès')
    assert 'Dossier IA INBOX.Clients' in log and 'Nb déjà traités : 1' in log


@pytest.mark.parametrize('cancelled, sync', [(False, False), (True, True)])
def test_failed_source_only_or_cancelled_never_transfers(env, monkeypatch, cancelled, sync):
    account = main.load_config()['accounts'][0]
    account.update(bPretraitementIA=True, bActiverSynchro=sync)
    monkeypatch.setattr(main, 'preprocess', AsyncMock(side_effect=ai.PreprocessingError('Arrêt du prétraitement')))
    process = AsyncMock(side_effect=AssertionError('must not launch'))
    monkeypatch.setattr(main.asyncio, 'create_subprocess_exec', process)
    main.processes['a'] = {'cancelled': cancelled, 'process': None}
    asyncio.run(main.execute(account, {'pseudo': 'Alice'}))
    assert process.await_count == 0
    assert main.load_config()['runs'][-1]['status'] == ('Annulée' if cancelled else 'Erreur')


def test_explicit_manual_folder_size_check_is_preserved(env, monkeypatch):
    account = main.load_config()['accounts'][0]
    account['options'] = ['--justfoldersizes']
    completed = type('Process', (), {'returncode': 0, 'wait': AsyncMock(),
                     'stdout': type('Output', (), {'read': AsyncMock(return_value=b'')})()})()
    process = AsyncMock(return_value=completed)
    monkeypatch.setattr(main.asyncio, 'create_subprocess_exec', process)
    main.processes['a'] = {'cancelled': False, 'process': None}
    asyncio.run(main.execute(account, {'pseudo': 'Alice'}))
    assert '--justfoldersizes' in process.call_args.args
    assert '--nofoldersizes' not in process.call_args.args
