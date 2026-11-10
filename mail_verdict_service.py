"""Admin mailbox verdict service. SMTP success precedes UID-specific deletion."""
import asyncio
import hashlib
import html
import imaplib
import io
import json
import re
import ssl
import time
import textwrap
import smtplib
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, make_msgid, parseaddr
from html.parser import HTMLParser
from urllib.parse import urlsplit

from PIL import Image, ImageDraw, ImageFont

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
    smtp = factory(settings['smtp_host'], settings['smtp_port'], **options)
    try:
        if mode == 'starttls':
            smtp.starttls(context=ssl.create_default_context())
        if settings['smtp_user']:
            smtp.login(settings['smtp_user'], settings['smtp_password'])
        if smtp.send_message(message, from_addr=settings['smtp_from']):
            raise smtplib.SMTPRecipientsRefused({})
    finally:
        # A failure closing the connection must not undo DATA acceptance.
        try:
            smtp.close()
        except OSError:
            pass


def smtp_refusal(error):
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        responses = list(error.recipients.values())
        code, reply = responses[0] if len(responses) == 1 else (None, b'')
        stage = 'RCPT TO'
    elif isinstance(error, (smtplib.SMTPDataError, smtplib.SMTPSenderRefused,
                            smtplib.SMTPAuthenticationError, smtplib.SMTPConnectError,
                            smtplib.SMTPHeloError)):
        stage = {smtplib.SMTPDataError: 'DATA', smtplib.SMTPSenderRefused: 'MAIL FROM',
                 smtplib.SMTPAuthenticationError: 'AUTH', smtplib.SMTPConnectError: 'connexion',
                 smtplib.SMTPHeloError: 'EHLO/HELO'}[type(error)]
        code, reply = error.smtp_code, error.smtp_error
    else:
        return None
    if not isinstance(code, int) or not 400 <= code <= 599:
        return None
    text = reply.decode('utf-8', errors='replace') if isinstance(reply, bytes) else str(reply)
    extended = re.search(r'\b[45]\.\d{1,3}\.\d{1,3}\b', text)
    # Map the reply to fixed categories; arbitrary SMTP text can echo private content.
    hints = [('spam|phish|malware|virus|attachment|content', 'filtre de contenu / pièce jointe'),
             ('relay|relaying', 'relais refusé'), ('quota|mailbox full', 'quota de boîte'),
             ('authentication|credentials', 'authentification'), ('policy|blocked', 'politique SMTP')]
    reason = next((label for pattern, label in hints if re.search(pattern, text, re.I)), 'motif non reconnu')
    return f'{stage} | SMTP {code}' + (f' {extended[0]}' if extended else '') + f' | {reason}'


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


class VisibleEmail(HTMLParser):
    """Extract readable text and anchor labels without rendering or fetching resources."""
    OMIT = {'head', 'script', 'style', 'template', 'noscript', 'svg'}
    BLOCK = {'p', 'div', 'br', 'tr', 'td', 'li', 'h1', 'h2', 'h3', 'section'}

    def __init__(self):
        super().__init__()
        self.text = []
        self.omitted = []
        self.links = []
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        if tag in self.OMIT:
            self.omitted.append(tag)
        if self.omitted:
            return
        if tag in self.BLOCK:
            self.text.append(' ')
        if tag == 'a':
            self.anchor = None
            try:
                url = urlsplit(dict(attrs).get('href') or '')
                if url.scheme in {'http', 'https'} and url.hostname:
                    self.anchor = (url.hostname[:253], [])
            except ValueError:
                pass

    def handle_endtag(self, tag):
        if self.omitted:
            if tag == self.omitted[-1]:
                self.omitted.pop()
            return
        if tag == 'a' and self.anchor:
            domain, chunks = self.anchor
            label = ' '.join(''.join(chunks).split())[:100]
            if label and len(self.links) < 10:
                self.links.append({'label': label, 'domain': domain})
            self.anchor = None
        if tag in self.BLOCK:
            self.text.append(' ')

    def handle_data(self, data):
        if not self.omitted:
            self.text.append(data)
            if self.anchor:
                self.anchor[1].append(data)


def forwarded_original(raw):
    """Separate the submitted message from the outer reply address."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    for part in message.walk():
        if part.get_content_type() == 'message/rfc822':
            payload = part.get_payload()
            if isinstance(payload, list) and payload and payload[0].get('From'):
                return payload[0].as_bytes(policy=policy.SMTP)
    part = message.get_body(preferencelist=('plain',))
    if not part:
        return raw
    content = part.get_content()
    marker = re.search(r'(?im)^\s*(?:-{2,}\s*(?:forwarded message|message transf[ée]r[ée]|original message|message d.origine)\s*-{2,}|begin forwarded message:|d[ée]but du message transf[ée]r[ée]\s*:)\s*$', content)
    if not marker:
        return raw
    fields = {'from': 'From', 'de': 'From', 'subject': 'Subject', 'objet': 'Subject',
              'date': 'Date', 'sent': 'Date', 'envoyé': 'Date'}
    headers = {}
    lines = content[marker.end():].lstrip('\r\n ').splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(r'^([^:]{1,30}):\s*(.*)$', line)
        if not match:
            break
        name = fields.get(match[1].strip().lower())
        if name and match[2]:
            headers.setdefault(name, match[2])
        index += 1
    if not {'From', 'Subject'} <= headers.keys() or not parseaddr(headers['From'])[1]:
        return raw
    original = EmailMessage()
    for name, value in headers.items():
        original[name] = value
    original.set_content('\n'.join(lines[index:]).lstrip('\n'))
    return original.as_bytes(policy=policy.SMTP)


def verdict_subject(value):
    return re.sub(r'^(?:\s*(?:fwd|fw|tr)\s*:\s*)+', '', str(value), flags=re.I).strip()


def verdict_evidence(raw):
    evidence = compact_message(raw)
    message = BytesParser(policy=policy.default).parsebytes(raw)
    evidence['subject'] = verdict_subject(message.get('Subject', ''))[:500]
    parser = VisibleEmail()
    part = message.get_body(preferencelist=('html', 'plain'))
    excerpt = ''
    if part:
        payload = part.get_payload(decode=True) or b''
        try:
            content = payload[:131072].decode(part.get_content_charset() or 'utf-8', errors='replace')
        except LookupError:
            content = payload[:131072].decode('utf-8', errors='replace')
        if part.get_content_type() == 'text/html':
            parser.feed(content)
            parser.close()
            excerpt = ''.join(parser.text)
        else:
            excerpt = content
    # Keep homoglyphs intact. Remove raw links from the excerpt; destinations
    # are represented separately by hostnames, without paths or query tokens.
    excerpt = re.sub(r'https?://[^\s<>"]+', '[lien]', excerpt, flags=re.I)
    domains = set()
    for value in evidence.pop('urls'):
        try:
            host = urlsplit(value).hostname
            if host:
                domains.add(host[:253])
        except ValueError:
            continue
    evidence.update(visible_excerpt=' '.join(excerpt.split())[:1000],
                    link_domains=sorted(domains)[:15], labelled_links=parser.links)
    return evidence


def email_preview_png(raw, dangerous=False):
    """Rasterize a bounded text reconstruction. Never render the original HTML."""
    evidence = verdict_evidence(raw)
    heading = re.sub(r'https?://[^\s<>]+', '[lien masqué]', evidence['subject'], flags=re.I)
    excerpt = evidence['visible_excerpt'] or '(Aucun texte à afficher.)'
    lines = textwrap.wrap(heading, 54)[:3] + [''] + textwrap.wrap(excerpt, 54)[:24]
    width, top, spacing = 920, 118, 32
    image = Image.new('RGB', (width, top + len(lines) * spacing + 40), '#ffffff')
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype('DejaVuSans.ttf', 22)
        title = ImageFont.truetype('DejaVuSans-Bold.ttf', 28)
    except OSError:
        font, title = ImageFont.load_default(size=22), ImageFont.load_default(size=28)
    if dangerous:
        draw.rectangle((0, 0, width, 65), fill='#ffdc36')
        for x in range(0, width, 60):
            draw.polygon([(x, 0), (x + 22, 0), (x + 44, 12), (x + 22, 12)], fill='#181818')
        draw.text((24, 20), 'SCÈNE DE CRIME — MESSAGE DANGEREUX', fill='#181818', font=title)
    else:
        draw.rectangle((0, 0, width, 65), fill='#e8eff7')
        draw.text((24, 17), 'Aperçu du message', fill='#182e43', font=title)
    draw.text((24, 77), 'Aperçu texte • Liens et images externes désactivés', fill='#52667a', font=font)
    for index, line in enumerate(lines):
        draw.text((24, top + index * spacing), line, fill='#182e43', font=font)
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


def report_message(raw, recipient, smtp_from, verdict, score=None, received_at=None):
    label, colour, description = (
        ('INCONCLUSIF', '#52667a', 'Les éléments analysés ne permettent pas de trancher.')
        if verdict == 'uncertain' else LABELS[verdict])
    original = BytesParser(policy=policy.default).parsebytes(raw)
    subject = ' '.join(verdict_subject(original.get('Subject', '')).split())[:180] or '(sans objet)'
    summary_only = verdict in {'scam', 'spam', 'uncertain'}
    def reference(value):
        text = ' '.join(str(value).split())
        if summary_only:
            text = re.sub(r'https?://[^\s<>]+', '[lien masqué]', text, flags=re.I)
        return text[:250]
    references = {
        'Reçu dans la boîte de vérification (UTC)': received_at.strftime('%d/%m/%Y %H:%M:%S') if received_at else 'Date indisponible',
        'Objet du message vérifié': reference(verdict_subject(original.get('Subject', '')) or '(sans objet)'),
        'Expéditeur du message vérifié (From)': reference(original.get('From') or '(non renseigné)'),
    }
    plain_references = '\n'.join(f'{key} : {value}' for key, value in references.items())
    html_references = ''.join(f'<dt><strong>{html.escape(key)}</strong></dt><dd>{html.escape(value)}</dd>'
                              for key, value in references.items())
    body = None if summary_only else original.get_body(preferencelist=('plain',))
    excerpt = 'Contenu original omis par sécurité.' if summary_only else body.get_content()[:20000] if body else '(Pas de version texte : consultez le message original joint.)'
    details = description + (' Score Rspamd : ' + str(score) + '.' if score is not None else ' Score numérique non communiqué par le moteur IA.')
    caution = 'Analyse automatisée par une IA, sans vérification humaine. Un résultat SAIN ne garantit pas l’absence de danger : restez prudent avant d’ouvrir un lien ou une pièce jointe.'
    message = EmailMessage()
    message['From'], message['To'] = smtp_from, recipient
    if summary_only:
        subject = 'Compte rendu de diagnostic'
    message['Subject'] = '[' + label + '] ' + subject
    message['Date'] = format_datetime(datetime.now(timezone.utc))
    message['Message-ID'] = make_msgid()
    message['Auto-Submitted'] = 'auto-replied'
    message['X-Auto-Response-Suppress'] = 'All'
    message['X-IMAPSync-Verdict'] = label
    message.set_content(label + '\n' + details + '\n' + caution + '\n\n' + plain_references + '\n\n' + excerpt)
    message.add_alternative('<h1 style="color:' + colour + '">' + label + '</h1><p>' + html.escape(details)
        + '</p><p>' + html.escape(caution) + '</p><hr><dl>' + html_references
        + '</dl><pre style="white-space:pre-wrap">' + html.escape(excerpt) + '</pre>', subtype='html')
    if not summary_only:
        message.add_attachment(raw, maintype='application', subtype='octet-stream', filename='message-original.eml')
    message.add_attachment(email_preview_png(raw, dangerous=verdict == 'scam'),
                           maintype='image', subtype='png', filename='apercu-email.png')
    return message


def mailbox_scope(account, client):
    validity = client.response('UIDVALIDITY')[1][0]
    if not validity or not validity.isdigit():
        raise PreprocessingError('UIDVALIDITY absent ; opération interrompue.')
    return hashlib.sha256(json.dumps([account['host1'], account['user1'],
                                      account['source_folder'], validity.decode()]).encode()).hexdigest()


def inspect_verdict_mailbox(account, token, state, deletion=None):
    """Read headers only, or delete one UID in the exact mailbox previously listed."""
    client = imaplib.IMAP4_SSL(account['host1'], port=993, ssl_context=ssl.create_default_context(), timeout=30)
    try:
        if account.get('authmech1') == 'XOAUTH2':
            auth = ('user=' + account['user1'] + '\x01auth=Bearer ' + token + '\x01\x01').encode()
            checked(client.authenticate('XOAUTH2', lambda _: auth))
        else:
            checked(client.login(account['user1'], account['pass1']))
        if deletion and b'UIDPLUS' not in b' '.join(checked(client.capability())).upper().split():
            raise PreprocessingError('UIDPLUS requis pour la suppression individuelle.')
        checked(client.select(quote_mailbox(account['source_folder']), readonly=not deletion))
        scope = mailbox_scope(account, client)
        if deletion:
            expected_scope, uid = deletion
            if scope != expected_scope:
                raise PreprocessingError('La boîte ou ses UID ont changé. Rechargez la liste.')
            uid = uid.encode('ascii')
            if uid not in b' '.join(checked(client.uid('SEARCH', None, 'UID', uid))).split():
                raise PreprocessingError('Ce message n’est plus dans la boîte. Rechargez la liste.')
            checked(client.uid('STORE', uid, '+FLAGS.SILENT', '(\\Deleted)'))
            checked(client.uid('EXPUNGE', uid))
            return None
        uids = b' '.join(checked(client.uid('SEARCH', None, 'UNDELETED'))).split()
        messages = []
        for uid in sorted({uid for uid in uids if uid.isdigit()}, key=int, reverse=True)[:50]:
            rows = checked(client.uid('FETCH', uid, '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])'))
            header = next((row[1] for row in rows if isinstance(row, tuple) and isinstance(row[1], bytes)), None)
            if header is None:
                continue
            msg = BytesParser(policy=policy.default).parsebytes(header[:65536])
            uid = uid.decode()
            identifier = scope + ':' + uid
            messages.append(dict(uid=uid, sender=str(msg.get('From', ''))[:254],
                                 subject=verdict_subject(msg.get('Subject', ''))[:250], date=str(msg.get('Date', ''))[:100],
                                 status=state.get(identifier, ''), result=state.get(identifier + ':result', {})))
        return {'scope': scope, 'messages': messages, 'total': len(uids)}
    finally:
        try:
            client.logout()
        except Exception:
            pass


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
        scope = mailbox_scope(account, client)
        selected = account.get('_force_message')
        if selected and selected[0] != scope:
            raise PreprocessingError('La boîte ou ses UID ont changé. Rechargez la liste.')
        if selected and dry:
            raise PreprocessingError('Désactivez la simulation avant l’envoi du rapport.')
        state = read_state()
        criteria = ['ALL']  # Includes read mail, and sent-but-not-yet-expunged messages.
        days = 0 if selected else account.get('period_days', 5)
        if selected:
            criteria = ['UID', selected[1]]
        if days:
            since = datetime.now(timezone.utc) - timedelta(days=days + 1)
            months = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()
            criteria += ['SINCE', f'{since.day:02d}-{months[since.month-1]}-{since.year}']
        uids = b' '.join(checked(await call(client.uid, 'SEARCH', None, *criteria))).split()
        if not all(uid.isdigit() for uid in uids):
            raise PreprocessingError('Réponse UID invalide.')
        progress('Simulation : ' + ('oui' if dry else 'non') + f' | Dossier : {account["source_folder"]} | Messages trouvés : {len(uids)}')
        eligible = [uid for uid in sorted(set(uids), key=int) if state.get(scope + ':' + uid.decode()) not in {'deleted', 'ignored'}]
        if selected:
            eligible = [uid for uid in eligible if uid.decode() == selected[1]]
            if not eligible:
                raise PreprocessingError('Message absent ou déjà exclu du traitement.')
        retry_uids = set(account.get('_retry_uids', ()))
        if retry_uids:
            eligible = [uid for uid in eligible if uid.decode() in retry_uids
                        and state.get(scope + ':' + uid.decode()) == 'sending']
            progress(f'Reprise SMTP ciblée | UID bloqués retrouvés : {len(eligible)} / {len(retry_uids)}.')
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
            if stage == 'sending' and not retry_uids:
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
                    parsed = imaplib.Internaldate2tuple(info)
                    received_at = datetime.fromtimestamp(time.mktime(parsed), timezone.utc) if parsed else None
                    if days:
                        if received_at is None:
                            raise PreprocessingError('Date IMAP invalide ; message conservé.')
                        # Internaldate2tuple returns local time; mktime restores the timestamp.
                        if received_at < datetime.now(timezone.utc) - timedelta(days=days):
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
                    original = forwarded_original(raw)
                    if original != raw:
                        progress(f'UID {uid.decode()} : message transféré détecté ; analyse de l’original, réponse à la personne qui l’a soumis.')
                    score = None
                    if account.get('use_rspamd'):
                        result = await call(scan, original, settings)
                        score = result['score']
                        progress(f'UID {uid.decode()} : score Rspamd {score}, action {result["action"]}.')
                    engine = account['sMoteurIA']
                    if not key:
                        raise PreprocessingError('Clé IA de l’administrateur absente.')
                    progress(f'UID {uid.decode()} : appel {engine} | modèle : {settings.get(engine.lower() + "_model", "défaut du fournisseur")}.')
                    evidence = verdict_evidence(original)
                    progress(f'UID {uid.decode()} : contexte IA | extrait : {len(evidence["visible_excerpt"])} caractères'
                             f' | domaines : {len(evidence["link_domains"])} | liens libellés : {len(evidence["labelled_links"])}.')
                    verdict = await provider_call(settings, key, evidence, active, call, progress, engine)
                    progress(f'UID {uid.decode()} : réponse {engine} | verdict : {verdict}'
                             f' | {getattr(verdict, "diagnostic", "réponse validée")}.')
                    progress({'usage': getattr(verdict, 'usage', {})})
                    counts['analysed'] += 1
                    if not dry:
                        state[identifier + ':result'] = {'verdict': LABELS.get(verdict, ('INCONCLUSIF',))[0]}
                        save_state(state)
                    if verdict not in LABELS and not (selected and verdict == 'uncertain'):
                        raise PreprocessingError('Verdict incertain ; aucun envoi ni suppression.')
                    progress(f'UID {uid.decode()} : {LABELS.get(verdict, ("INCONCLUSIF",))[0]}.')
                    if dry:
                        continue
                    stop()
                    report = report_message(original, recipient, smtp_from, verdict, score, received_at)
                    progress(f'UID {uid.decode()} : préparation SMTP, Message-ID {report["Message-ID"]}.')
                    state[identifier] = 'sending'; save_state(state)
                    try:
                        await call(send, report)
                    except smtplib.SMTPException as error:
                        refusal = smtp_refusal(error)
                        if refusal is None:
                            raise
                        state[identifier] = 'refused'
                        state[identifier + ':result']['smtp'] = refusal
                        save_state(state)
                        raise PreprocessingError('Refus SMTP confirmé : ' + refusal +
                                                 '. Original conservé ; relance autorisée.') from None
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
                if (not dry and state.get(identifier) not in {'sending', 'refused', 'sent', 'deleted'}
                        and state.get(identifier + ':result', {}).get('verdict') != 'INCONCLUSIF'):
                    state.setdefault(identifier + ':result', {})['failure'] = 'Analyse ou lecture incomplète ; voir le journal.'
                    save_state(state)
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
