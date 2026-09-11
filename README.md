# IMAPSync Manager

Interface Docker / FastAPI pour synchroniser des boîtes IMAP, avec espaces utilisateurs cloisonnés et connexion sans mot de passe par email.

## Fonctionnalités

- `/` : présentation publique et logo ; `/cgu` et `/privacy` : conditions et confidentialité ; favicon commun.
- `/login` : lien magique envoyé seulement aux utilisateurs enregistrés. Réponse identique pour les adresses inconnues.
- Liens de 15 minutes, à usage unique, confirmation avant consommation pour résister aux scanners d’emails. Sessions de 12 heures, cookies `Secure`, `HttpOnly`, `SameSite=Lax`, révocation à la déconnexion.
- `/admin` : page réservée à la création d’utilisateurs (email + pseudo), aux paramètres globaux et aux applications OAuth.
- `/dashboard` : compteurs d’utilisateurs, de configurations et d’erreurs du jour, puis configurations et historiques accessibles ; filtre par propriétaire pour l’administrateur. Les compteurs d’un locataire restent limités à son espace (un utilisateur).
- Bouton Nouvelle synchronisation dans Configurations, édition orange, confirmations et notifications SweetAlert2 servies localement (version 11.22.2, licence dans `static/vendor`).
- Planification `RUNNING` (vert) ou `PAUSED` (gris) : les configurations en pause sont ignorées par le planificateur, mais peuvent être lancées ponctuellement. Une mise en pause ne tue pas une exécution en cours ; utiliser Arrêter. Les anciennes configurations sont migrées vers RUNNING.
- Journaux filtrés par dates, aujourd’hui et les quatre jours précédents par défaut. Affichage à la seconde dans le fuseau `TZ` (Europe/Paris par défaut). Le filtre et les erreurs du jour utilisent la date de fin, ou de début pour une exécution en cours. Les compteurs ne sont pas limités par le filtre historique.
- Suppression d’un journal par ligne et vidage des journaux d’une configuration, avec confirmation. Les journaux en cours ne peuvent pas être supprimés. Les anciens journaux restent associés à leur propriétaire historique, même après réattribution de la configuration.
- Comptes Google et Microsoft via OAuth avec état aléatoire associé à la session et PKCE. Renouvellement des jetons avant exécution.
- Le choix d’authentification active uniquement les champs utiles : mot de passe IMAP actif et boutons OAuth grisés en mode classique ; mot de passe grisé et boutons actifs en mode OAuth2. Les retours OAuth apparaissent en gras, verts en cas de réussite avec invitation à enregistrer, rouges en cas d’erreur. Le bouton Enregistrer porte une coche et une bordure vertes.
- Historique associé au propriétaire au moment du lancement, au pseudo de l’acteur et à la configuration. Un transfert de propriété ne transfère pas les anciens journaux.

## Installation

1. Copier `.env.example` vers `.env` et renseigner `ADMIN_EMAIL`, `PUBLIC_URL`, `CADDY_URL` et les paramètres SMTP.
2. `PUBLIC_URL` doit être l’origine HTTPS publique exacte, sans chemin (par exemple `https://imapsync.example.com`). Elle sert aux liens magiques, au retour OAuth et au contrôle des requêtes de formulaires.
3. Préparer le volume `/opt/imapsyncmanager` avec un accès limité à l’exploitant et au conteneur. Ne pas le rendre accessible à tous (`chmod 777`).
4. Démarrer avec `docker compose up -d --build`. Le réseau externe `proxy-net` et le proxy Caddy doivent déjà être configurés.
5. Ouvrir `/login`, saisir `ADMIN_EMAIL`, suivre le lien reçu et confirmer la connexion. Créer ensuite les utilisateurs dans l’administration.

Le SMTP du lien magique réutilise `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_USER` et `SMTP_PASS`. `SMTP_SECURITY=starttls` convient au port 587 ; utiliser `ssl` et le port 465 si nécessaire. Aucun email d’invitation n’est automatiquement envoyé lors de la création d’un utilisateur.

Le port applicatif doit rester accessible au seul proxy / réseau de confiance. Les cookies sécurisés nécessitent HTTPS. La protection Basic Auth globale du proxy a été retirée pour que les pages publiques soient accessibles. Supprimer également toute ancienne règle équivalente gérée hors de ce fichier Compose.

## Migration depuis la version mono-utilisateur

Sauvegarder le volume et arrêter l’ancienne instance avant la mise à jour. Au premier chargement, `config.json` est sauvegardé dans `config.pre-saas.json`. Les paramètres IMAP et OAuth existants sont conservés. Une liste `users` est créée et chaque configuration sans `owner` est attribuée à `ADMIN_EMAIL`.

Extrait de structure (autres champs IMAP inchangés) :

```json
{
  "users": [
    {"email": "admin@example.com", "pseudo": "Administrateur", "role": "admin"},
    {"email": "alice@example.com", "pseudo": "Alice", "role": "user"}
  ],
  "accounts": [{"id": "identifiant", "owner": "alice@example.com", "label": "Migration"}],
  "runs": []
}
```

Les anciennes erreurs non structurées ne sont pas exposées aux locataires : elles n’ont pas de propriétaire fiable. Le rapport quotidien global reste réservé à une adresse choisie par l’administrateur. Les nouveaux historiques sont dans `runs` ; les secrets de connexion ne sont pas réinjectés dans les formulaires.

Le menu Synchronisation manuelle ouvre `/manual` et son template `manual.html`. Ce formulaire lance une exécution ponctuelle (mot de passe IMAP ou OAuth), avec suivi du journal et arrêt individuel. Les identifiants restent en mémoire pour cette exécution, sans créer de configuration automatique. L’historique appartient à l’utilisateur connecté. Une seule exécution manuelle à la fois est autorisée par utilisateur.

Le endpoint `/cgi-bin/imapsync` accepte les champs du formulaire et les options explicitement proposées (simulation, test de connexion, dossiers, tailles, sous-dossiers, suppression source/destination). Les arguments CLI libres restent interdits. Les options de suppression sont décochées par défaut et nécessitent une confirmation SweetAlert2. Les configurations automatiques ne suppriment pas implicitement les messages source.

Chaque exécution dispose de son propre dossier temporaire et fichier PID : les processus de locataires différents ne partagent pas leurs fichiers de suivi.

## Exploitation du stockage JSON

Déployer **une seule instance et un seul worker Uvicorn** sur le volume. Le planificateur et les transactions JSON sont gérés par la même boucle événementielle. Les écritures sont atomiques et les mises à jour après exécution relisent la configuration pour conserver les modifications intervenues pendant la synchronisation. Plusieurs workers/réplicas nécessiteraient une base transactionnelle et une file de tâches.

`data/auth.json` ne conserve que les empreintes des liens et sessions, avec expiration. Un redémarrage conserve les sessions valides. Les identifiants IMAP et jetons de renouvellement restent dans le volume JSON : protéger le volume et ses sauvegardes, qui ne sont pas chiffrés par l’application. Ne pas partager ce stockage avec les utilisateurs.

Les journaux d’exécution conservent au plus 64 Kio par exécution, masquent les secrets connus et excluent les lignes d’authentification. Les historiques s’accumulent : leur rétention / purge relève de l’exploitant. Les journaux HTTP Uvicorn sont désactivés pour éviter d’enregistrer les liens de connexion ; configurer également le proxy pour ne pas journaliser les paramètres `token`, `code` et `state`.

Pour révoquer un utilisateur, arrêter l’instance, retirer son entrée `users` et ses données selon votre politique de rétention, puis redémarrer. Les sessions d’un email absent sont refusées. Changer `ADMIN_EMAIL` crée ou promeut le nouvel administrateur sans supprimer les anciens administrateurs : retirer explicitement l’ancien rôle si souhaité.

## Branding Google OAuth

Après déploiement, configurer dans Google Cloud le nom **IMAPSync Manager**, le logo, le domaine autorisé et vérifié, l’email de support, l’accueil `PUBLIC_URL/`, les conditions `PUBLIC_URL/cgu`, la confidentialité `PUBLIC_URL/privacy` et le retour OAuth `PUBLIC_URL/oauth/callback`.

Les textes publics fournis décrivent le fonctionnement technique ; les compléter avec l’identité de l’exploitant, son contact, la rétention effective et les conditions propres au service avant publication. Vérifier aussi la cohérence avec les pratiques d’hébergement et les obligations applicables.

La présence de ces pages prépare la demande sans garantir l’approbation Google. La vérification de marque et celle des autorisations sensibles/restreintes sont distinctes. Le scope IMAP Gmail utilisé reste `https://mail.google.com/`.

Référence officielle : [vérification de marque Google](https://developers.google.com/identity/protocols/oauth2/production-readiness/brand-verification).

## Tests

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Les tests utilisent un stockage temporaire et simulent les envois / exécutions : aucun email réel n’est envoyé et aucune boîte mail n’est synchronisée. Ils couvrent les accès croisés, les mutations réservées aux administrateurs, l’attribution de propriétaire, la migration, les liens et cookies, la révocation, OAuth, les arrêts et la conservation des modifications concurrentes.

Les vérifications SMTP réelles, les parcours Google/Microsoft et les synchronisations réelles nécessitent les identifiants et l’environnement de déploiement.

Le test navigateur facultatif se lance avec `python tests/browser_magic_link.py` après installation de Playwright et de Chromium (Edge sous Windows). Il couvre aussi les confirmations, la pause, les suppressions de journaux et la synchronisation manuelle avec processus simulé.

L’image installe `procps` pour fournir `ps`, utilisé par imapsync, et `tzdata` pour les fuseaux horaires. Le build vérifie `ps`, la compilation Perl et `imapsync --version`. En cas d’erreur de dépendance, reconstruire l’image et recréer le conteneur ; un simple redémarrage ne suffit pas. Le code 64 indique un problème d’utilisation d’imapsync : les diagnostics d’erreur sont conservés dans le journal tout en masquant les secrets connus.

Les champs web `oauth2_token1/2` sont transmis au moteur sous ses options réelles `--oauthaccesstoken1/2`. Les anciens arguments `--oauth2_token1/2` ne sont pas reconnus par l’imapsync embarqué et provoquent le code 64. Le build teste désormais aussi les noms d’options OAuth avec `--version`, sans connexion IMAP. Les tests Python exercent le véritable analyseur d’arguments extrait du script lorsque Perl est disponible.

## Vérification de source et prétraitement IA

Dans **Administration**, enregistrer une clé Mistral et/ou Gemini. Un champ vide conserve la clé ; la case « Effacer » la retire. Ces secrets sont enregistrés côté serveur dans `data/config.json` (permissions 0600) et ne sont jamais affichés dans les formulaires. Protéger et sauvegarder le volume de données.

Dans une configuration, **Activer la synchronisation** est coché par défaut, y compris pour les anciennes configurations. Décocher désactive entièrement la destination. Sans IA, cette tâche vérifie la connexion IMAP source et l’accès au dossier ; avec IA, elle effectue le tri sans lancer imapsync. La planification RUNNING/PAUSED reste applicable aux deux modes.

**Prétraitement IA sur la source** est désactivé par défaut. Choisir Mistral/Gemini et une période de 1 à 365 jours (5 par défaut). L’IA parcourt exclusivement `INBOX` et ses sous-dossiers, avec le séparateur fourni par le serveur (`.` ou `/`, par exemple). Les branches `Sent`, `Trash`, `Junk`, `Drafts`, `Archive`, `spam`, `_01-Arnaques` et `_02-BlackList`, ainsi que leurs descendants, sont exclues sans distinction de casse. Les autres dossiers racine ne sont pas analysés. Seuls les messages non lus, non supprimés, non encore traités et reçus durant la période sont analysés. Le champ « Dossier à vérifier sans IA » concerne uniquement les tâches de vérification sans IA.

L’IA reçoit uniquement l’objet, une sélection bornée d’en-têtes et jusqu’à 30 URLs, jamais les pièces jointes ou le texte complet. Le corps est lu avec `BODY.PEEK[]`, sans marquer le message comme lu ; aucune URL n’est visitée. Les réponses sont validées strictement. Les messages légitimes ou incertains restent en place. Les messages spam/arnaque sont copiés dans le sous-dossier `_01-Arnaques`, puis supprimés de la source avec `UID EXPUNGE` ciblé. Le serveur doit supporter UIDPLUS : la date d’arrivée `INTERNALDATE` de la copie et son UID sont vérifiés avant de supprimer l’original. La quarantaine est exclue de l’étape imapsync lorsque le prétraitement est activé.

Une erreur du prétraitement (API/IMAP, réponse invalide ou message dépassant 2 Mio) arrête l’analyse IA mais laisse continuer le transfert imapsync. Si le transfert réussit, le résultat est **Succès avec avertissement** ; si le transfert échoue, le résultat reste **Erreur**. Le journal, même sans Debug, et le rapport quotidien contiennent l’avertissement IA et la consommation connue. Une tâche sans synchronisation reste en erreur si sa vérification échoue. Une demande d’arrêt ne lance jamais le transfert. La limite de 2 Mio porte sur le message complet, pièces jointes comprises. Une classification IA peut se tromper : les messages déplacés restent consultables dans `INBOX/_01-Arnaques` (séparateur adapté au serveur).

Les exclusions IA ne retirent pas `Sent`, `spam`, etc. du transfert. Les configurations enregistrées utilisent `--automap` pour faire correspondre les dossiers spéciaux, dont le spam, à ceux de la destination lorsqu’imapsync les reconnaît. Ce mécanisme dépend des noms et attributs exposés par les serveurs ; contrôler la correspondance en Debug pour une destination inhabituelle. Les deux quarantaines restent exclues du transfert lorsque l’IA est activée. `--nofoldersizes --nofoldersizesatend` suppriment les calculs de taille avant/après transfert, sauf demande manuelle explicite `--justfoldersizes`. Imapsync consulte toujours la destination pour établir les correspondances et éviter les doublons : il ne s’agit pas d’un transfert inverse.

Chaque dossier analysé a un bilan dans tous les niveaux de journal : messages présents, non lus non supprimés, candidats au filtre IMAP SINCE, candidats déjà traités et messages finalement trop anciens après contrôle précis de la date d’arrivée. SINCE est élargi d’un jour pour les fuseaux horaires ; le contrôle INTERNALDATE applique ensuite la période exacte. Un résultat zéro ne signifie donc pas que le dossier était vide. Les dossiers exclus ne sont pas ouverts par le prétraitement.

Le rapport quotidien utilise l’adresse de rapport configurée, ou à défaut celle d’un administrateur enregistré. Sans destinataire ou si msmtp échoue, le rapport est conservé pour une prochaine tentative.

Le suivi des UID et UIDVALIDITY persiste par dossier dans `data/ai_state.json`, indépendamment des journaux. Vider les logs ne relance pas l’analyse des messages déjà traités. Une copie interrompue ou dont la date ne peut être confirmée bloque la reprise automatique du prétraitement pour éviter les copies multiples et une suppression non vérifiée ; le transfert reste autorisé et peut donc supprimer l’original après transfert si `--delete1` est activé. Dans ce cas, mettre la tâche en PAUSED, attendre/arrêter son exécution, vérifier l’original, la destination et sa copie dans `_01-Arnaques`, puis faire corriger par l’administrateur uniquement l’entrée UID concernée dans `ai_state.json` (marquer `done` si le déplacement est confirmé, ou retirer l’entrée uniquement après avoir restauré l’original et retiré la copie ambiguë). Ne pas effacer tout le suivi sans cette vérification.

Les modèles peuvent être remplacés dans `.env` avec `MISTRAL_MODEL` (défaut `mistral-small-latest`) et `GEMINI_MODEL` (défaut `gemini-2.5-flash`). Les appels utilisent les interfaces REST [Mistral Chat](https://docs.mistral.ai/api/endpoint/chat) et [Gemini Generate Content avec sortie structurée](https://ai.google.dev/gemini-api/docs/generate-content/structured-output).

L’option de suppression après transfert transmet `--delete1` une seule fois. Les messages `Info: turning on --expunge1...` sont informatifs : aucun `--noexpunge1` n’est ajouté. Le code de retour du processus décide du résultat, affiché **OK** pour une réussite ; la page de journal et le tableau de bord actualisent le statut automatiquement. Un journal encore en cours de lecture des dossiers ne constitue pas une preuve de fin du processus.

Après fusion, reconstruire l’image (`docker compose up -d --build imapsync-manager`). Les tests simulent les fournisseurs IA et IMAP, sans envoyer de données à un fournisseur ni déplacer de vrais messages.

## Niveaux de log, consommation IA et conservation

### Diagnostic IA manuel

En Debug, chaque échec fournisseur est désormais écrit immédiatement après l’appel avec le marqueur `[ERROR IA]`, avant la déconnexion IMAP. Les lignes `[ERROR IA DEBUG]` affichent les champs diagnostiques disponibles (`message`, `type`, `code`, `param`, `status`), l’identifiant de requête, `Retry-After` et les en-têtes de limites/solde/réinitialisation transmis par le fournisseur. Ces détails sont bornés et masqués : pas de dump du corps brut, des cookies, des autorisations ou des entrées de la requête. Le message fournisseur et les métadonnées envoyées sont filtrés pour masquer les secrets, emails et URLs. Les champs absents ne sont pas inventés. En journal minimal, seule la cause générale reste affichée.

Pour une erreur Mistral 429, consulter [Limits dans le panneau d’administration](https://admin.mistral.ai/plateforme/limits), dans l’organisation de la clé API utilisée. Contrôler requêtes par seconde, tokens par minute et consommation mensuelle ; plusieurs applications peuvent partager ces limites. Voir [l’aide officielle Mistral](https://help.mistral.ai/fr/articles/698531-pourquoi-est-ce-que-j-atteins-mes-limites-d-usage-api-et-comment-les-augmenter). Un simple « Rate limit exceeded » ne désigne pas nécessairement la limite précise : comparer les compteurs de la console ou transmettre l’identifiant de requête au support. Le code ne relance pas automatiquement les requêtes refusées.

Dans `/manual`, la section **Transfert IMAP** conserve les commandes de transfert. La nouvelle section **Analyse IA** utilise uniquement la connexion Source : la destination peut rester vide. Choisir Mistral/Gemini, la période, le dossier IMAP (INBOX par défaut), les sous-dossiers éventuels et, si nécessaire, la réanalyse des messages déjà traités. Les filtres non lu, non supprimé et récent restent actifs ; les listes personnelles d’expéditeurs restent appliquées. Le dossier explicitement saisi est analysé, même si son nom correspond à une exclusion automatique ; les branches spéciales restent exclues du parcours récursif. Les configurations automatiques conservent leur périmètre INBOX habituel.

Le mode **Simulation** ne déplace rien et ne modifie pas le suivi des UID ; les appels fournisseur restent réels et peuvent consommer des tokens. Le mode **Tri** demande une confirmation avant de déplacer les spams/arnaques vers les quarantaines d’INBOX. Une copie précédemment interrompue bloque toujours le tri pour éviter une suppression non vérifiée.

Le bouton **Lancer l’Analyse IA** alimente **Suivi d’exécution**, avec arrêt et lien vers l’historique privé, comme un transfert. Le Debug est forcé pour ce lancement uniquement. Les identifiants ne sont pas enregistrés comme configuration ; les traces Docker distinguent MANUAL_AI_REQUESTED et MANUAL_AI_FINISHED.

Les diagnostics montrent les étapes IMAP, le modèle, les tailles des métadonnées, le nombre d’en-têtes/URLs, le résultat de validation, la durée et les tokens connus. Les erreurs distinguent HTTP 401 (clé), 403 (droits), 404 (modèle/endpoint), 429 (quota/débit), TLS, DNS/connexion, timeout, limite de génération, structure de réponse absente et JSON/verdict invalide. Aucun corps de réponse fournisseur, email, URL extraite ou clé n’est recopié dans les diagnostics. Les erreurs détaillées restent également visibles dans les tâches automatiques et leur rapport quotidien.

Pour diagnostiquer un ancien message générique : reconstruire l’image après fusion, renseigner la source dans `/manual`, laisser **Simulation**, puis lancer l’analyse et relever la nouvelle ligne d’erreur. Les anciennes exécutions ne contiennent pas les détails perdus et ne peuvent pas être enrichies rétroactivement.

Dans **Administration → Journaux**, « Log : Niveau Debug » est décoché par défaut. Les nouvelles exécutions conservent alors uniquement le résultat, le nombre de messages transférés annoncé par le bilan imapsync, les compteurs IA (analysés, frauduleux/spams détectés, effectivement déplacés) et les erreurs utiles. Si imapsync ne fournit pas son bilan, le compteur indique « non communiqué », pas zéro. Le mode Debug conserve le détail actuel (fin de journal limitée à 64 Kio, secrets masqués) et le même résumé. Le niveau est fixé au début de l’exécution ; changer la case ne recrée pas les détails d’un ancien journal résumé.

Les deux niveaux, ainsi que le rapport quotidien d’erreurs, incluent la consommation de tokens renvoyée par Mistral ou Gemini : entrée, sortie, total et, si disponibles, cache et raisonnement. Les compteurs cache/raisonnement sont présentés séparément sans les ajouter une seconde fois au total fournisseur. Les appels dont la réponse de classification est invalide restent comptabilisés si leur usage est disponible. Une information absente est indiquée comme non communiquée ; les totaux incomplets sont signalés comme partiels. Aucun montant monétaire n’est inventé à partir de ces compteurs.

La conservation est de **90 jours par défaut**, réglable de 1 à 3650 jours. La rotation intégrée purge les entrées expirées et leur contenu dans `config.json`, ainsi que les lignes datées expirées de `daily_errors.log`, au démarrage, à l’enregistrement des paramètres et chaque heure dans une tâche indépendante du planificateur IMAP. Elle utilise la date de fin, ou la date de début pour les anciens journaux sans date de fin. Les journaux en cours et ceux dont la date est inexploitable sont conservés. Les configurations, comptes, dernières dates d’exécution et le suivi anti-doublon `ai_state.json` restent intacts. Il ne faut pas appliquer le programme système logrotate directement à `config.json`, qui contient également les configurations.

Après fusion, reconstruire le conteneur pour inclure le nouveau module `run_logging.py`. Les essais automatisés simulent IMAP et les réponses IA et ne traitent aucun email réel.

## Remerciements

Basé sur [imapsync de Gilles LAMIRAL](https://imapsync.lamiral.info/).


### Exemple du cumul Mistral

L’appel Chat Completions précise `"stream": false` pour recevoir la réponse JSON complète. Le résultat se trouve dans `choices` et la consommation dans `usage`. Les compteurs sont additionnés pour tous les appels IA d’une même exécution, en mode résumé comme en Debug. Ils repartent de zéro à la prochaine exécution ; ce n’est pas un total mensuel du compte Mistral.

Exemple : une première réponse annonce 25 tokens en entrée et 10 en sortie (35 au total), puis une seconde 40 en entrée et 15 en sortie (55 au total). Le journal affiche :

```text
Consommation IA en tokens — cumul de cette exécution (2 appels) : entrée : 65 ; sortie : 25 ; total : 90.
```

Référence : [API Mistral Chat Completions](https://docs.mistral.ai/api/endpoint/chat).


### Traces d’audit dans les logs Docker

Les événements utilisateur sont émis immédiatement sur stdout, indépendamment de « Log : Niveau Debug », et consultables avec `docker compose logs imapsync-manager`. Chaque événement tient sur une ligne : date à la seconde avec fuseau `TZ`, niveau, type, pseudo, email et données JSON compactes.

```text
2026-09-11T10:15:00+02:00 [INFO] [MAGIC_LINK_REQUEST] pseudo="Alice" | email="alice@example.com" | data={"outcome":"accepted"}
2026-09-11T10:15:01+02:00 [INFO] [MAGIC_LINK_SENT] pseudo="Alice" | email="alice@example.com" | data={}
2026-09-11T10:16:12+02:00 [INFO] [LOGIN_SUCCESS] pseudo="Alice" | email="alice@example.com" | data={}
2026-09-11T10:20:00+02:00 [INFO] [CONFIG_DELETED] pseudo="Alice" | email="alice@example.com" | data={"configuration":{"id":"exemple","label":"Migration","owner":"alice@example.com","pass1":"[MASQUÉ]"}}
```

Les demandes refusées sont distinguées par `unknown_user` ou `rate_limited` dans `outcome`. Une demande acceptée ne signifie pas que le mail a été envoyé : `MAGIC_LINK_SENT` confirme l’envoi SMTP, `MAGIC_LINK_DELIVERY_FAILED` signale un échec. `LOGIN_SUCCESS` n’est émis qu’après création de la session ; ouvrir la page du lien sans confirmer ne constitue pas une connexion.

`CONFIG_DELETED` contient la configuration supprimée, après sauvegarde réussie, et identifie l’utilisateur qui a effectué la suppression (qui peut être l’administrateur). Les mots de passe, jetons, secrets et arguments contenant des identifiants sont masqués. Aucun Magic Link, cookie de session, jeton OAuth ou message brut du serveur SMTP n’est journalisé. Les retours à la ligne des données sont échappés pour préserver une ligne par événement.

Les synchronisations manuelles produisent `MANUAL_SYNC_REQUESTED` à l’acceptation de la demande et `MANUAL_SYNC_FINISHED` à la fin, avec le même `run_id`, le résultat et les compteurs. Une demande rejetée par la validation n’est pas annoncée comme lancée.

Exemples de recherche :

```bash
docker compose logs --no-log-prefix imapsync-manager | grep -F '[LOGIN_SUCCESS]'
docker compose logs --no-log-prefix imapsync-manager | grep -F 'alice@example.com'
docker compose logs --no-log-prefix imapsync-manager | grep -F '[CONFIG_DELETED]'
```

Ces traces relèvent de la conservation des logs Docker configurée sur l’hôte ; la purge applicative des historiques à 90 jours ne supprime pas les logs Docker. Reconstruire l’image après fusion pour inclure `audit_logging.py`.


### Listes d’expéditeurs par utilisateur

Le menu **Mes listes d’expéditeurs** ouvre `/sender-lists`. Chaque utilisateur (administrateur compris) gère ses propres WhiteList et BlackList. Lorsqu’un administrateur lance une configuration d’un autre utilisateur, les listes du propriétaire de la configuration sont utilisées, jamais celles de l’administrateur.

L’import accepte des adresses seules séparées par `;`, `|`, `,`, TAB ou retour à la ligne. Elles sont normalisées en minuscules, dédupliquées et validées côté serveur. SweetAlert présente la liste exacte, le nombre d’ajouts et les adresses déjà présentes avant confirmation. Une adresse déjà dans l’autre liste bloque l’import : il faut d’abord la retirer de cette autre liste. Limites : 1000 adresses distinctes par import et 10000 par liste. La suppression d’une adresse est immédiate, sans confirmation. La recherche « contient » filtre les deux listes sans distinction de majuscules.

Les règles s’appliquent au prétraitement IA activé, dans la période configurée et uniquement aux messages non lus, non supprimés et non encore traités. Les listes sont prises au début du prétraitement et stockées dans l’entrée `users` du propriétaire, sous `sender_lists.whitelist` et `sender_lists.blacklist` dans `config.json`. Un changement de liste ne relance pas l’analyse des UID déjà traités.

- WhiteList : l’adresse exacte de l’en-tête From est acceptée immédiatement, sans lire le corps ni appeler l’IA. Le message reste non lu et peut être synchronisé normalement.
- BlackList : déplacement sans IA vers le sous-dossier `_02-BlackList` d’INBOX (par exemple `INBOX._02-BlackList` sur Dovecot), quelle que soit la source vérifiée. Le déplacement conserve la vérification COPYUID/INTERNALDATE et la suppression ciblée de l’original. Le corps n’est pas chargé, y compris pour les messages dépassant la limite d’analyse IA de 2 Mio.
- Hors listes, ou From ambigu/malformé : parcours IA habituel. Un nom d’affichage ne suffit jamais pour reconnaître une adresse autorisée.

Une adresse From peut être usurpée : une WhiteList est une décision explicite d’accepter cet expéditeur sans analyse IA, pas une preuve d’authenticité. Si une ancienne configuration de listes contient malgré tout une adresse dans les deux listes, la BlackList est prioritaire. Les deux dossiers de quarantaine sont exclus de l’étape imapsync lorsque le prétraitement est activé. En cas de copie interrompue, vérifier également INBOX/_02-BlackList avant de corriger une entrée du suivi `ai_state.json`.

Le résumé affiche désormais :

```text
Emails analysés par IA Mistral : 0 | Nb frauduleux/spams détectés : 0 | Nb déplacés : 0
```

Sans appel IA, les lignes de consommation/coût sont omises. Les traitements WhiteList/BlackList ont leurs propres compteurs et ne sont pas comptés comme analyses IA. Si une tentative IA échoue après avoir consommé des tokens, la consommation connue reste visible pour ne pas masquer cet usage.

### Adresse IP dans les traces Docker, derrière Caddy

Chaque événement d’audit comprend l’IP client, par exemple `[LOGIN_SUCCESS] [198.51.100.23] pseudo="Alice" | email="alice@example.com"`. IPv4 et IPv6 sont prises en charge. Le contexte IP accompagne aussi l’envoi SMTP et la fin des tâches manuelles lancées en arrière-plan. Les en-têtes réseau bruts ne sont pas écrits dans le journal.

Pour recevoir l’IP publique transmise par Caddy, renseigner **FORWARDED_ALLOW_IPS dans `.env` avec l’IP interne exacte du conteneur Caddy**, puis recréer l’application. Exemple de repérage (le réseau du compose est `proxy-net`) :

```bash
docker network inspect proxy-net --format '{{range .Containers}}{{.Name}} {{.IPv4Address}}{{println}}{{end}}'
```

Utiliser l’adresse du conteneur Caddy sans son suffixe de masque, pas l’adresse publique du visiteur. Plusieurs IP ou un CIDR de proxies réellement de confiance peuvent être séparés par une virgule. La valeur par défaut est `127.0.0.1`, adaptée uniquement à un proxy local de confiance. Pour Caddy dans un autre conteneur, cette valeur doit être adaptée ; sinon le journal montrera l’adresse de Caddy, pas celle du visiteur. Si l’adresse de Caddy change, mettre ce réglage à jour.

Uvicorn n’accepte X-Forwarded-For que depuis les proxies déclarés. Ne pas mettre `*` lorsque le port de l’application est joignable directement : un client pourrait alors choisir l’IP inscrite dans les logs. Si un CDN est placé devant Caddy, ses proxies doivent aussi être configurés correctement dans Caddy. Une IP ne peut pas être déduite au-delà de ce que fournit la chaîne de proxies. Les adresses absentes ou invalides sont indiquées `inconnue`, jamais remplacées par une IP supposée.

Après fusion : `git pull` puis `docker compose up -d --build imapsync-manager`. Aucune modification de la configuration Caddy de production n’est effectuée par le code du dépôt.
