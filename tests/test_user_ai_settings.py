import asyncio
import copy

import pytest
import main
import ai_preprocessing as ai
import ai_quotas
from test_tenants import env, client, form
from test_mistral_rate import rate_clock


def personal(email):
    return main.user_ai_settings(main.load_config(), email)


def test_migration_only_to_principal_admin_and_idempotent(env):
    config = main.load_config()
    config.update(sApiKeyMistral='legacy-secret', mistral_model='legacy-model', gemini_batch_size=7)
    main.save_config(config)
    migrated = main.load_config()
    assert all(k not in migrated for k in main.AI_KEYS)
    assert personal('admin@example.com')['sApiKeyMistral'] == 'legacy-secret'
    assert personal('admin@example.com')['gemini_batch_size'] == 7
    assert 'sApiKeyMistral' not in personal('alice@example.com')
    assert personal('alice@example.com')['mistral_model'] != 'legacy-model'
    assert main.load_config() == migrated


def test_user_and_admin_edit_isolation_and_secret_retention(env):
    alice, admin = client('alice@example.com'), client('admin@example.com')
    assert alice.post('/ai/settings', data={'sApiKeyGemini':'alice-secret', 'gemini_model':'alice-model'}).status_code == 303
    assert admin.post('/ai/settings', data={'sApiKeyGemini':'admin-secret'}).status_code == 303
    assert admin.post('/ai/settings', data={'owner':'bob@example.com','sApiKeyGemini':'bob-secret','gemini_batch_size':9}).status_code == 303
    assert personal('alice@example.com')['gemini_model'] == 'alice-model'
    assert personal('admin@example.com')['sApiKeyGemini'] == 'admin-secret'
    assert personal('bob@example.com')['gemini_batch_size'] == 9
    for verb in ('get', 'post'):
        response = alice.get('/ai/settings?owner=bob@example.com') if verb == 'get' else alice.post('/ai/settings', data={'owner':'bob@example.com', 'clear_sApiKeyGemini':'on'})
        assert response.status_code == 403
    assert alice.post('/ai/settings',data={'sApiKeyGemini':''}).status_code == 303
    assert personal('alice@example.com')['sApiKeyGemini'] == 'alice-secret'
    for browser, path in [(alice,'/ai/settings'),(admin,'/ai/settings?owner=bob@example.com'),(admin,'/admin'),(alice,'/dashboard')]:
        page = browser.get(path).text
        assert all(secret not in page for secret in ('alice-secret','admin-secret','bob-secret'))
    assert alice.post('/ai/settings',data={'clear_sApiKeyGemini':'on'}).status_code == 303
    assert 'sApiKeyGemini' not in personal('alice@example.com')
    assert personal('bob@example.com')['sApiKeyGemini'] == 'bob-secret'


@pytest.mark.parametrize('engine', ['Mistral','Gemini'])
def test_execute_uses_owner_even_when_admin_launches(env, monkeypatch, engine):
    admin = client('admin@example.com')
    prefix = engine.lower()
    for email in ('admin@example.com','alice@example.com','bob@example.com'):
        admin.post('/ai/settings', data={'owner':email,'sApiKey'+engine:email+'-secret',prefix+'_model':email.split('@')[0]+'-model',prefix+'_batch_size':3})
    account = copy.deepcopy(main.load_config()['accounts'][0])
    account.update(bActiverSynchro=False, bPretraitementIA=True, sMoteurIA=engine)
    calls = []
    async def preprocess(account, token, key, *args):
        calls.append(key)
        assert key == 'alice@example.com-secret'
        assert account[prefix+'_model'] == 'alice-model' and account[prefix+'_batch_size'] == 3
    monkeypatch.setattr(main, 'preprocess', preprocess)
    main.processes['a'] = {'cancelled':False,'process':None}
    asyncio.run(main.execute(account, {'pseudo':'Administrateur'}))
    assert calls == ['alice@example.com-secret']
    assert 'alice@example.com-secret' not in str(main.load_config()['accounts'])
    assert 'alice@example.com-secret' not in str(main.load_config()['runs'])


def test_manual_uses_session_owner_ignoring_posted_owner(env, monkeypatch):
    client('alice@example.com').post('/ai/settings',data={'sApiKeyMistral':'alice-secret'})
    seen = []
    async def preprocess(account, token, key, *args):
        seen.append((account['owner'],key))
    monkeypatch.setattr(main,'preprocess',preprocess)
    response = client('alice@example.com').post('/cgi-bin/imapsync',data={'action':'ai','owner':'admin@example.com','host1':'mail','user1':'alice','password1':'secret','sMoteurIA':'Mistral'})
    assert response.status_code == 200
    assert seen == [('alice@example.com','alice-secret')]


def test_quotas_independent_by_owner(env):
    data = {}
    def save(value): data.update(value)
    async def run():
        for owner in ('alice','bob'):
            await ai_quotas.reserve('Mistral','same',{'owner':owner,'mistral_rpd':1},1,{'cancelled':False},lambda _:None,lambda:data,save)
        with pytest.raises(ai_quotas.QuotaExceeded):
            await ai_quotas.reserve('Mistral','same',{'owner':'alice','mistral_rpd':1},1,{'cancelled':False},lambda _:None,lambda:data,save)
    asyncio.run(run())
    assert len(data) == 2


def test_rate_independent_by_owner(rate_clock):
    calls=[]
    async def call(*args): calls.append(rate_clock[0]); return 'legitimate'
    async def run():
        for owner in ('alice','bob','alice'):
            await ai.provider_call({'owner':owner,'mistral_rps':1},'key',{}, {'cancelled':False},call,lambda _:None)
    asyncio.run(run())
    assert calls == [1000,1000,1001]


def test_account_cannot_use_admin_key(env):
    admin = client('admin@example.com')
    admin.post('/ai/settings',data={'sApiKeyMistral':'admin-only'})
    data = form(bPretraitementIA='on')
    assert client('alice@example.com').post('/account/add', data=data).status_code == 400
    admin.post('/ai/settings',data={'owner':'alice@example.com','sApiKeyMistral':'alice-only'})
    assert client('alice@example.com').post('/account/add',data=data).status_code == 303
