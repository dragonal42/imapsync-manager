# Service de diagnostic email (expérimental)

Accessible uniquement aux administrateurs : une tâche prédéfinie apparaît au-dessus des configurations du tableau de bord. Elle possède ses propres paramètres, boutons Lancer/Arrêter, RUNNING/PAUSED et journaux. Elle n'effectue aucun transfert imapsync vers une destination.

## Premier essai

1. Dans Administration ou le tableau de bord, ouvrir **Service de diagnostic email → Configurer**.
2. Renseigner une **boîte dédiée aux soumissions**, avec les mêmes identifiants IMAP TLS (port 993) ou connexion OAuth Google/Microsoft que les sources habituelles.
3. Choisir le dossier (INBOX par défaut), la période (5 jours par défaut ; 0 = toutes les dates), le maximum de messages par passage (50 par défaut) et l'intervalle par pas de 5 minutes.
4. Choisir Mistral ou Gemini. Les clés, modèles et quotas sont ceux du compte principal `ADMIN_EMAIL`, même lorsqu'un autre administrateur déclenche le service. Aucun compte utilisateur n'est utilisé par repli. Chaque message est analysé séparément dans cette première version ; les tailles de lots des synchronisations ne s'appliquent pas ici.
5. Laisser **Simulation** cochée, enregistrer puis lancer et consulter les logs. Les appels IA peuvent consommer les quotas/crédits même en simulation. Aucun email n'est envoyé, supprimé ou marqué comme traité en simulation.
6. Après vérification, décocher Simulation pour autoriser l'envoi et la suppression. Le serveur SMTP est celui du backend (`SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`). Cliquer sur Activer pour planifier la tâche ; elle est initialement PAUSED. Lancer reste possible en pause.

Un seul dossier est examiné, sans récursion, y compris les messages déjà lus. La période s'appuie sur la date d'arrivée IMAP. Les listes blanche/noire ne court-circuitent pas cette analyse. Les messages dépassant 2 Mio sont conservés avec erreur. Rspamd peut ajouter son score avant l'IA ; si sa simulation globale est active, le service reste également en simulation. L'IA existante analyse les métadonnées réduites, l'objet et les URLs ; les pièces jointes ne sont pas exécutées. Sans Rspamd, aucun score numérique n'est inventé.

## Compte rendu

Le destinataire est **le From du message reçu par cette boîte**, pas le Reply-To ni le From d'un message joint. Pour demander l'analyse d'un email suspect, l'utilisateur doit donc le transférer lui-même à cette boîte. Les adresses From pouvant être usurpées, cette boîte doit être réservée aux soumissions attendues, pas utilisée comme répondeur public à tous les spams reçus.

- **[DANGEREUX]** en rouge : suspicion de fraude/hameçonnage.
- **[SPAM]** en orange : indésirable/publicité.
- **[SAIN]** en vert : aucun signal suspect identifié (aucune garantie absolue).

Le compte rendu comprend le verdict, le score Rspamd si demandé, puis le texte du message original (limité à 20 000 caractères), échappé pour éviter l'affichage de contenu HTML actif. L'original complet est joint en `.eml`. Une version uniquement HTML reste consultable dans cette pièce jointe. Un verdict incertain ne déclenche ni envoi ni suppression.

## Reprise et journaux

La suppression se fait **après acceptation du compte rendu par SMTP**, uniquement pour l'UID concerné. UIDPLUS est exigé avant tout envoi réel ; aucun EXPUNGE global n'est effectué. L'acceptation SMTP ne garantit pas la livraison finale au destinataire. Les messages automatiques, listes de diffusion et réponses du service sont ignorés et conservés pour éviter les boucles.

Le fichier persistant `data/mail_verdict_state.json` mémorise les étapes d'envoi par serveur, boîte, dossier, UIDVALIDITY et UID. Conserver ce fichier avec le volume de données : le supprimer peut entraîner des renvois. Vider les logs ou supprimer les paramètres ne l'efface pas.

- Envoi confirmé, suppression échouée : le prochain passage reprend uniquement la suppression, sans renvoyer le compte rendu.
- Envoi ambigu (erreur SMTP, arrêt pendant l'envoi ou crash) : aucun renvoi automatique. Le journal indique l'UID et, en Debug, le Message-ID à rechercher dans les traces SMTP. Vérifier manuellement l'acceptation/livraison avant de déplacer le message ou d'intervenir sur son état. Il n'y a pas de bouton de relance forcée.
- Arrêter demande l'arrêt coopératif : un appel réseau en cours termine ou atteint son délai avant la libération du service. La configuration ne peut pas être modifiée pendant une exécution.

Les logs sont réservés aux administrateurs, avec les mêmes pages de consultation, suppression et durée de conservation que les autres tâches. Le mode Debug conserve les étapes détaillées et les diagnostics fournisseur expurgés ; sinon restent le résultat, les compteurs, les erreurs et les tokens communiqués par l'IA. Les erreurs/avertissements figurent aussi dans le rapport quotidien. Un passage réussi sans activité ne conserve pas de journal.

Tester avec une boîte dédiée avant utilisation réelle. Les tests automatisés utilisent des doubles IMAP, SMTP, Rspamd et IA : aucun envoi réel n'est effectué par ces tests.
