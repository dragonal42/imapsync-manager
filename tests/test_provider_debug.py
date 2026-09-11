import asyncio

import pytest
import requests

import ai_preprocessing as ai
import main
from test_ai_preprocessing import Mailbox
from test_tenants import env
from test_mistral_rate import rate_clock


def response():
    result = requests.Response()
    result.status_code = 429
    result._content = b'{"message":"Rate limit exceeded", "type":"rate_limited", "code":"1300"}'
    result.headers.update({'retry-after': '15', 'x-request-id': 'request-123',
                           'x-ratelimit-remaining-tokens': '0', 'set-cookie': 'SECRET'})
    return result


def test_diagnostics_mask_secrets_and_email_metadata():
    result = response()
    result._content = b'{"error":{"message":"key SECRET, Private subject, a@example.com https://private.example", "code":1300}, "input":"DO NOT PRINT"}'
    detail = ai.provider_debug(result, 'SECRET', {'subject': 'Private subject'})
    assert '1300' in detail and 'retry-after' in detail and 'request-123' in detail
    assert all(value not in detail for value in ('SECRET', 'Private subject', 'a@example.com', 'https://private.example', 'DO NOT PRINT', 'set-cookie'))


@pytest.mark.parametrize('debug', [False, True])
def test_error_immediately_after_call_and_details_only_debug(env, monkeypatch, debug, rate_clock):
    config = main.load_config()
    config.update(log_debug=debug, sApiKeyMistral='SECRET')
    main.save_config(config)
    mailbox = Mailbox()
    monkeypatch.setattr(ai.imaplib, 'IMAP4_SSL', lambda *a, **kw: mailbox)
    monkeypatch.setattr(ai.requests, 'post', lambda *a, **kw: response())
    account = config['accounts'][0]
    account.update(bActiverSynchro=False, bPretraitementIA=True, sMoteurIA='Mistral')
    main.processes['a'] = {'cancelled': False, 'process': None}
    asyncio.run(main.execute(account, {'pseudo': 'Alice'}))
    log = main.load_config()['runs'][-1]['log']
    assert 'HTTP 429' in log
    assert ('Rate limit exceeded' in log) is debug
    if debug:
        assert log.index('appel Mistral') < log.index('[ERROR IA]') < log.index('LOGOUT')
        assert 'request-123' in log and 'retry-after' in log and 'remaining-tokens' in log
    assert 'SECRET' not in log


def test_non_json_body_not_dumped():
    result = response()
    result._content = b'<html>private email</html>'
    assert 'non JSON' in ai.provider_debug(result, 'key', {})
    assert 'private email' not in ai.provider_debug(result, 'key', {})
