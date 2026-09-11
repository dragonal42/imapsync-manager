import asyncio
import io
import json
import logging
import re
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

import main
import audit_logging
from test_tenants import env, client, form


@pytest.fixture
def audit_output(monkeypatch):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter('%(message)s'))
    monkeypatch.setattr(audit_logging.logger, 'handlers', [handler])
    return stream


def test_request_and_real_login_are_distinct(env, monkeypatch, audit_output):
    deliveries = []
    async def deliver(email, token): deliveries.append(token)
    monkeypatch.setattr(main, 'deliver_link', deliver)
    browser = client()
    browser.post('/login', data={'email': 'Alice@EXAMPLE.com'})
    text = audit_output.getvalue()
    assert '[MAGIC_LINK_REQUEST]' in text and 'pseudo="Alice" | email="alice@example.com"' in text
    assert '"outcome":"accepted"' in text and 'LOGIN_SUCCESS' not in text
    token = deliveries[0]
    browser.get('/auth/verify?token=' + token)
    assert 'LOGIN_SUCCESS' not in audit_output.getvalue()
    result = browser.post('/auth/verify', data={'token': token})
    assert result.status_code == 303
    browser.post('/auth/verify', data={'token': token})
    text = audit_output.getvalue()
    assert text.count('[LOGIN_SUCCESS]') == 1
    assert token not in text
    assert browser.cookies.get(main.COOKIE) not in text
    assert re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} \[INFO\]', text)


def test_unknown_and_limited_requests(env, monkeypatch, audit_output):
    monkeypatch.setattr(main, 'deliver_link', AsyncMock())
    browser = client()
    unknown = browser.post('/login', data={'email': 'nobody@example.com'})
    known = browser.post('/login', data={'email': 'alice@example.com'})
    assert known.text == unknown.text
    for _ in range(3):
        browser.post('/login', data={'email': 'alice@example.com'})
    text = audit_output.getvalue()
    assert '"outcome":"unknown_user"' in text and '"outcome":"rate_limited"' in text
    assert 'LOGIN_SUCCESS' not in text


@pytest.mark.parametrize('failed', [False, True])
def test_smtp_result_without_sensitive_error(env, monkeypatch, audit_output, failed):
    def send(*args):
        if failed: raise RuntimeError('SMTP password private-secret')
    monkeypatch.setattr(main, 'send_magic_link', send)
    asyncio.run(main.deliver_link('alice@example.com', 'one-time-secret'))
    text = audit_output.getvalue()
    assert ('[MAGIC_LINK_DELIVERY_FAILED]' if failed else '[MAGIC_LINK_SENT]') in text
    assert 'one-time-secret' not in text and 'private-secret' not in text


def test_delete_inline_configuration_redacted_after_success(env, audit_output):
    config = main.load_config()
    account = config['accounts'][0]
    account.update(refresh1='refresh-secret', token2='token-secret', nested={'api_key': 'api-secret'},
                   options=['--password1', 'argv-secret'])
    main.save_config(config)
    browser = client('alice@example.com')
    assert browser.post('/account/delete/b').status_code == 404
    main.processes['a'] = {'process': None}
    assert browser.post('/account/delete/a').status_code == 409
    assert audit_output.getvalue() == ''
    main.processes.clear()
    assert browser.post('/account/delete/a').status_code == 303
    lines = audit_output.getvalue().splitlines()
    assert len(lines) == 1 and '[CONFIG_DELETED]' in lines[0]
    deleted = json.loads(lines[0].split(' | data=', 1)[1])['configuration']
    assert deleted['owner'] == 'alice@example.com' and deleted['label'] == 'ALICE_PRIVATE'
    assert deleted['pass1'] == audit_logging.MASK
    for secret in ('refresh-secret', 'token-secret', 'api-secret', 'argv-secret', '"secret"'):
        assert secret not in lines[0]
    assert all(a['id'] != 'a' for a in main.load_config()['accounts'])


def test_manual_request_and_completion_correlate(env, monkeypatch, audit_output):
    class Process:
        returncode = 0
        stdout = type('Output', (), {'read': AsyncMock(side_effect=[b'Messages transferred : 2\n', b''])})()
        wait = AsyncMock()
    monkeypatch.setattr(main.asyncio, 'create_subprocess_exec', AsyncMock(return_value=Process()))
    result = client('alice@example.com').post('/cgi-bin/imapsync', data=form(password1='source-secret', password2='dest-secret'))
    assert result.status_code == 200
    text = audit_output.getvalue()
    assert '[MANUAL_SYNC_REQUESTED]' in text and '[MANUAL_SYNC_FINISHED]' in text
    assert text.count(result.json()['run_id']) == 2
    assert 'source-secret' not in text and 'dest-secret' not in text
    finished = json.loads(text.splitlines()[-1].split(' | data=', 1)[1])
    assert finished['status'] == 'Succès' and finished['metrics']['transferred'] == 2
    assert main.load_config()['log_debug'] is False


def test_single_line_no_injected_events(audit_output):
    audit_logging.audit('CONFIG_DELETED', {'pseudo': 'Alice\n[LOGIN_SUCCESS]', 'email': 'a\r\n@example.com'},
                        configuration={'label': 'hello\nworld\u2028test', 'pass1': 'hidden'})
    text = audit_output.getvalue()
    assert len(text.splitlines()) == 1 and 'hidden' not in text
    assert '\\n[LOGIN_SUCCESS]' in text


def test_stdout_without_uvicorn_or_debug_configuration():
    result = subprocess.run([sys.executable, '-c',
        "from audit_logging import audit; audit('LOGIN_SUCCESS', {'pseudo':'Alice','email':'alice@example.com'})"],
        capture_output=True, text=True, check=True)
    assert '[INFO] [LOGIN_SUCCESS]' in result.stdout
    assert result.stderr == ''
