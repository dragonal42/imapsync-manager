import time
from unittest.mock import Mock
import pytest
import main
from test_tenants import env, client


def manage(action, **data):
    return client('admin@example.com').post('/admin/users/'+action, data={'user_email':'alice@example.com', **data})


def test_disabled_revokes_session_links_oauth_and_blocks_scheduler(env, monkeypatch):
    alice=client('alice@example.com')
    auth=main.auth_data()
    auth['links']['existing']={'email':'alice@example.com','expires':time.time()+900}
    auth['oauth']['oauth']={'session':main.digest('session-alice@example.com'),'expires':time.time()+900}
    main.write_json(main.AUTH_FILE,auth)
    assert manage('status',enabled='false').status_code==303
    assert alice.get('/dashboard').headers['location']=='/login'
    auth=main.auth_data()
    assert not auth['links'] and not auth['oauth']
    assert not any(v['email']=='alice@example.com' for v in auth['sessions'].values())
    sender=Mock()
    monkeypatch.setattr(main,'send_magic_link',sender)
    alice.post('/login',data={'email':'alice@example.com'})
    sender.assert_not_called()
    assert client('admin@example.com').post('/account/run/a').status_code==409
    owners=[]
    def spawn(coro):
        owners.append(coro.cr_frame.f_locals['account']['owner']); coro.close()
    monkeypatch.setattr(main,'spawn',spawn)
    main.dispatch_due_accounts()
    assert owners==['bob@example.com']
    main.processes.clear()
    assert manage('status',enabled='true').status_code==303
    assert alice.get('/dashboard').headers['location']=='/login'
    assert client('alice@example.com').get('/dashboard').status_code==200


def test_edit_moves_ownership_settings_quota_and_preserves_ai_identity(env):
    config=main.load_config()
    target=next(u for u in config['users'] if u['email']=='alice@example.com')
    target['ai_settings']={'sApiKeyMistral':'private-key'}
    config['runs'].append({'id':'alice-run','owner':'alice@example.com','status':'Succès'})
    main.save_config(config)
    main.write_json(main.CONFIG_FILE.with_name('ai_quotas.json'),{'alice@example.com:Mistral:model':{'daily':12},'bob@example.com:Mistral:model':{'daily':9}})
    assert manage('edit',email='new@example.com',pseudo='Nouveau').status_code==303
    config=main.load_config()
    target=next(u for u in config['users'] if u['email']=='new@example.com')
    assert target['ai_identity']=='alice@example.com' and target['ai_settings']['sApiKeyMistral']=='private-key'
    assert target['pseudo']=='Nouveau'
    assert config['accounts'][0]['owner']=='new@example.com'
    assert config['runs'][-1]['owner']=='new@example.com'
    assert main.read_json(main.CONFIG_FILE.with_name('ai_quotas.json'),{})['new@example.com:Mistral:model']['daily']==12
    assert client('alice@example.com').get('/dashboard').status_code==303


def test_delete_cascade_keeps_other_user(env):
    config=main.load_config()
    config['runs'].append({'id':'alice-run','owner':'alice@example.com','status':'Succès'})
    next(u for u in config['users'] if u['email']=='alice@example.com').update(ai_settings={'sApiKeyGemini':'private'},sender_lists={'whitelist':['a@b.test']})
    main.save_config(config)
    main.write_json(main.CONFIG_FILE.with_name('ai_state.json'),{'alice-state':{'1':'done'},'bob-state':{'2':'done'}})
    main.write_json(main.CONFIG_FILE.with_name('ai_state_owners.json'),{'alice-state':'alice@example.com','bob-state':'bob@example.com'})
    main.write_json(main.CONFIG_FILE.with_name('ai_quotas.json'),{'alice@example.com:Mistral:model':{},'bob@example.com:Mistral:model':{}})
    assert manage('delete').status_code==303
    config=main.load_config()
    assert all(u['email']!='alice@example.com' for u in config['users'])
    assert [a['id'] for a in config['accounts']]==['b']
    assert [r['id'] for r in config['runs']]==['bobrun']
    assert main.read_json(main.CONFIG_FILE.with_name('ai_state.json'),{})=={'bob-state':{'2':'done'}}
    assert list(main.read_json(main.CONFIG_FILE.with_name('ai_quotas.json'),{}))==['bob@example.com:Mistral:model']
    assert client('admin@example.com').post('/admin/users',data={'email':'alice@example.com','pseudo':'Alice'}).status_code==303
    assert next(u for u in main.load_config()['users'] if u['email']=='alice@example.com')['ai_identity']!='alice@example.com'


@pytest.mark.parametrize('action,data',[('delete',{}),('status',{'enabled':'false'}),('edit',{'email':'new@example.com','pseudo':'New'})])
def test_permissions_protection_and_active_run_guard(env, action, data):
    assert client('alice@example.com').post('/admin/users/'+action,data={'user_email':'bob@example.com',**data}).status_code==403
    assert client('admin@example.com').post('/admin/users/'+action,data={'user_email':'admin@example.com',**data}).status_code==400
    main.processes['a']={'owner':'alice@example.com'}
    assert manage(action,**data).status_code==409


def test_duplicate_email_rejected(env):
    assert manage('edit',email='bob@example.com',pseudo='Alice').status_code==409
