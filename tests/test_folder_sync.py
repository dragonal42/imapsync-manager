import re

import pytest
from fastapi import HTTPException

from main import folder_settings, folder_exclusion_regex, transfer_folder_options


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
