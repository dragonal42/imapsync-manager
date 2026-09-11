"""Per-owner exact sender address rules, shared by the UI and IMAP preprocessing."""
import re
from email import policy
from email.parser import BytesParser

KINDS = {'whitelist', 'blacklist'}
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}\Z")


def parse_import(text):
    if len(text) > 300000:
        raise ValueError('Import trop volumineux (1000 adresses maximum).')
    values = {part.strip().lower() for part in re.split(r'[;|,\t\r\n]+', text) if part.strip()}
    if not values or len(values) > 1000:
        raise ValueError('Saisissez de 1 à 1000 adresses email par import.')
    invalid = sorted(value for value in values if len(value) > 254 or not EMAIL.fullmatch(value))
    if invalid:
        raise ValueError('Adresses invalides : ' + ', '.join(invalid[:10]))
    return sorted(values)


def sender_address(raw):
    """Ambiguous/malformed From headers never bypass AI as a trusted sender."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
        headers = message.get_all('From', [])
        if len(headers) != 1 or headers[0].defects or len(headers[0].addresses) != 1:
            return None
        address = headers[0].addresses[0].addr_spec.lower()
        return address if EMAIL.fullmatch(address) else None
    except (ValueError, TypeError, AttributeError, IndexError):
        return None


def sender_decision(address, lists):
    if address and address in lists.get('blacklist', []):
        return 'blacklist'
    if address and address in lists.get('whitelist', []):
        return 'whitelist'
    return None
