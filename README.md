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

**Prétraitement IA sur la source** est désactivé par défaut. Choisir Mistral/Gemini, une période de 1 à 365 jours (5 par défaut), et le dossier source (`INBOX` par défaut ; nom IMAP ASCII). Seuls les messages non lus, non supprimés, non encore traités et reçus durant cette période sont analysés. Le dossier choisi concerne la vérification IA ; imapsync conserve son comportement de synchronisation des dossiers.

L’IA reçoit uniquement l’objet, une sélection bornée d’en-têtes et jusqu’à 30 URLs, jamais les pièces jointes ou le texte complet. Le corps est lu avec `BODY.PEEK[]`, sans marquer le message comme lu ; aucune URL n’est visitée. Les réponses sont validées strictement. Les messages légitimes ou incertains restent en place. Les messages spam/arnaque sont copiés dans le sous-dossier `_01-Arnaques`, puis supprimés de la source avec `UID EXPUNGE` ciblé. Le serveur doit supporter UIDPLUS : la date d’arrivée `INTERNALDATE` de la copie et son UID sont vérifiés avant de supprimer l’original. La quarantaine est exclue de l’étape imapsync lorsque le prétraitement est activé.

Une erreur API/IMAP ou un message dépassant 2 Mio interrompt le traitement et empêche la synchronisation suivante pour cette exécution ; le journal indique la cause sans exposer les emails ni les clés. La limite porte sur le message complet, pièces jointes comprises. Une classification IA peut se tromper : les messages déplacés restent consultables et récupérables dans `_01-Arnaques`.

Le suivi des UID et UIDVALIDITY persiste dans `data/ai_state.json`, indépendamment des journaux. Vider les logs ne relance pas l’analyse des messages déjà traités. Une copie interrompue ou dont la date ne peut être confirmée bloque la reprise automatique pour éviter les copies multiples et une suppression non vérifiée. Dans ce cas, mettre la tâche en PAUSED, attendre/arrêter son exécution, vérifier l’original et sa copie dans `_01-Arnaques`, puis faire corriger par l’administrateur uniquement l’entrée UID concernée dans `ai_state.json` (marquer `done` si le déplacement est confirmé, ou retirer l’entrée uniquement après avoir restauré l’original et retiré la copie ambiguë). Ne pas effacer tout le suivi sans cette vérification.

Les modèles peuvent être remplacés dans `.env` avec `MISTRAL_MODEL` (défaut `mistral-small-latest`) et `GEMINI_MODEL` (défaut `gemini-2.5-flash`). Les appels utilisent les interfaces REST [Mistral Chat](https://docs.mistral.ai/api/endpoint/chat) et [Gemini Generate Content avec sortie structurée](https://ai.google.dev/gemini-api/docs/generate-content/structured-output).

L’option de suppression après transfert transmet `--delete1` une seule fois. Les messages `Info: turning on --expunge1...` sont informatifs : aucun `--noexpunge1` n’est ajouté. Le code de retour du processus décide du résultat, affiché **OK** pour une réussite ; la page de journal et le tableau de bord actualisent le statut automatiquement. Un journal encore en cours de lecture des dossiers ne constitue pas une preuve de fin du processus.

Après fusion, reconstruire l’image (`docker compose up -d --build imapsync-manager`). Les tests simulent les fournisseurs IA et IMAP, sans envoyer de données à un fournisseur ni déplacer de vrais messages.

## Niveaux de log, consommation IA et conservation

Dans **Administration → Journaux**, « Log : Niveau Debug » est décoché par défaut. Les nouvelles exécutions conservent alors uniquement le résultat, le nombre de messages transférés annoncé par le bilan imapsync, les compteurs IA (analysés, frauduleux/spams détectés, effectivement déplacés) et les erreurs utiles. Si imapsync ne fournit pas son bilan, le compteur indique « non communiqué », pas zéro. Le mode Debug conserve le détail actuel (fin de journal limitée à 64 Kio, secrets masqués) et le même résumé. Le niveau est fixé au début de l’exécution ; changer la case ne recrée pas les détails d’un ancien journal résumé.

Les deux niveaux, ainsi que le rapport quotidien d’erreurs, incluent la consommation de tokens renvoyée par Mistral ou Gemini : entrée, sortie, total et, si disponibles, cache et raisonnement. Les compteurs cache/raisonnement sont présentés séparément sans les ajouter une seconde fois au total fournisseur. Les appels dont la réponse de classification est invalide restent comptabilisés si leur usage est disponible. Une information absente est indiquée comme non communiquée ; les totaux incomplets sont signalés comme partiels. Aucun montant monétaire n’est inventé à partir de ces compteurs.

La conservation est de **90 jours par défaut**, réglable de 1 à 3650 jours. La rotation intégrée purge les entrées expirées et leur contenu dans `config.json`, ainsi que les lignes datées expirées de `daily_errors.log`, au démarrage, à l’enregistrement des paramètres et chaque heure dans une tâche indépendante du planificateur IMAP. Elle utilise la date de fin, ou la date de début pour les anciens journaux sans date de fin. Les journaux en cours et ceux dont la date est inexploitable sont conservés. Les configurations, comptes, dernières dates d’exécution et le suivi anti-doublon `ai_state.json` restent intacts. Il ne faut pas appliquer le programme système logrotate directement à `config.json`, qui contient également les configurations.

Après fusion, reconstruire le conteneur pour inclure le nouveau module `run_logging.py`. Les essais automatisés simulent IMAP et les réponses IA et ne traitent aucun email réel.

## Remerciements

Basé sur [imapsync de Gilles LAMIRAL](https://imapsync.lamiral.info/).
