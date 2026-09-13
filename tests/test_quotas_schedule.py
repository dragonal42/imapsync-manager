import asyncio
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main
import ai_preprocessing as ai
import ai_quotas as quotas
from test_tenants import env, client, form
from test_mistral_rate import rate_clock


def test_intervals_migrate_and_default_only_affects_new_accounts(env):
    config = main.load_config()
    config['poll_interval'] = 12
    for account in config['accounts']: account.pop('sync_interval', None)
    main.save_config(config)
    config = main.load_config()
    assert config['poll_interval'] == 15
    assert all(a['sync_interval'] == 15 for a in config['accounts'])
    assert client('admin@example.com').post('/settings', data={'poll_interval': '30'}).status_code == 303
    assert client('alice@example.com').post('/account/add', data=form()).status_code == 303
    config = main.load_config()
    assert config['accounts'][0]['sync_interval'] == 15 and config['accounts'][-1]['sync_interval'] == 30
    assert client('alice@example.com').post('/account/edit/a', data=form(sync_interval='10')).status_code == 303
    assert client('alice@example.com').post('/account/edit/a', data=form(sync_interval='7')).status_code == 400
    assert client('admin@example.com').post('/settings', data={'poll_interval': '7'}).status_code == 400


def test_due_dispatch_no_overlap_and_paused(env, monkeypatch):
    now = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
    config = main.load_config()
    config['accounts'][0].update(sync_interval=15, last_started=(now-timedelta(minutes=14)).isoformat())
    config['accounts'][1].update(sync_interval=5, last_started=(now-timedelta(minutes=5)).isoformat())
    main.save_config(config)
    started = []
    def spawn(coroutine):
        started.append(coroutine)
        coroutine.close()
    monkeypatch.setattr(main, 'spawn', spawn)
    main.dispatch_due_accounts(now)
    assert set(main.processes) == {'b'}
    main.dispatch_due_accounts(now + timedelta(minutes=1))
    assert set(main.processes) == {'a', 'b'} and len(started) == 2
    assert not main.account_due({'schedule_state': 'PAUSED', 'sync_interval': 5}, now)


def test_gemini_admin_quota_validation(env):
    data = {'gemini_model': 'gemini-2.5-flash-lite', 'gemini_rps': '.2', 'gemini_rpm': '10', 'gemini_rpd': '50', 'gemini_tpm': '20000'}
    assert client('alice@example.com').post('/ai/settings', data=data).status_code == 403
    assert client('admin@example.com').post('/ai/settings', data=data).status_code == 303
    assert main.load_config()['gemini_rpd'] == 50
    assert client('admin@example.com').post('/ai/settings', data={'gemini_tpm': '-1'}).status_code == 400


def storage():
    saved = {}
    def read(): return copy.deepcopy(saved)
    def write(data): saved.clear(); saved.update(copy.deepcopy(data))
    return read, write


def test_daily_quota_persists_and_resets_at_pacific_midnight(rate_clock):
    rate_clock[0] = datetime(2026, 9, 13, 6, 59, tzinfo=timezone.utc).timestamp()
    read, write = storage()
    settings = {'gemini_rpd': 1}
    def reserve(): return asyncio.run(quotas.reserve('Gemini', 'model', settings, 20, {'cancelled': False}, lambda _: None, read, write))
    reserve()
    with pytest.raises(quotas.QuotaExceeded, match='quotidien'): reserve()
    rate_clock[0] += 60
    reserve()
    assert read()['Gemini:model']['daily'] == 1


def test_tpm_wait_and_cancellation_and_reconciliation(rate_clock, monkeypatch):
    read, write = storage()
    settings = {'gemini_tpm': 100, 'gemini_rpm': 10}
    active = {'cancelled': False}
    reservation = asyncio.run(quotas.reserve('Gemini', 'model', settings, 80, active, lambda _: None, read, write))
    quotas.reconcile(reservation, {'input': 90}, 'Gemini', read, write)
    assert read()['Gemini:model']['minute'][0]['tokens'] == 90
    def progress(_): active['cancelled'] = True
    with pytest.raises(quotas.QuotaExceeded, match='Arrêt'):
        asyncio.run(quotas.reserve('Gemini', 'model', settings, 80, active, progress, read, write))
    rate_clock[0] += 61
    active['cancelled'] = False
    asyncio.run(quotas.reserve('Gemini', 'model', settings, 80, active, lambda _: None, read, write))
    assert len(read()['Gemini:model']['minute']) == 1


def test_oversize_local_token_quota_never_sends(rate_clock):
    read, write = storage()
    with pytest.raises(quotas.QuotaExceeded, match='supérieure'):
        asyncio.run(quotas.reserve('Mistral', 'model', {'mistral_tpm': 10}, 20, {'cancelled': False}, lambda _: None, read, write))
    assert read() == {}


def test_gemini_exponential_backoff_and_model(rate_clock, monkeypatch):
    monkeypatch.setattr(ai, '_other_next', {})
    starts = []
    async def call(fn, engine, key, metadata, model):
        assert engine == 'Gemini' and model == 'gemini-custom'
        starts.append(rate_clock[0])
        error = ai.PreprocessingError('HTTP 429')
        error.http_status = 429
        raise error
    with pytest.raises(ai.PreprocessingError):
        asyncio.run(ai.provider_call({'gemini_model': 'gemini-custom', 'gemini_rps': 100}, 'key', {}, {'cancelled': False}, call, lambda _: None, 'Gemini'))
    assert starts == [1000, 1002, 1006, 1014]
