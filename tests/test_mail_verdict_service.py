import asyncio
import copy
import pytest
import main
import mail_verdict_service as service
from ai_preprocessing import PreprocessingError
from run_logging import Classification
from test_tenants import env, client

RAW = b'From: submitter@example.org\r\nSubject: Test <script>alert(1)</script>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nOriginal <img src=x onerror=alert(1)>'


class Mailbox:
    def __init__(self): self.calls=[]; self.uidplus=True; self.fail_store=False
    def login(self,*args): return 'OK',[]
    def authenticate(self,*args): return 'OK',[]
    def capability(self): return 'OK',[b'IMAP4rev1 UIDPLUS' if self.uidplus else b'IMAP4rev1']
    def select(self,*args,**kwargs): self.readonly=kwargs['readonly']; return 'OK',[b'1']
    def response(self,*args): return 'UIDVALIDITY',[b'123']
    def logout(self): return 'BYE',[]
    def uid(self,command,*args):
        self.calls.append((command,args))
        if command=='SEARCH': return 'OK',[b'42']
        if command=='FETCH':
            if args[1]=='(BODY.PEEK[])': return 'OK',[(b'42',RAW)]
            return 'OK',[b'42 (RFC822.SIZE 200 FLAGS () INTERNALDATE "01-Oct-2026 10:00:00 +0000")']
        if command=='STORE' and self.fail_store: return 'NO',[b'failed']
        return 'OK',[]


def setup(monkeypatch, verdict='legitimate', simulation=False):
    mailbox=Mailbox()
    monkeypatch.setattr(service.imaplib,'IMAP4_SSL',lambda *a,**kw:mailbox)
    async def classify(*args): return Classification(verdict,{'total':12})
    monkeypatch.setattr(service,'provider_call',classify)
    account={'host1':'mail','user1':'service@example.org','pass1':'secret','authmech1':'PLAIN',
        'source_folder':'INBOX','period_days':0,'max_messages':50,'sMoteurIA':'Mistral','simulation':simulation}
    state={}; sent=[]
    def save(value): state.clear(); state.update(copy.deepcopy(value))
    def run(send=None, active=None):
        return asyncio.run(service.process_mailbox(account,'',{'owner':'admin'},'key','robot@example.org',
            active or {'cancelled':False}, lambda:copy.deepcopy(state), save, send or sent.append,lambda _:None))
    return mailbox,account,state,sent,run


@pytest.mark.parametrize('verdict,label',[('legitimate','SAIN'),('spam','SPAM'),('scam','DANGEREUX')])
def test_response_before_uid_delete_and_report_content(monkeypatch,verdict,label):
    mailbox,account,state,sent,run=setup(monkeypatch,verdict)
    def send(message):
        assert not any(command in {'STORE','EXPUNGE'} for command,_ in mailbox.calls)
        assert list(state.values())==['sending']
        sent.append(message)
    counts=run(send)
    assert counts['sent']==counts['deleted']==counts['analysed']==1
    message=sent[0]
    assert message['To']=='submitter@example.org' and message['Subject'].startswith('['+label+']')
    assert message['Auto-Submitted']=='auto-replied'
    html=message.get_body(preferencelist=('html',)).get_content()
    assert '<script>' not in html and '<img' not in html and '&lt;img' in html
    assert list(message.iter_attachments())[0].get_payload(decode=True)==RAW
    assert ('EXPUNGE',(b'42',)) in mailbox.calls
    assert list(state.values())==['deleted']


def test_smtp_failure_keeps_original_and_blocks_duplicate(monkeypatch):
    mailbox,_,state,sent,run=setup(monkeypatch)
    def fail(message): raise RuntimeError('SMTP timeout')
    assert run(fail)['errors']==1
    assert list(state.values())==['sending']
    assert not any(command in {'STORE','EXPUNGE'} for command,_ in mailbox.calls)
    assert run()['errors']==1 and not sent


def test_successful_send_failed_delete_resumes_without_resending(monkeypatch):
    mailbox,_,state,sent,run=setup(monkeypatch)
    mailbox.fail_store=True
    assert run()['errors']==1 and len(sent)==1
    assert list(state.values())==['sent']
    mailbox.fail_store=False
    assert run()['deleted']==1 and len(sent)==1


def test_stop_after_smtp_preserves_original_then_only_deletes(monkeypatch):
    mailbox,_,state,sent,run=setup(monkeypatch)
    active={'cancelled':False}
    def send(message):
        sent.append(message)
        active['cancelled']=True
    with pytest.raises(PreprocessingError,match='Arrêt demandé'):
        run(send,active)
    assert list(state.values())==['sent']
    assert not any(command in {'STORE','EXPUNGE'} for command,_ in mailbox.calls)
    assert run()['deleted']==1 and len(sent)==1


def test_period_and_message_limit(monkeypatch):
    mailbox,account,state,sent,run=setup(monkeypatch,simulation=True)
    account.update(period_days=1,max_messages=1)
    original=mailbox.uid
    def uid(command,*args):
        if command=='SEARCH': return 'OK',[b'1 2 3']
        if command=='FETCH' and args[1]=='(RFC822.SIZE FLAGS INTERNALDATE)':
            # First message is too old; remaining messages qualify.
            date=b'01-Jan-2000' if args[0]==b'1' else b'01-Jan-2099'
            return 'OK',[b'1 (RFC822.SIZE 200 FLAGS () INTERNALDATE "'+date+b' 10:00:00 +0000")']
        return original(command,*args)
    mailbox.uid=uid
    counts=run()
    assert counts['analysed']==1 and counts['skipped']==1
    assert not sent and not state


@pytest.mark.parametrize('debug',[False,True])
def test_debug_diagnostics_and_tokens(env,monkeypatch,debug):
    admin=client('admin@example.com')
    admin.post('/admin/mail-verdict/save',data=config_data())
    config=main.load_config(); config['log_debug']=debug; main.save_config(config)
    async def process(*args):
        progress=args[-1]
        progress('DETAIL étape de connexion')
        progress('[ERROR] Fournisseur indisponible HTTP 429')
        progress('[ERROR IA DEBUG] Diagnostic fournisseur expurgé')
        progress({'usage':{'input':10,'output':2,'total':12}})
        return {'analysed':1,'sent':0,'deleted':0,'skipped':0,'errors':1}
    monkeypatch.setattr(main,'process_mailbox',process)
    main.processes[service.SERVICE_ID]={'cancelled':False,'process':None,'owner':'admin@example.com'}
    asyncio.run(main.execute_mail_verdict(main.load_config()['mail_verdict_service'],{'pseudo':'Admin'}))
    log=main.load_config()['runs'][-1]['log']
    assert ('DETAIL' in log)==debug
    assert ('Diagnostic fournisseur expurgé' in log)==debug
    assert 'HTTP 429' in log and 'Tokens IA' in log and '12' in log


@pytest.mark.parametrize('verdict',['uncertain','legitimate'])
def test_simulation_and_uncertain_never_send_or_delete(monkeypatch,verdict):
    mailbox,_,state,sent,run=setup(monkeypatch,verdict,simulation=verdict=='legitimate')
    run()
    assert not sent and not state and not any(command in {'STORE','EXPUNGE'} for command,_ in mailbox.calls)


def test_uidplus_required_before_any_send(monkeypatch):
    mailbox,_,state,sent,run=setup(monkeypatch)
    mailbox.uidplus=False
    with pytest.raises(PreprocessingError,match='UIDPLUS'): run()
    assert not sent and not state


@pytest.mark.parametrize('header',[b'Auto-Submitted: auto-replied',b'X-IMAPSync-Verdict: SAIN',b'Return-Path: <>',b'List-Id: list',b'Precedence: bulk'])
def test_loop_prevention(header):
    assert service.recipient_for(header+b'\r\n'+RAW,'service@example.org','robot@example.org') is None


def config_data():
    return {'host1':'mail','user1':'service@example.org','pass1':'secret','source_folder':'INBOX',
            'sMoteurIA':'Mistral','simulation':'on','sync_interval':5}


def test_admin_routes_separate_task_and_private_logs(env, monkeypatch):
    admin,alice=client('admin@example.com'),client('alice@example.com')
    assert alice.get('/admin/mail-verdict').status_code==403
    for action in ('save','run','stop','schedule','clear','reset'):
        assert alice.post('/admin/mail-verdict/'+action,data=config_data()).status_code==403
    assert admin.post('/admin/mail-verdict/save',data=config_data()).status_code==303
    config=main.load_config()
    assert len(config['accounts'])==2
    assert config['mail_verdict_service']['schedule_state']=='PAUSED'
    assert 'id="mail-verdict-service"' in admin.get('/dashboard').text
    assert 'id="mail-verdict-service"' not in alice.get('/dashboard').text
    assert 'value="secret"' not in admin.get('/admin/mail-verdict').text
    config['runs'].append({'id':'service-run','account_id':service.SERVICE_ID,'owner':'alice@example.com',
         'kind':'mail_verdict_service','label':'PRIVATE_SERVICE','status':'Succès','started':'2026-10-02T00:00:00+00:00','actor':'Admin','log':'private'})
    main.save_config(config)
    assert alice.get('/logs/service-run').status_code==404
    assert alice.get('/api/logs/service-run').status_code==404
    assert alice.get('/dashboard?account_id='+service.SERVICE_ID).status_code==403
    assert admin.get('/logs/service-run').status_code==200
    assert admin.post('/admin/mail-verdict/clear').status_code==303
    assert any(r['id']=='bobrun' for r in main.load_config()['runs'])


def test_scheduler_obeys_pause_and_no_overlap(env,monkeypatch):
    client('admin@example.com').post('/admin/mail-verdict/save',data=config_data())
    started=[]
    def spawn(coro): started.append(coro.cr_code.co_name); coro.close()
    monkeypatch.setattr(main,'spawn',spawn)
    main.dispatch_due_accounts()
    assert 'execute_mail_verdict' not in started
    client('admin@example.com').post('/admin/mail-verdict/schedule',data={'schedule_state':'RUNNING'})
    main.dispatch_due_accounts();main.dispatch_due_accounts()
    assert started.count('execute_mail_verdict')==1
    assert client('admin@example.com').post('/admin/mail-verdict/save',data=config_data()).status_code==409
    assert client('admin@example.com').post('/admin/mail-verdict/stop').status_code==303
    assert main.processes[service.SERVICE_ID]['cancelled']


def test_runner_uses_principal_keys_and_keeps_service_logs(env,monkeypatch):
    admin=client('admin@example.com')
    admin.post('/admin/mail-verdict/save',data=config_data())
    admin.post('/ai/settings',data={'sApiKeyMistral':'admin-key','mistral_model':'admin-model'})
    async def process(account, token, settings, key, *args):
        assert key=='admin-key' and settings['mistral_model']=='admin-model'
        assert settings['owner']=='admin@example.com'
        return {'analysed':1,'sent':0,'deleted':0,'skipped':0,'errors':0}
    monkeypatch.setattr(main,'process_mailbox',process)
    main.processes[service.SERVICE_ID]={'cancelled':False,'process':None,'owner':'admin@example.com'}
    asyncio.run(main.execute_mail_verdict(main.load_config()['mail_verdict_service'],{'pseudo':'Admin'}))
    run=main.load_config()['runs'][-1]
    assert run['status']=='Succès' and run['kind']=='mail_verdict_service'
    assert 'admin-key' not in run['log']
