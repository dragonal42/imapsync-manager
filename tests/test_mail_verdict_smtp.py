import asyncio
import copy
import smtplib
from unittest.mock import AsyncMock

import pytest
import main
import mail_verdict_service as service
from test_tenants import env, client
from test_mail_verdict_service import config_data, Mailbox


@pytest.mark.parametrize('mode',['ssl','starttls'])
def test_dedicated_transport_uses_saved_config_not_environment(monkeypatch,mode):
    calls=[]
    class SMTP:
        def __init__(self,host,port,**kwargs):
            calls.append(('connect',host,port,kwargs))
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def starttls(self,**kwargs): calls.append(('tls',kwargs))
        def login(self,user,password): calls.append(('login',user,password))
        def send_message(self,message,**kwargs): calls.append(('send',message,kwargs)); return {}
    monkeypatch.setattr(service.smtplib,'SMTP_SSL' if mode=='ssl' else 'SMTP',SMTP)
    monkeypatch.setenv('SMTP_HOST','unrelated-backend')
    settings=service.smtp_settings({**config_data(),'smtp_security':mode}, {})
    message=service.report_message(b'From: sender@example.org\r\n\r\nbody','sender@example.org',settings['smtp_from'],'spam')
    service.send_verdict_smtp(message,settings)
    assert calls[0][1:3]==('smtp.example.org',587)
    assert ('login','service@example.org','smtp-secret') in calls
    assert any(c[0]=='tls' for c in calls)==(mode=='starttls')
    assert ('context' in calls[0][3])==(mode=='ssl')
    assert calls[-1][2]['from_addr']=='service@example.org'
    assert calls[-1][1]['From']=='service@example.org'


def test_secret_retention_validation_and_admin_only(env):
    admin=client('admin@example.com')
    assert admin.post('/admin/mail-verdict/save',data=config_data()).status_code==303
    assert admin.post('/admin/mail-verdict/save',data={**config_data(),'smtp_password':''}).status_code==303
    assert main.load_config()['mail_verdict_service']['smtp_password']=='smtp-secret'
    html=admin.get('/admin/mail-verdict').text
    assert 'smtp-secret' not in html
    assert 'name="smtp_password"' in html
    for kind in ('imap','smtp'):
        assert client('alice@example.com').post('/admin/mail-verdict/test/'+kind,data=config_data()).status_code==403
    for changes in ({'smtp_port':0},{'smtp_port':65536},{'smtp_security':'none'},{'smtp_from':'bad\nBcc: attacker@example.org'}):
        assert admin.post('/admin/mail-verdict/test/smtp',data={**config_data(),**changes}).status_code==400


def test_smtp_test_current_form_and_saved_password(env,monkeypatch):
    admin=client('admin@example.com')
    admin.post('/admin/mail-verdict/save',data=config_data())
    sent=[]
    monkeypatch.setattr(main,'send_verdict_smtp',lambda message,settings:sent.append((message,settings)))
    response=admin.post('/admin/mail-verdict/test/smtp',data={**config_data(),'smtp_password':'','smtp_host':'new.smtp.example.org','smtp_test_to':'test@example.org'})
    assert response.status_code==200
    message,settings=sent[0]
    assert settings['smtp_host']=='new.smtp.example.org' and settings['smtp_password']=='smtp-secret'
    assert message['From']=='service@example.org' and message['To']=='test@example.org'
    assert main.load_config()['mail_verdict_service']['smtp_host']=='smtp.example.org'
    assert message['X-IMAPSync-Verdict']=='TEST'


def test_test_errors_safe_and_explicit(env,monkeypatch):
    def fail(*args): raise smtplib.SMTPAuthenticationError(535,b'password smtp-secret refused')
    monkeypatch.setattr(main,'send_verdict_smtp',fail)
    response=client('admin@example.com').post('/admin/mail-verdict/test/smtp',data=config_data())
    assert response.status_code==502 and '535' in response.text
    assert 'smtp-secret' not in response.text


@pytest.mark.parametrize('oauth',[False,True])
def test_imap_connection_read_only(env,monkeypatch,oauth):
    mailbox=Mailbox()
    monkeypatch.setattr(service.imaplib,'IMAP4_SSL',lambda *args,**kw:mailbox)
    monkeypatch.setattr(main,'refresh_access_token',AsyncMock(return_value='private-access-token'))
    data=config_data()
    if oauth: data.update(authmech1='XOAUTH2',refresh_oauth2_token1='private-refresh',provider_oauth2_token1='google')
    response=client('admin@example.com').post('/admin/mail-verdict/test/imap',data=data)
    assert response.status_code==200
    assert mailbox.readonly and not mailbox.calls
    assert 'private-access-token' not in response.text
    assert not main.load_config().get('mail_verdict_service')


def test_runner_sends_using_dedicated_smtp(env,monkeypatch):
    admin=client('admin@example.com')
    admin.post('/admin/mail-verdict/save',data=config_data())
    sent=[]
    monkeypatch.setattr(main,'send_verdict_smtp',lambda message,settings:sent.append(settings))
    def backend(*args): raise AssertionError('Backend SMTP must not send verdicts')
    monkeypatch.setattr(main,'send_smtp_message',backend)
    async def process(account,token,settings,key,smtp_from,active,read,save,send,progress):
        assert smtp_from=='service@example.org'
        send(service.report_message(b'From: person@example.org\r\n\r\nbody','person@example.org',smtp_from,'legitimate'))
        return {'analysed':1,'sent':1,'deleted':1,'skipped':0,'errors':0}
    monkeypatch.setattr(main,'process_mailbox',process)
    main.processes[service.SERVICE_ID]={'cancelled':False,'process':None}
    asyncio.run(main.execute_mail_verdict(copy.deepcopy(main.load_config()['mail_verdict_service']),{'pseudo':'Admin'}))
    assert sent[0]['smtp_password']=='smtp-secret' and sent[0]['smtp_host']=='smtp.example.org'
