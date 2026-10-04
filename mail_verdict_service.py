"""Admin mailbox verdict service. SMTP success precedes UID-specific deletion."""
import asyncio
import hashlib
import html
import imaplib
import json
import re
import ssl
import time
import smtplib
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, make_msgid, parseaddr

from ai_preprocessing import PreprocessingError, checked, quote_mailbox, compact_message, provider_call, MAX_MESSAGE
from rspamd_client import scan, RspamdError
from sender_rules import sender_address, EMAIL

SERVICE_ID = 'admin-mail-verdict'
LABELS = {'scam': ('DANGEREUX', '#b91c1c', 'Suspicion de fraude ou de hameçonnage.'),
          'spam': ('SPAM', '#b45309', 'Message indésirable ou publicitaire.'),
          'legitimate': ('SAIN', '#15803d', 'Aucun signal suspect identifié par cette analyse.')}


def smtp_settings(form, saved):
    """Dedicated service SMTP. Never fall back to the backend SMTP environment."""
    result = {}
    for name in ('smtp_host', 'smtp_user', 'smtp_from'):
        value = str(form.get(name, '')).strip()
        if len(value) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Champ SMTP invalide : ' + name)
        result[name] = value
    if not result['smtp_host'] or not EMAIL.fullmatch(result['smtp_from']):
        raise ValueError('Renseignez le serveur SMTP et l’adresse email de la boîte source.')
    try:
        result['smtp_port'] = int(form.get('smtp_port', 587))
        if not 1 <= result['smtp_port'] <= 65535:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Port SMTP invalide (1 à 65535).') from None
    result['smtp_security'] = str(form.get('smtp_security', 'starttls'))
    if result['smtp_security'] not in {'ssl', 'starttls'}:
        raise ValueError('Choisissez SSL/TLS ou STARTTLS pour le SMTP.')
    result['smtp_password'] = str(form.get('smtp_password', '')) or saved.get('smtp_password', '')
    if result['smtp_user'] and not result['smtp_password']:
        raise ValueError('Mot de passe SMTP requis pour cet identifiant.')
    return result


def send_verdict_smtp(message, settings):
    mode = settings['smtp_security']
    if mode not in {'ssl', 'starttls'}:
        raise ValueError('Configuration SMTP du service invalide.')
    factory = smtplib.SMTP_SSL if mode == 'ssl' else smtplib.SMTP
    options = {'timeout': 20}
    if mode == 'ssl':
        options['context'] = ssl.create_default_context()
    with factory(settings['smtp_host'], settings['smtp_port'], **options) as smtp:
        if mode == 'starttls':
            smtp.starttls(context=ssl.create_default_context())
        if settings['smtp_user']:
            smtp.login(settings['smtp_user'], settings['smtp_password'])
        if smtp.send_message(message, from_addr=settings['smtp_from']):
            raise smtplib.SMTPRecipientsRefused({})


def test_imap_connection(account, token):
    """Read-only connection/folder check: no fetching, sending, flags or deletion."""
    client = imaplib.IMAP4_SSL(account['host1'], port=993, ssl_context=ssl.create_default_context(), timeout=20)
    try:
        if account['authmech1'] == 'XOAUTH2':
            auth = ('user=' + account['user1'] + '\x01auth=Bearer ' + token + '\x01\x01').encode()
            checked(client.authenticate('XOAUTH2', lambda _: auth))
        else:
            checked(client.login(account['user1'], account['pass1']))
        checked(client.select(quote_mailbox(account['source_folder']), readonly=True))
    finally:
        try:
            client.logout()
        except Exception:
            pass


def transport_error(error):
    """Useful diagnostics without echoing server text containing credentials."""
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return f'Authentification SMTP refusée (code {error.smtp_code}). Vérifiez login et mot de passe/app password.'
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return 'Destinataire refusé par le serveur SMTP. Vérifiez son adresse et les droits de relais.'
    if isinstance(error, smtplib.SMTPSenderRefused):
        return f'Adresse d’envoi refusée par le serveur SMTP (code {error.smtp_code}). Vérifiez l’adresse et les droits d’envoi.'
    if isinstance(error, smtplib.SMTPResponseException):
        return f'Commande SMTP refusée (code {error.smtp_code}). Vérifiez la configuration du serveur.'
    if isinstance(error, smtplib.SMTPNotSupportedError):
        return 'Le serveur SMTP ne prend pas en charge le mode TLS ou l’authentification sélectionné.'
    if isinstance(error, ssl.SSLError):
        return 'Échec TLS/certificat. Vérifiez le nom du serveur, le port et le mode SSL/TLS ou STARTTLS.'
    if isinstance(error, TimeoutError):
        return 'Délai de connexion dépassé. Vérifiez serveur, port et accès réseau.'
    if isinstance(error, OSError):
        return 'Connexion réseau impossible. Vérifiez le nom du serveur, son port et le réseau Docker.'
    if isinstance(error, (imaplib.IMAP4.error, PreprocessingError)):
        return 'Connexion/authentification IMAP ou ouverture du dossier refusée. Vérifiez serveur, identifiant, mot de passe/OAuth et dossier.'
    return 'Échec de connexion ou d’envoi. Vérifiez la configuration et les accès au service.'


def recipient_for(raw, mailbox, smtp_from):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender = sender_address(raw)
    own = {parseaddr(mailbox)[1].lower(), parseaddr(smtp_from)[1].lower()}
    if not sender or sender.lower() in own:
        return None
    if (message.get('Auto-Submitted', 'no').lower() != 'no' or message.get('X-IMAPSync-Verdict')
            or message.get('List-Id') or message.get('Precedence', '').lower() in {'bulk', 'list', 'junk'}
            or message.get('Return-Path', '').strip() == '<>'):
        return None
    return sender


def report_message(raw, recipient, smtp_from, verdict, score=None):
    label, colour, description = LABELS[verdict]
    original = BytesParser(policy=policy.default).parsebytes(raw)
    subject = ' '.join(str(original.get('Subject', '(sans objet)')).split())[:180]
    body = original.get_body(preferencelist=('plain',))
    excerpt = body.get_content()[:20000] if body else '(Pas de version texte : consultez le message original joint.)'
    details = description + (' Score Rspamd : ' + str(score) + '.' if score is not None else ' Score numérique non communiqué par le moteur IA.')
    caution = 'Classification automatique indicative. Un résultat SAIN ne garantit pas l’absence de danger. Les pièces jointes ne sont pas exécutées.'
    message = EmailMessage()
    message['From'], message['To'] = smtp_from, recipient
    message['Subject'] = '[' + label + '] ' + subject
    message['Date'] = format_datetime(datetime.now(timezone.utc))
    message['Message-ID'] = make_msgid()
    message['Auto-Submitted'] = 'auto-replied'
    message['X-Auto-Response-Suppress'] = 'All'
    message['X-IMAPSync-Verdict'] = label
    message.set_content(label + '\n' + details + '\n' + caution + '\n\nMessage reçu : ' + subject + '\n\n' + excerpt)
    message.add_alternative('<h1 style="color:' + colour + '">' + label + '</h1><p>' + html.escape(details)
        + '</p><p>' + html.escape(caution) + '</p><hr><h2>Message reçu : ' + html.escape(subject)
        + '</h2><pre style="white-space:pre-wrap">' + html.escape(excerpt) + '</pre>', subtype='html')
    message.add_attachment(raw, maintype='application', subtype='octet-stream', filename='message-original.eml')
    return message


async def process_mailbox(account, token, settings, key, smtp_from, active, read_state, save_state, send, progress):
    client = None
    counts = {'analysed': 0, 'sent': 0, 'deleted': 0, 'skipped': 0, 'errors': 0}
    dry = account.get('simulation', True) or (account.get('use_rspamd') and settings.get('rspamd_simulation', True))
    async def call(fn, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
    def stop():
        if active['cancelled']:
            raise PreprocessingError('Arrêt demandé ; les messages non envoyés sont conservés.')
    try:
        stop()
        if not dry and not parseaddr(smtp_from)[1]:
            raise PreprocessingError('SMTP_FROM absent ou invalide ; aucun envoi effectué.')
        client = await call(imaplib.IMAP4_SSL, account['host1'], port=993, ssl_context=ssl.create_default_context(), timeout=30)
        if account.get('authmech1') == 'XOAUTH2':
            auth = ('user=' + account['user1'] + '\x01auth=Bearer ' + token + '\x01\x01').encode()
            checked(await call(client.authenticate, 'XOAUTH2', lambda _: auth))
        else:
            checked(await call(client.login, account['user1'], account['pass1']))
        capabilities = b' '.join(checked(await call(client.capability))).upper().split()
        if not dry and b'UIDPLUS' not in capabilities:
            raise PreprocessingError('UIDPLUS requis pour supprimer uniquement le message envoyé. Aucun envoi effectué.')
        checked(await call(client.select, quote_mailbox(account['source_folder']), readonly=dry))
        validity = client.response('UIDVALIDITY')[1][0]
        if not validity or not validity.isdigit():
            raise PreprocessingError('UIDVALIDITY absent ; aucun envoi effectué.')
        scope = hashlib.sha256(json.dumps([account['host1'], account['user1'], account['source_folder'], validity.decode()]).encode()).hexdigest()
        state = read_state()
        criteria = ['ALL']  # Includes read mail, and sent-but-not-yet-expunged messages.
        days = account.get('period_days', 5)
        if days:
            since = datetime.now(timezone.utc) - timedelta(days=days + 1)
            months = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()
            criteria += ['SINCE', f'{since.day:02d}-{months[since.month-1]}-{since.year}']
        uids = b' '.join(checked(await call(client.uid, 'SEARCH', None, *criteria))).split()
        if not all(uid.isdigit() for uid in uids):
            raise PreprocessingError('Réponse UID invalide.')
        progress('Simulation : ' + ('oui' if dry else 'non') + f' | Dossier : {account["source_folder"]} | Messages trouvés : {len(uids)}')
        eligible = [uid for uid in sorted(set(uids), key=int) if state.get(scope + ':' + uid.decode()) not in {'deleted', 'ignored'}]
        handled = 0
        for uid in eligible:
            if handled >= account.get('max_messages', 50):
                break
            stop()
            identifier = scope + ':' + uid.decode()
            stage = state.get(identifier)
            if stage in {'deleted', 'ignored'}:
                counts['skipped'] += 1
                continue
            if stage == 'sending':
                counts['errors'] += 1
                progress(f'[ERROR] UID {uid.decode()} : envoi précédent ambigu. Vérifier le SMTP avant toute reprise ; aucun renvoi automatique.')
                continue
            try:
                if stage != 'sent':
                    metadata = checked(await call(client.uid, 'FETCH', uid, '(RFC822.SIZE FLAGS INTERNALDATE)'))
                    info = b' '.join(item for item in metadata if isinstance(item, bytes))
                    size = re.search(rb'RFC822.SIZE (\d+)', info)
                    if not size or int(size[1]) > MAX_MESSAGE:
                        raise PreprocessingError('Message absent ou supérieur à 2 Mio ; conservé sans envoi.')
                    if b'\\Deleted' in info:
                        counts['skipped'] += 1
                        continue
                    if days:
                        stamp = re.search(rb'INTERNALDATE "([^"]+)"', info)
                        parsed = imaplib.Internaldate2tuple(info)
                        if not stamp or parsed is None:
                            raise PreprocessingError('Date IMAP invalide ; message conservé.')
                        # Internaldate2tuple returns local time; mktime restores the timestamp.
                        if time.mktime(parsed) < (datetime.now(timezone.utc) - timedelta(days=days)).timestamp():
                            counts['skipped'] += 1
                            continue
                    handled += 1
                    rows = checked(await call(client.uid, 'FETCH', uid, '(BODY.PEEK[])'))
                    bodies = [item[1] for item in rows if isinstance(item, tuple) and isinstance(item[1], bytes)]
                    if len(bodies) != 1 or len(bodies[0]) > MAX_MESSAGE:
                        raise PreprocessingError('Message incomplet ou trop volumineux.')
                    raw = bodies[0]
                    recipient = recipient_for(raw, account['user1'], smtp_from)
                    if not recipient:
                        counts['skipped'] += 1
                        progress(f'UID {uid.decode()} : expéditeur absent/ambigu ou message automatique ; conservé sans réponse.')
                        if not dry:
                            state[identifier] = 'ignored'; save_state(state)
                        continue
                    score = None
                    if account.get('use_rspamd'):
                        result = await call(scan, raw, settings)
                        score = result['score']
                        progress(f'UID {uid.decode()} : score Rspamd {score}, action {result["action"]}.')
                    engine = account['sMoteurIA']
                    if not key:
                        raise PreprocessingError('Clé IA de l’administrateur absente.')
                    progress(f'UID {uid.decode()} : appel {engine} | modèle : {settings.get(engine.lower() + "_model", "défaut du fournisseur")}.')
                    verdict = await provider_call(settings, key, compact_message(raw), active, call, progress, engine)
                    progress({'usage': getattr(verdict, 'usage', {})})
                    counts['analysed'] += 1
                    if verdict not in LABELS:
                        raise PreprocessingError('Verdict incertain ; aucun envoi ni suppression.')
                    progress(f'UID {uid.decode()} : {LABELS[verdict][0]}.')
                    if dry:
                        continue
                    stop()
                    report = report_message(raw, recipient, smtp_from, verdict, score)
                    progress(f'UID {uid.decode()} : préparation SMTP, Message-ID {report["Message-ID"]}.')
                    state[identifier] = 'sending'; save_state(state)
                    await call(send, report)
                    state[identifier] = 'sent'; save_state(state)
                    counts['sent'] += 1
                if stage == 'sent':
                    handled += 1
                if dry:
                    continue
                stop()
                checked(await call(client.uid, 'STORE', uid, '+FLAGS.SILENT', '(\\Deleted)'))
                checked(await call(client.uid, 'EXPUNGE', uid))
                state[identifier] = 'deleted'; save_state(state)
                counts['deleted'] += 1
                progress(f'UID {uid.decode()} : réponse acceptée par SMTP ; original supprimé.')
            except Exception as error:
                counts['errors'] += 1
                detail = str(error) if isinstance(error, (PreprocessingError, RspamdError)) else transport_error(error)
                progress(f'[ERROR] UID {uid.decode()} : {detail}')
                for line in getattr(error, 'debug', '').splitlines():
                    progress('[ERROR IA DEBUG] ' + line)
                if hasattr(error, 'usage'):
                    progress({'usage': error.usage})
                if active['cancelled']:
                    raise
                if getattr(error, 'quota_exhausted', False) or getattr(error, 'no_retry', False):
                    progress('[ERROR] IA indisponible : arrêt du diagnostic pour ce passage ; aucun résultat SAIN déduit du seul score Rspamd. Messages restants conservés.')
                    break
        return counts
    finally:
        if client:
            try:
                await call(client.logout)
            except Exception:
                pass
