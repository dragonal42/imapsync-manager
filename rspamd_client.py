"""Bounded local HTTP scanner; no SMTP identities invented from IMAP headers."""
import json
import math
import re
import time
from urllib.parse import urlsplit

import requests


DEFAULTS = {'rspamd_enabled': False, 'rspamd_url': 'http://rspamd:11333',
            'rspamd_clean_score': 0.0, 'rspamd_spam_score': 8.0,
            'rspamd_timeout': 30, 'rspamd_simulation': True}


class RspamdError(Exception):
    pass


def endpoint(value):
    value = str(value).strip().rstrip('/')
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {'', '/checkv2'} or any(ord(c) <= 32 for c in value)):
        raise ValueError('Adresse Rspamd invalide : http(s)://serveur:port, sans identifiants ni paramètres.')
    _ = parsed.port
    return value if parsed.path else value + '/checkv2'


def settings_from_form(form):
    url = str(form.get('rspamd_url', DEFAULTS['rspamd_url'])).strip()
    endpoint(url)
    low = float(str(form.get('rspamd_clean_score', 0)).replace(',', '.'))
    high = float(str(form.get('rspamd_spam_score', 8)).replace(',', '.'))
    timeout = int(form.get('rspamd_timeout', 30))
    if not (math.isfinite(low) and math.isfinite(high) and -100 <= low < high <= 1000 and 1 <= timeout <= 120):
        raise ValueError('Seuils invalides : -100 ≤ seuil bas < seuil haut ≤ 1000 ; délai de 1 à 120 secondes.')
    return {'rspamd_enabled': form.get('rspamd_enabled') == 'on', 'rspamd_url': url,
            'rspamd_clean_score': low, 'rspamd_spam_score': high, 'rspamd_timeout': timeout,
            'rspamd_simulation': form.get('rspamd_simulation') == 'on'}


def scan(raw, settings):
    started = time.monotonic()
    try:
        # No ambient proxy, automatic retry or redirect can forward the email elsewhere.
        with requests.Session() as session:
            session.trust_env = False
            with session.post(endpoint(settings['rspamd_url']), data=raw,
                              headers={'Content-Type': 'message/rfc822', 'Flags': 'no_log',
                                       'Settings': json.dumps({'symbols_disabled': ['SPF_CHECK', 'DMARC_CHECK']})},
                              timeout=(5, settings['rspamd_timeout']), allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise RspamdError(f'Rspamd : HTTP {response.status_code}. Vérifiez le service et son port de scan 11333.')
                data = bytearray()
                for chunk in response.iter_content(8192):
                    data.extend(chunk)
                    if len(data) > 1024 * 1024:
                        raise RspamdError('Rspamd : réponse trop volumineuse.')
                result = json.loads(data)
        if not isinstance(result, dict) or result.get('is_skipped') is not False:
            raise RspamdError('Rspamd : analyse ignorée ou réponse incomplète ; aucun verdict appliqué.')
        score = result.get('score')
        if type(score) not in (int, float) or not math.isfinite(score):
            raise ValueError()
        action = result.get('action')
        allowed = {'no action', 'greylist', 'add header', 'rewrite subject', 'soft reject', 'reject', 'discard', 'quarantine'}
        if action not in allowed or action == 'soft reject':
            raise RspamdError('Rspamd : action absente, inconnue ou refus temporaire ; aucun verdict appliqué.')
        symbols = result.get('symbols')
        if not isinstance(symbols, dict):
            raise ValueError()
        reasons = []
        for name, item in symbols.items():
            if re.fullmatch(r'[A-Za-z0-9_:-]{1,100}', name) and isinstance(item, dict):
                weight = item.get('score')
                if type(weight) in (int, float) and math.isfinite(weight):
                    reasons.append((name, weight))
        reasons.sort(key=lambda item: abs(item[1]), reverse=True)
        verdict = 'rspamd_spam' if score >= settings['rspamd_spam_score'] else ('legitimate' if score < settings['rspamd_clean_score'] else 'uncertain')
        return {'score': score, 'action': action, 'verdict': verdict, 'symbols': reasons[:100],
                'elapsed': time.monotonic() - started}
    except requests.Timeout:
        raise RspamdError('Rspamd : délai réseau dépassé ; vérifiez la charge et le délai configuré.') from None
    except requests.RequestException:
        raise RspamdError('Rspamd : connexion impossible ; vérifiez le nom Docker, le réseau et le port 11333.') from None
    except (ValueError, TypeError, KeyError):
        raise RspamdError('Rspamd : réponse JSON ou configuration invalide ; aucun verdict appliqué.') from None
