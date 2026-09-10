# IMAPSync Manager

Interface Docker / FastAPI pour synchroniser des boîtes IMAP, avec espaces utilisateurs cloisonnés et connexion sans mot de passe par email.

## Fonctionnalités

- `/` : présentation publique et logo ; `/cgu` et `/privacy` : conditions et confidentialité ; favicon commun.
- `/login` : lien magique envoyé seulement aux utilisateurs enregistrés. Réponse identique pour les adresses inconnues.
- Liens de 15 minutes, à usage unique, confirmation avant consommation pour résister aux scanners d’emails. Sessions de 12 heures, cookies `Secure`, `HttpOnly`, `SameSite=Lax`, révocation à la déconnexion.
- `/admin` : création d’utilisateurs (email + pseudo), filtre par propriétaire, paramètres globaux et applications OAuth.
- `/dashboard` : configurations et historiques propres à l’utilisateur ; création, modification, lancement, arrêt et suppression contrôlés côté serveur.
- Comptes Google et Microsoft via OAuth avec état aléatoire associé à la session et PKCE. Renouvellement des jetons avant exécution.
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

Les lancements manuels passent désormais par une configuration enregistrée, avec des boutons Lancer / Arrêter individuels. `/manual` redirige vers le tableau de bord ; l’ancien CGI libre retourne 410. Les paramètres CLI arbitraires ne sont plus acceptés. La copie ne supprime plus implicitement les messages source (`--delete1` a été retiré) ; aucun mode destructif n’est activé dans cette version.

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

## Remerciements

Basé sur [imapsync de Gilles LAMIRAL](https://imapsync.lamiral.info/).
