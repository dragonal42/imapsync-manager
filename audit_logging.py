"""Single-line application audit events on stdout, visible through docker logs."""
from contextvars import ContextVar
import ipaddress
import json
import logging
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


logger = logging.getLogger('imapsync.audit')
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(handler)

SENSITIVE = re.compile(r'pass|token|secret|refresh|api.?key|credential|authorization|cookie|^log$', re.I)
MASK = '[MASQUÉ]'
client_ip = ContextVar('audit_client_ip', default='inconnue')


def normalized_ip(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return 'inconnue'


def redact(value):
    """Retain the configuration structure, never credentials or arbitrary argv."""
    if isinstance(value, dict):
        return {key: MASK if SENSITIVE.search(key) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        if any(isinstance(item, str) and item.startswith('--') and SENSITIVE.search(item) for item in value):
            return MASK
        return [redact(item) for item in value]
    return value


def inline(value):
    # JSON escaping prevents log forging through line breaks or control characters.
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')


def audit(event, user, level='INFO', **details):
    timestamp = datetime.now(ZoneInfo(os.getenv('TZ', 'Europe/Paris'))).isoformat(timespec='seconds')
    identity = f"pseudo={inline(user.get('pseudo', 'Inconnu'))} | email={inline(user.get('email', ''))}"
    logger.log(getattr(logging, level), f'{timestamp} [{level}] [{event}] [{client_ip.get()}] {identity} | data={inline(redact(details))}')
