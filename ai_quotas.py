"""Local shared quota reservations, persisted before sending a provider request."""
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import secrets
import hashlib
import re
from datetime import timedelta
from email.utils import parsedate_to_datetime

_provider_memory = {}


def provider_feedback(engine, response):
    """Numeric headers and fixed reasons only; 1300 alone is not proof of a daily quota."""
    if response is None:
        return {}
    headers = {str(k).lower(): str(v) for k, v in getattr(response, 'headers', {}).items()}
    numbers = {}
    for name, value in headers.items():
        if re.fullmatch(r'x-ratelimit-(?:limit|remaining)-(?:req|tokens)-(?:minute|day|month)', name) and re.fullmatch(r'\d{1,15}', value):
            numbers[name] = int(value)
    now = datetime.now(timezone.utc)
    stamp = now.timestamp()
    day = now.astimezone(ZoneInfo('America/Los_Angeles' if engine == 'Gemini' else 'UTC'))
    tomorrow = (day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    month = day.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = month.replace(year=month.year + 1, month=1) if month.month == 12 else month.replace(month=month.month + 1)

    def reset(suffix, fallback):
        value = headers.get('x-ratelimit-reset-' + suffix, '')
        try:
            amount = float(value.rstrip('s'))
            if amount > 1e12:
                amount /= 1000
            until = amount if amount > 1e9 else stamp + amount
            if stamp < until <= stamp + 370 * 86400:
                return until
        except (ValueError, TypeError):
            try:
                until = parsedate_to_datetime(value).timestamp()
                if stamp < until <= stamp + 370 * 86400:
                    return until
            except (ValueError, TypeError, OverflowError):
                pass
        return fallback

    reason, until, durable, no_retry = '', 0, False, False
    try:
        retry_until = stamp + max(1, float(headers.get('retry-after', '60')))
        if not stamp < retry_until <= stamp + 370 * 86400:
            retry_until = stamp + 60
    except ValueError:
        try:
            retry_until = max(stamp + 1, parsedate_to_datetime(headers['retry-after']).timestamp())
        except (ValueError, TypeError, KeyError, OverflowError):
            retry_until = stamp + 60
    # Explicit longer-window headers take priority over minute headers.
    for period, fallback, description in [('day', tomorrow, 'quota journalier fournisseur épuisé'),
                                          ('month', next_month.timestamp(), 'quota mensuel fournisseur épuisé')]:
        for metric in ('req', 'tokens'):
            suffix = metric + '-' + period
            if numbers.get('x-ratelimit-remaining-' + suffix) == 0:
                deadline = reset(suffix, fallback)
                if deadline >= until:
                    reason, until = description, deadline
                durable = no_retry = True
    body = {}
    if getattr(response, 'status_code', None) == 429:
        try:
            body = response.json()
        except (ValueError, TypeError):
            pass
        error = body.get('error', body) if isinstance(body, dict) else {}
        if not isinstance(error, dict):
            error = {}
        message = str(error.get('message', '')).lower()
        if re.search(r'(?:prepayment credits|credits|balance).*(?:depleted|exhausted|insufficient)', message):
            reason, until, durable, no_retry = 'crédits fournisseur épuisés : vérifier la facturation', tomorrow, True, True
        elif engine == 'Mistral' and str(error.get('code')) == '1300':
            no_retry = True  # Fail this execution immediately; daily semantics are unconfirmed.
            if numbers.get('x-ratelimit-limit-req-minute') == 0:
                if not durable:
                    reason, until, durable = 'capacité fournisseur nulle pour ce modèle : vérifier les limites du compte', max(until, tomorrow), True
            elif not durable:
                reason, until = 'limite fournisseur Mistral (code 1300), fenêtre de quota non précisée', reset('req-minute', retry_until)
    if not durable:
        for metric, low in [('req', 2), ('tokens', 0)]:
            suffix = metric + '-minute'
            remaining = numbers.get('x-ratelimit-remaining-' + suffix)
            if remaining is not None and remaining <= low:
                until = max(until, reset(suffix, stamp + 60))
                reason = 'limite de minute fournisseur proche ou atteinte'
    return {'until': until, 'durable': durable, 'no_retry': no_retry, 'reason': reason, 'headers': numbers} if reason or numbers else {}


def provider_state_key(settings, engine, model, api_key):
    # Key rotation releases a stale suspension without ever storing the secret.
    return settings.get('owner', '') + ':provider:' + engine + ':' + model + ':' + hashlib.sha256(api_key.encode()).hexdigest()


def provider_state(settings, engine, model, api_key, feedback=None):
    read, save = settings.get('_quota_read'), settings.get('_quota_save')
    key = provider_state_key(settings, engine, model, api_key)
    data = read() if read and save else _provider_memory
    state = data.get(key, {})
    if feedback and feedback.get('until', 0):
        if feedback['until'] >= state.get('until', 0):
            state = dict(feedback)
            data[key] = state
            if read and save:
                save(data)
            elif len(data) > 512:
                data.pop(next(iter(data)))
    return state


def provider_wait(state):
    return max(0, state.get('until', 0) - datetime.now(timezone.utc).timestamp())


def provider_notice(engine, state):
    deadline = datetime.now(timezone.utc) + timedelta(seconds=provider_wait(state))
    return (engine + ' : ' + state.get('reason', 'limite fournisseur') + '. Appels IA suivants ' + ('suspendus. ' if state.get('durable') else 'régulés. ')
            + ('Nouvel essai autorisé à partir de ' if state.get('durable') else 'Reprise au plus tôt à ')
            + deadline.isoformat(timespec='seconds') + ' (date locale de protection, pas une garantie de rétablissement).')


class QuotaExceeded(Exception):
    pass


async def reserve(engine, model, settings, estimate, active, progress, read, save):
    prefix = engine.lower()
    key = (settings['owner'] + ':' if settings.get('owner') else '') + engine + ':' + model
    rpm = settings.get(prefix + '_rpm', 0)
    rpd = settings.get(prefix + '_rpd', 0)
    tpm = settings.get(prefix + '_tpm', 0)
    spacing = max(1 / settings.get(prefix + '_rps', 1), 60 / rpm if rpm else 0)
    if tpm and estimate > tpm:
        raise QuotaExceeded(f'{engine} : estimation de {estimate} tokens supérieure au quota TPM local ({tpm}).')
    announced = False
    while True:
        if active['cancelled']:
            raise QuotaExceeded('Arrêt demandé pendant l’attente du quota IA.')
        now = datetime.now(timezone.utc)
        stamp = now.timestamp()
        day = now.astimezone(ZoneInfo('America/Los_Angeles' if engine == 'Gemini' else 'UTC')).date().isoformat()
        data = read()
        bucket = data.get(key, {})
        entries = [entry for entry in bucket.get('minute', []) if entry['at'] > stamp - 60]
        daily = bucket.get('daily', 0) if bucket.get('day') == day else 0
        if rpd and daily >= rpd:
            raise QuotaExceeded(f'{engine} : quota quotidien local atteint ({daily}/{rpd}). Analyse arrêtée ; prochain jour de quota requis.')
        used = sum(entry['tokens'] for entry in entries)
        if stamp >= bucket.get('next', 0) and (not rpm or len(entries) < rpm) and (not tpm or used + estimate <= tpm):
            identifier = secrets.token_hex(12)
            entries.append({'id': identifier, 'at': stamp, 'tokens': estimate})
            data[key] = {'minute': entries, 'day': day, 'daily': daily + 1, 'next': stamp + spacing}
            save(data)
            progress(f'{engine} : réservation locale | RPM {len(entries)}/{rpm or "non limité"} | RPD {daily + 1}/{rpd or "non limité"} | TPM estimés {used + estimate}/{tpm or "non limité"}.')
            return key, identifier
        if not announced:
            progress(f'{engine} : attente du quota local RPM/TPM (fenêtre glissante de 60 secondes).')
            announced = True
        await asyncio.sleep(0.25)


def defer(reservation, delay, read, save):
    key, _ = reservation
    data = read()
    if key in data:
        data[key]['next'] = max(data[key].get('next', 0), datetime.now(timezone.utc).timestamp() + delay)
        save(data)


def reconcile(reservation, usage, engine, read, save):
    key, identifier = reservation
    actual = usage.get('input' if engine == 'Gemini' else 'total')
    if actual is None:
        return
    data = read()
    for entry in data.get(key, {}).get('minute', []):
        if entry['id'] == identifier:
            # Do not reduce the conservative estimate mid-window.
            entry['tokens'] = max(entry['tokens'], actual)
    save(data)
