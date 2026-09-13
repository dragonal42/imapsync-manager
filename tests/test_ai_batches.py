import asyncio
import json

import pytest

import ai_preprocessing as ai
import main
from test_ai_preprocessing import Mailbox
from test_tenants import env, client
from test_mistral_rate import rate_clock


@pytest.mark.parametrize('engine', ['Mistral', 'Gemini'])
@pytest.mark.parametrize('invalid', [None, 'missing', 'duplicate', 'unknown', 'verdict'])
def test_batches_validate_before_moves_and_count_once(monkeypatch, rate_clock, engine, invalid):
    mailbox = Mailbox()
    original = mailbox.uid
    mailbox.uid = lambda command, *args: ('OK', [b'1 2 3']) if command == 'SEARCH' else original(command, *args)
    monkeypatch.setattr(ai.imaplib, 'IMAP4_SSL', lambda *a, **kw: mailbox)
    calls, events, state = [], [], {}

    class Response:
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def json(self):
            rows = [{'id': 'mail-003', 'verdict': 'legitimate'},
                    {'id': 'mail-002', 'verdict': 'uncertain'},
                    {'id': 'mail-001', 'verdict': 'spam'}]
            if invalid == 'missing': rows.pop()
            if invalid == 'duplicate': rows.append(rows[0])
            if invalid == 'unknown': rows[0]['id'] = 'other'
            if invalid == 'verdict': rows[0]['verdict'] = 'delete'
            text = json.dumps({'results': rows})
            if engine == 'Mistral':
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}],
                        'usage': {'prompt_tokens': 100, 'completion_tokens': 30, 'total_tokens': 130}}
            return {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': text}]}}],
                    'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 30, 'totalTokenCount': 130}}

    monkeypatch.setattr(ai.requests, 'post', lambda *a, **kw: calls.append(kw['json']) or Response())
    account = {'owner': 'alice', 'host1': 'mail', 'user1': 'alice', 'pass1': 'secret',
               'bPretraitementIA': True, 'sMoteurIA': engine, engine.lower() + '_batch_size': 5}
    def run():
        asyncio.run(ai.preprocess(account, '', 'key', {'cancelled': False}, lambda _: state,
                                  lambda *a: None, events.append))
    if invalid:
        with pytest.raises(ai.PreprocessingError, match='lot invalide'): run()
        assert not state
        assert not any(cmd in ('COPY', 'STORE', 'EXPUNGE') for cmd, _ in mailbox.calls)
    else:
        run()
        assert state == {'1': 'done', '2': 'done', '3': 'done'}
        assert [args[0] for cmd, args in mailbox.calls if cmd == 'COPY'] == [b'1']
        assert len([e for e in events if isinstance(e, dict) and 'verdict' in e]) == 3
    assert len(calls) == 1
    assert len([e for e in events if isinstance(e, dict) and 'usage' in e]) == 1
    if engine == 'Mistral':
        payload = json.loads(calls[0]['messages'][1]['content'])
        assert calls[0]['max_tokens'] > 128
    else:
        payload = json.loads(calls[0]['contents'][0]['parts'][0]['text'])
    assert len(payload['emails']) == 3


def test_global_batch_settings(env):
    admin = client('admin@example.com')
    config = main.load_config()
    assert config['mistral_batch_size'] == config['gemini_batch_size'] == 5
    assert admin.post('/ai/settings', data={'mistral_batch_size': '3', 'gemini_batch_size': '8'}).status_code == 303
    assert main.load_config()['mistral_batch_size'] == 3
    assert main.load_config()['gemini_batch_size'] == 8
    assert 'name="mistral_batch_size"' in admin.get('/admin').text
    assert 'name="gemini_batch_size"' in admin.get('/admin').text
    for value in ('0', '51', '1.5', 'oops'):
        assert admin.post('/ai/settings', data={'mistral_batch_size': value}).status_code == 400
    assert client('alice@example.com').post('/ai/settings', data={'mistral_batch_size': '4'}).status_code == 403


@pytest.mark.parametrize('bound', ['count', 'bytes', 'tokens'])
def test_batch_splitting_and_simulation(monkeypatch, rate_clock, bound):
    mailbox = Mailbox()
    original = mailbox.uid
    mailbox.uid = lambda cmd, *args: ('OK', [b'1 2 3 4 5']) if cmd == 'SEARCH' else original(cmd, *args)
    monkeypatch.setattr(ai.imaplib, 'IMAP4_SSL', lambda *a, **kw: mailbox)
    calls, states = [], []
    def classify(engine, key, metadata, model):
        calls.append(metadata)
        return ai.BatchClassification({row['id']: 'spam' for row in metadata['emails']}, {})
    monkeypatch.setattr(ai, 'classify', classify)
    metadata = ai.compact_message(b'Subject: test\r\n\r\nhttps://spam.example')
    two = {'emails': [{'id': f'mail-{i+1:03d}', **metadata} for i in range(2)]}
    account = {'owner': 'alice', 'host1': 'mail', 'user1': 'alice', 'pass1': 'secret',
               'bPretraitementIA': True, 'sMoteurIA': 'Mistral', 'mistral_batch_size': 2 if bound == 'count' else 5,
               'manual_ai': True, 'ai_dry': True}
    if bound == 'bytes': monkeypatch.setattr(ai, 'MAX_BATCH_BYTES', len(json.dumps(two).encode()))
    if bound == 'tokens': account['mistral_tpm'] = ai.request_estimate('Mistral', two)
    asyncio.run(ai.preprocess(account, '', 'key', {'cancelled': False}, lambda _: {},
                             lambda *args: states.append(args), lambda *a: None))
    assert [len(call['emails']) for call in calls] == [2, 2, 1]
    assert not states
    assert not any(cmd in ('COPY', 'STORE', 'EXPUNGE') for cmd, _ in mailbox.calls)
