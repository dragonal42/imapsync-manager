"""Structured execution counters, independent of the retained debug tail."""
import re


def token_usage(engine, response):
    if not isinstance(response, dict):
        return {}
    usage = response.get('usage' if engine == 'Mistral' else 'usageMetadata') or {}
    if not isinstance(usage, dict):
        return {}
    names = ({'input': 'prompt_tokens', 'output': 'completion_tokens', 'total': 'total_tokens'}
             if engine == 'Mistral' else {'input': 'promptTokenCount', 'output': 'candidatesTokenCount',
                 'total': 'totalTokenCount', 'cached': 'cachedContentTokenCount', 'reasoning': 'thoughtsTokenCount'})
    result = {key: usage[name] for key, name in names.items()
              if type(usage.get(name)) is int and usage[name] >= 0}
    if engine == 'Mistral':
        details = usage.get('prompt_tokens_details') or {}
        cached = details.get('cached_tokens') if isinstance(details, dict) else None
        if type(cached) is int and cached >= 0:
            result['cached'] = cached
    return result


class Classification(str):
    def __new__(cls, verdict, usage):
        value = super().__new__(cls, verdict)
        value.usage = usage
        return value


class RunMetrics:
    def __init__(self, account):
        self.data = {'sync_enabled': account.get('bActiverSynchro', True),
                     'ai_enabled': account.get('bPretraitementIA', False),
                     'engine': account.get('sMoteurIA', 'Mistral'), 'transferred': None,
                     'analysed': 0, 'fraudulent': 0, 'quarantined': 0, 'ai_calls': 0,
                     'whitelisted': 0, 'blacklisted': 0, 'blacklist_moved': 0,
                     'folders': {}, 'usage': {}, 'usage_reports': {}, 'returncode': None}
        self.pending = b''

    def event(self, event):
        if 'folder_scan' in event:
            scan = event['folder_scan']
            self.data['folders'][scan['folder']] = dict(scan)
        if 'usage' in event:
            self.data['ai_calls'] += 1
            for key, count in event['usage'].items():
                if key in {'input', 'output', 'total', 'cached', 'reasoning'} and type(count) is int and count >= 0:
                    self.data['usage'][key] = self.data['usage'].get(key, 0) + count
                    self.data['usage_reports'][key] = self.data['usage_reports'].get(key, 0) + 1
        if 'verdict' in event:
            self.data['analysed'] += 1
            if event['verdict'] in {'spam', 'scam'}:
                self.data['fraudulent'] += 1
        for key in ('whitelisted', 'blacklisted', 'blacklist_moved'):
            if event.get(key):
                self.data[key] += 1
        if event.get('quarantined'):
            self.data['quarantined'] += 1

    def feed(self, chunk, final=False):
        lines = (self.pending + chunk).split(b'\n')
        self.pending = b'' if final else lines.pop()[-8192:]
        for line in lines:
            match = re.match(rb'^Messages transferred\s*:\s*(\d+)\b', line.strip())
            if match:
                self.data['transferred'] = int(match[1])

    def summary(self, status, note=''):
        d = self.data
        lines = ['Résultat : ' + ('OK' if status == 'Succès' else status)]
        if d['sync_enabled']:
            count = str(d['transferred']) if d['transferred'] is not None else 'non communiqué'
            lines.append('Emails transférés par imapsync : ' + count + '.')
        else:
            lines.append('Vérification de la source uniquement — sans synchronisation.')
        if d['ai_enabled']:
            for scan in d['folders'].values():
                lines.append(f"Dossier IA {scan['folder']} | Nb présents : {scan['total']} | Nb non lus non supprimés : {scan['unread']} | Nb candidats SINCE : {scan['candidates']} | Nb déjà traités : {scan['already_done']} | Nb hors période exacte : {scan['too_old']}")
            lines.append(f"Emails analysés par IA {d['engine']} : {d['analysed']} | Nb frauduleux/spams détectés : {d['fraudulent']} | Nb déplacés : {d['quarantined']}")
            if d["whitelisted"] or d["blacklisted"]:
                lines.append(f"Nb acceptés par WhiteList : {d['whitelisted']} | Nb détectés par BlackList : {d['blacklisted']} | Nb déplacés dans _02-BlackList : {d['blacklist_moved']}")
            if d['analysed'] > 0 or any(d['usage'].values()):
                labels = {'input': 'entrée', 'output': 'sortie', 'total': 'total', 'cached': 'cache', 'reasoning': 'raisonnement'}
                values = []
                for key, label in labels.items():
                    count = d['usage'].get(key)
                    if count is not None:
                        reports = d['usage_reports'][key]
                        suffix = f" (partiel : {reports}/{d['ai_calls']} appels)" if reports < d['ai_calls'] else ''
                        values.append(f'{label} : {count}{suffix}')
                lines.append(f"Consommation IA en tokens — cumul de cette exécution ({d['ai_calls']} appels) : " + (' ; '.join(values) if values else 'non communiquée') + '.')
                lines.append('Coût monétaire : non communiqué par le fournisseur (aucune estimation).')
        if d['returncode'] is not None:
            lines.append(f"Code de retour imapsync : {d['returncode']}.")
        if note:
            lines.append(note)
        return '\n'.join(lines)
