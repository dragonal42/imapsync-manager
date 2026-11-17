import re

import pytest
from fastapi import HTTPException

from main import folder_settings, folder_exclusion_regex, transfer_folder_options
from test_tenants import env


def test_glob_excludes_accounting_folders_and_descendants_only():
    rx = re.compile(folder_exclusion_regex("comptable *"))
    for folder in ("comptable 2025", "INBOX.Comptable 2026", "INBOX/comptable 2026/Factures"):
        assert rx.search(folder)
    assert not rx.search("INBOX.non comptable 2026")
    assert not rx.search("INBOX.comptable")
    assert re.search(folder_exclusion_regex("Compta (2026)"), "INBOX.Compta (2026)")
    assert not re.search(folder_exclusion_regex("Compta (2026)"), "INBOX.Compta 2026")


def test_folder_modes_and_validation():
    config = folder_settings({"folder_exclusions": "comptable *\r\ncomptable *"})
    assert config["folder_exclusions"] == ["comptable *"]
    assert transfer_folder_options(config, {})[1] == "ascending"
    assert transfer_folder_options(config, {"last_folder_order": "ascending"})[0][0] == "--folderreverse"
    assert transfer_folder_options(config, {"last_folder_order": "descending"})[1] == "ascending"
    assert transfer_folder_options({"folder_order": "descending"}, {}) == (["--folderreverse"], "descending")
    assert transfer_folder_options({"folder_order": "ascending"}, {"last_folder_order": "ascending"}) == ([], "ascending")
    with pytest.raises(HTTPException):
        folder_settings({"folder_order": "bad"})


def test_inventory_decodes_and_excludes_only_the_selected_path():
    from sender_export import listed_folder_names
    names = listed_folder_names([b'() "." "INBOX.&AMk-t&AMk-"',
                                 b'(\\Noselect) "." Virtual', b'() "." "INBOX.test*"'])
    assert names == ['INBOX.test*', 'INBOX.ÉtÉ']
    rx = re.compile(folder_exclusion_regex('=' + names[1]))
    assert rx.search('INBOX.&AMk-t&AMk-')
    assert not rx.search('INBOX.&AMk-t&AMk-.Child')
    assert re.search(folder_exclusion_regex('=' + names[0]), 'INBOX.test*')
    assert not re.search(folder_exclusion_regex('=' + names[0]), 'INBOX.test2026')


def test_manual_folder_list_requires_session_and_passes_only_source(env, monkeypatch):
    import main
    from test_tenants import client
    calls = []
    def extract(account):
        calls.append(account)
        return ['INBOX.Exemple']
    monkeypatch.setattr(main, 'extract_folders', extract)
    form = {'host1': 'source.example.org', 'user1': 'alice', 'password1': 'secret', 'host2': 'unused'}
    assert client().post('/manual/folders', data=form).status_code in (303, 401)
    reply = client('alice@example.com').post('/manual/folders', data=form)
    assert reply.json() == {'folders': ['INBOX.Exemple']}
    assert reply.headers['cache-control'] == 'no-store'
    assert calls == [{'host1': 'source.example.org', 'user1': 'alice', 'authmech1': 'PLAIN', 'pass1': 'secret'}]


def test_folder_extraction_only_lists_folders(monkeypatch):
    from unittest.mock import Mock
    import sender_export as export
    mailbox = Mock(spec=['login', 'list', 'logout'])
    mailbox.login.return_value = ('OK', [])
    mailbox.list.return_value = ('OK', [b'() "." INBOX.ALLIANZ'])
    monkeypatch.setattr(export.imaplib, 'IMAP4_SSL', lambda *args, **kwargs: mailbox)
    assert export.extract_folders(dict(host1='source', user1='user', pass1='secret', authmech1='PLAIN')) == ['INBOX.ALLIANZ']
    assert [x[0] for x in mailbox.method_calls] == ['login', 'list', 'logout']
    assert re.search(folder_exclusion_regex('ALLIANZ'), 'INBOX.ALLIANZ')
