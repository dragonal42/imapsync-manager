import asyncio
from types import SimpleNamespace

import pytest

import ai_preprocessing as ai
import main
from test_tenants import env, client


@pytest.fixture
def rate_clock(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ai, '_mistral_next', 0)
    monkeypatch.setattr(ai, '_rate_now', lambda: clock[0])
    async def sleep(delay):
        clock[0] += delay
        await asyncio.sleep(0)
    monkeypatch.setattr(ai, '_rate_sleep', sleep)
    return clock


def test_shared_rate_across_concurrent_accounts(rate_clock):
    starts = []
    async def call(*args):
        starts.append(rate_clock[0])
        return 'legitimate'
    async def run():
        await asyncio.gather(*(ai.mistral_call({'mistral_rps': 1}, 'key', {}, {'cancelled': False}, call, lambda _: None) for _ in range(4)))
    asyncio.run(run())
    assert len(starts) == 4 and all(b - a >= 1 for a, b in zip(starts, starts[1:]))


def test_429_retries_bounded_and_cooldown_shared(rate_clock):
    starts = []
    async def call(*args):
        starts.append(rate_clock[0])
        error = ai.PreprocessingError('HTTP 429')
        error.http_status = 429
        error.retry_after = 60
        raise error
    with pytest.raises(ai.PreprocessingError):
        asyncio.run(ai.mistral_call({}, 'key', {}, {'cancelled': False}, call, lambda _: None))
    assert starts == [1000, 1060, 1120]
    assert ai._mistral_next == 1180


def test_cancel_during_rate_wait(rate_clock):
    ai._mistral_next = rate_clock[0] + 60
    active = {'cancelled': False}
    def progress(_): active['cancelled'] = True
    async def call(*args): raise AssertionError('must not send')
    with pytest.raises(ai.PreprocessingError, match='Arrêt demandé'):
        asyncio.run(ai.mistral_call({}, 'key', {}, active, call, progress))


def test_retry_after_parsing():
    assert ai.retry_delay(SimpleNamespace(headers={'Retry-After': '12'})) == 12
    assert ai.retry_delay(SimpleNamespace(headers={})) == 60
    assert ai.retry_delay(SimpleNamespace(headers={'Retry-After': 'nan'})) == 60


def test_retry_success_preserves_attempt_usage_and_selected_model(rate_clock):
    events = []
    starts = []
    async def call(fn, engine, key, metadata, model):
        assert model == 'mistral-small-2603'
        starts.append(rate_clock[0])
        if len(starts) == 1:
            error = ai.PreprocessingError('HTTP 429')
            error.http_status = 429
            error.retry_after = 2
            error.usage = {'total': 7}
            raise error
        return 'legitimate'
    result = asyncio.run(ai.mistral_call({'mistral_model': 'mistral-small-2603'}, 'key', {},
                                        {'cancelled': False}, call, events.append))
    assert result == 'legitimate' and starts == [1000, 1002]
    assert {'usage': {'total': 7}} in events


def test_non_429_is_not_retried(rate_clock):
    calls = []
    async def call(*args):
        calls.append(1)
        error = ai.PreprocessingError('HTTP 401')
        error.http_status = 401
        raise error
    with pytest.raises(ai.PreprocessingError):
        asyncio.run(ai.mistral_call({}, 'key', {}, {'cancelled': False}, call, lambda _: None))
    assert len(calls) == 1


def test_admin_rate_model_settings(env):
    assert main.load_config()['mistral_rps'] == 1
    form = {'mistral_rps': '0.5', 'mistral_model': 'mistral-small-2603'}
    assert client('alice@example.com').post('/ai/settings', data=form).status_code == 403
    assert client('admin@example.com').post('/ai/settings', data=form).status_code == 303
    config = main.load_config()
    assert config['mistral_rps'] == .5 and config['mistral_model'] == 'mistral-small-2603'
    assert 'mistral-small-2603' in client('admin@example.com').get('/admin').text
    for value in ('0', 'nan', 'inf', '-1'):
        assert client('admin@example.com').post('/ai/settings', data={'mistral_rps': value}).status_code == 400
