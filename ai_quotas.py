"""Local shared quota reservations, persisted before sending a provider request."""
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import secrets


class QuotaExceeded(Exception):
    pass


async def reserve(engine, model, settings, estimate, active, progress, read, save):
    prefix = engine.lower()
    key = engine + ':' + model
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
