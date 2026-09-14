"""Read-only recipient inventory from the newest messages in the Sent folder."""
import asyncio
import imaplib
import re
import ssl
from email import policy
from email.parser import BytesParser

from ai_preprocessing import PreprocessingError, quote_mailbox
from sender_rules import EMAIL


def addresses_from_header(raw):
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
        result = set()
        for header in message.get_all('To', []):
            if header.defects:
                continue
            for address in header.addresses:
                value = address.addr_spec.lower()
                if len(value) <= 254 and EMAIL.fullmatch(value):
                    result.add(value)
        return result
    except (ValueError, TypeError, AttributeError):
        return set()


async def extract_senders(account, whitelist, active, progress):
    client = None
    stage = 'Connexion TLS au serveur source (port 993)'
    def diagnostic(value):
        if isinstance(value, bytes):
            value = value.decode('utf-8', errors='replace')
        text = str(value)
        for key in ('pass1', 'token1', 'refresh_token1'):
            secret = account.get(key)
            if secret:
                text = text.replace(str(secret), '[SECRET MASQUÉ]')
        text = re.sub(r'(?i)Bearer\s+\S+', 'Bearer [SECRET MASQUÉ]', text)
        return ' '.join(text.split())[:1000]

    def failure(detail):
        hint = (' Vérifiez le nom IMAP exact du dossier Envoyés : Sent, INBOX.Sent ou [Gmail]/Sent Mail selon le serveur.'
                if stage.startswith('Ouverture') else '')
        return PreprocessingError(f'Extraction incomplète — {stage} : {diagnostic(detail)}.{hint} Aucun message modifié.')

    def checked(result):
        if result[0] != 'OK':
            details = ' | '.join(diagnostic(row) for row in (result[1] or []) if isinstance(row, (bytes, str)))
            raise failure(f'IMAP {result[0]} | {details or "aucun détail fourni par le serveur"}')
        return result[1]
    async def call(fn, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
    def stopped():
        if active['cancelled']:
            raise PreprocessingError('Extraction arrêtée ; aucune liste importée.')
    addresses, scanned = set(), 0
    try:
        stopped()
        client = await call(imaplib.IMAP4_SSL, account['host1'], port=993,
                            ssl_context=ssl.create_default_context(), timeout=30)
        stage = 'Authentification source'
        if account.get('authmech1') == 'XOAUTH2':
            auth = ('user=' + account['user1'] + '\x01auth=Bearer ' + account['token1'] + '\x01\x01').encode()
            checked(await call(client.authenticate, 'XOAUTH2', lambda _: auth))
        else:
            checked(await call(client.login, account['user1'], account['pass1']))
        folder = account.get('sent_folder', 'Sent')
        limit = account.get('sent_limit', 500)
        stopped()
        progress(f'Dossier : {folder} | Lecture des {limit} messages les plus récents (ordre UID).')
        stage = f'Ouverture du dossier {folder} en lecture seule (EXAMINE)'
        checked(await call(client.select, quote_mailbox(folder), readonly=True))
        stage = f'Recherche des messages du dossier {folder} (UID SEARCH ALL)'
        rows = checked(await call(client.uid, 'SEARCH', None, 'ALL'))
        uids = b' '.join(row for row in rows if isinstance(row, bytes)).split()
        if not all(uid.isdigit() for uid in uids):
            raise PreprocessingError('Liste des UID IMAP invalide.')
        uids = sorted(set(uids), key=int, reverse=True)[:limit]
        for offset in range(0, len(uids), 100):
            stopped()
            batch = uids[offset:offset + 100]
            stage = f'Lecture des en-têtes To du dossier {folder} (UID FETCH, lot {offset // 100 + 1})'
            data = checked(await call(client.uid, 'FETCH', b','.join(batch), '(BODY.PEEK[HEADER.FIELDS (TO)])'))
            for item in data:
                if isinstance(item, tuple) and isinstance(item[1], bytes):
                    addresses.update(addresses_from_header(item[1]) - whitelist)
            scanned += len(batch)
            progress(f'Dossier : {folder} | Messages examinés : {scanned}/{len(uids)} | Destinataires uniques : {len(addresses)}')
        stopped()
        return sorted(addresses), scanned
    except (imaplib.IMAP4.error, OSError) as exc:
        raise failure(f'{type(exc).__name__} | {exc}') from None
    except (ValueError, TypeError, IndexError):
        raise failure('réponse IMAP invalide ou inattendue') from None
    finally:
        if client:
            try:
                await call(client.logout)
            except (imaplib.IMAP4.error, OSError):
                pass
