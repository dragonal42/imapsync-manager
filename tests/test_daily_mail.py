import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize('recipient, users, exit_code, expected', [
    ('report@example.com', [{'role': 'admin', 'email': 'admin@example.com'}], 0, 'report@example.com'),
    ('', [{'role': 'admin', 'email': 'admin@example.com'}], 0, 'admin@example.com'),
    ('report@example.com', [], 1, 'report@example.com'),
    ('', [], 0, None),
])
def test_daily_report_recipient_and_retry(tmp_path, recipient, users, exit_code, expected):
    bash = 'C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' else shutil.which('bash')
    if not bash or not Path(bash).exists():
        pytest.skip('Bash unavailable')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'report_email': recipient, 'users': users}), encoding='utf-8')
    report = tmp_path / 'daily_errors.log'
    report.write_text('Avertissement IA : test\n', encoding='utf-8')
    commands = tmp_path / 'bin'
    commands.mkdir()
    python = commands / 'python3'
    python.write_text(f'#!/bin/bash\nexec "{Path(sys.executable).as_posix()}" "$@"\n', encoding='utf-8')
    msmtp = commands / 'msmtp'
    msmtp.write_text('#!/bin/bash\nprintf "%s" "$2" > "$CAPTURE_TO"\ncat > "$CAPTURE_BODY"\nexit "$SEND_EXIT"\n', encoding='utf-8')
    for command in (python, msmtp):
        command.chmod(0o755)
    capture = tmp_path / 'recipient'
    body = tmp_path / 'body'
    env = {**os.environ, 'CONFIG_FILE': str(config), 'REPORT_FILE': str(report),
           'CAPTURE_TO': str(capture), 'CAPTURE_BODY': str(body), 'SEND_EXIT': str(exit_code),
           'TEST_BIN': commands.as_posix()}
    script = Path(__file__).resolve().parents[1] / 'daily_mail.sh'
    # Set PATH inside Bash so Windows drive-letter colons are converted correctly.
    subprocess.run([bash, '-c', 'export PATH="$(cd "$TEST_BIN" && pwd):$PATH"; bash "$1"',
                    'test', script.as_posix()], env=env, check=True, capture_output=True)
    if expected:
        assert capture.read_text() == expected
        assert 'Avertissement IA' in body.read_text(encoding='utf-8')
    else:
        assert not capture.exists()
    assert bool(report.read_text(encoding='utf-8')) == (exit_code != 0 or expected is None)
