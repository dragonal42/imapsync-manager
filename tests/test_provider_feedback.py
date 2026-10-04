import asyncio
import copy
import json
from datetime import datetime, timezone

import pytest
import requests
import ai_preprocessing as ai
import ai_quotas as quotas
from test_mistral_rate import rate_clock


def response(status=200,headers=None,body=None):
    result=requests.Response()
    result.status_code=status
    result.headers.update(headers or {})
    result._content=json.dumps(body if body is not None else {
        'choices':[{'finish_reason':'stop','message':{'content':'{"verdict":"legitimate"}'}}],
        'usage':{'total_tokens':12}}).encode()
    return result


def runner(monkeypatch,responses):
    sent=[]; events=[]; data={}
    def post(*args,**kwargs):
        sent.append(kwargs)
        return responses.pop(0)
    monkeypatch.setattr(ai.requests,'post',post)
    def save(value): data.clear();data.update(copy.deepcopy(value))
    settings={'owner':'alice','mistral_rps':100,'_quota_read':lambda:copy.deepcopy(data),'_quota_save':save}
    async def call(fn,*args): return fn(*args)
    def run(key='key',owner='alice'):
        return asyncio.run(ai.provider_call({**settings,'owner':owner},key,{}, {'cancelled':False},call,events.append))
    return run,sent,events,data


@pytest.mark.parametrize('code',[1300,'1300'])
def test_zero_capacity_no_retry_persists_and_next_day_probe(rate_clock,monkeypatch,code):
    blocked=response(429,{'x-ratelimit-limit-req-minute':'0','x-ratelimit-remaining-req-minute':'0'},
                     {'type':'rate_limited','code':code})
    run,sent,events,data=runner(monkeypatch,[blocked,response(),response()])
    with pytest.raises(ai.PreprocessingError,match='capacité fournisseur nulle') as caught: run()
    assert caught.value.quota_exhausted and caught.value.no_retry and len(sent)==1
    assert rate_clock[0]==1000
    with pytest.raises(ai.QuotaExhausted): run()
    assert len(sent)==1
    assert any(':provider:' in k for k in data)
    assert 'key' not in json.dumps(data)
    assert run(owner='bob')=='legitimate'  # Another tenant is independent.
    rate_clock[0]=86400
    assert run()=='legitimate' and len(sent)==3


def test_1300_alone_is_not_called_daily_quota(rate_clock,monkeypatch):
    run,sent,events,data=runner(monkeypatch,[response(429,{}, {'code':'1300','message':'Rate limit exceeded'})])
    with pytest.raises(ai.PreprocessingError,match='fenêtre de quota non précisée') as caught: run()
    assert caught.value.no_retry and not caught.value.quota_exhausted
    assert len(sent)==1 and rate_clock[0]==1000


def test_key_rotation_releases_stale_suspension(rate_clock,monkeypatch):
    run,sent,events,data=runner(monkeypatch,[response(429,{'x-ratelimit-limit-req-minute':'0'},{'code':'1300'}),response()])
    with pytest.raises(ai.PreprocessingError): run()
    assert run(key='new-secret')=='legitimate'
    assert 'new-secret' not in json.dumps(data)


@pytest.mark.parametrize('remaining',[0,1,2])
def test_200_remaining_minute_anticipates_wait(rate_clock,monkeypatch,remaining):
    run,sent,events,data=runner(monkeypatch,[response(headers={
        'x-ratelimit-remaining-req-minute':str(remaining),'x-ratelimit-reset-req-minute':'12'}),response()])
    assert run()=='legitimate' and rate_clock[0]==1000
    assert run()=='legitimate' and rate_clock[0]>=1012
    assert any('[IA LIMITES]' in str(event) for event in events)


def test_last_daily_success_processed_then_no_next_call(rate_clock,monkeypatch):
    run,sent,events,data=runner(monkeypatch,[response(headers={'x-ratelimit-remaining-req-day':'0'})])
    result=run()
    assert result=='legitimate' and result.usage=={'total':12}
    with pytest.raises(ai.QuotaExhausted,match='journalier'): run()
    assert len(sent)==1


def test_temporary_429_honors_header_reset_and_retry_after(rate_clock,monkeypatch):
    run,sent,events,data=runner(monkeypatch,[response(429,{
        'retry-after':'7','x-ratelimit-remaining-req-minute':'0','x-ratelimit-reset-req-minute':'12'},
        {'code':'temporary'}),response()])
    assert run()=='legitimate' and len(sent)==2 and rate_clock[0]>=1012


def test_gemini_depleted_credits_are_durable_and_masked(rate_clock):
    result=response(429,body={'error':{'message':'Your prepayment credits are depleted. SECRET https://billing.example','code':429}})
    feedback=quotas.provider_feedback('Gemini',result)
    assert feedback['durable'] and feedback['no_retry'] and feedback['until']>rate_clock[0]
    assert 'SECRET' not in json.dumps(feedback) and 'billing.example' not in json.dumps(feedback)


def test_monthly_deadline_and_malformed_headers(rate_clock):
    rate_clock[0]=datetime(2026,10,4,tzinfo=timezone.utc).timestamp()
    feedback=quotas.provider_feedback('Mistral',response(headers={
        'x-ratelimit-remaining-tokens-month':'0','x-ratelimit-reset-tokens-month':'nan','authorization':'SECRET'}))
    assert feedback['until']==datetime(2026,11,1,tzinfo=timezone.utc).timestamp()
    assert 'SECRET' not in json.dumps(feedback)


def test_cancel_while_waiting_for_success_header_reset(rate_clock,monkeypatch):
    run,sent,events,data=runner(monkeypatch,[response(headers={'x-ratelimit-remaining-req-minute':'1'})])
    run()
    active={'cancelled':False}
    settings={'owner':'alice','_quota_read':lambda:copy.deepcopy(data),'_quota_save':lambda value:None}
    def progress(_): active['cancelled']=True
    async def call(*args): raise AssertionError('No HTTP after stop')
    with pytest.raises(ai.PreprocessingError,match='Arrêt demandé'):
        asyncio.run(ai.provider_call(settings,'key',{},active,call,progress))
    assert len(sent)==1 and rate_clock[0]<1060


def test_concurrent_suspension_checked_after_local_reservation(rate_clock,monkeypatch):
    run,sent,events,data=runner(monkeypatch,[])
    async def reserve(engine,model,settings,*args):
        feedback=quotas.provider_feedback(engine,response(429,{'x-ratelimit-limit-req-minute':'0'},{'code':'1300'}))
        quotas.provider_state(settings,engine,model,'key',feedback)
        return 'fake','reservation'
    monkeypatch.setattr(quotas,'reserve',reserve)
    with pytest.raises(ai.QuotaExhausted): run()
    assert not sent
