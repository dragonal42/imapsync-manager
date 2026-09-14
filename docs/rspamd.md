# Prétraitement local Rspamd

Le service Rspamd doit déjà fonctionner sur le même réseau Docker que l’application.
Le Compose utilise le réseau externe `imapsync-antispam`, créé par votre stack antispam.
Aucun port Rspamd ne doit être publié via Caddy. L’intégration ne déploie pas Rspamd.

Après fusion et reconstruction du conteneur dans Dockhand :

1. Dans `/admin`, activer Rspamd et conserver `http://rspamd:11333`.
2. Conserver d’abord la simulation obligatoire, les seuils 0 et 8 et le délai de 30 secondes.
3. Dans une configuration, cocher le prétraitement et choisir **Rspamd local**.
   Décocher la synchronisation pour vérifier uniquement la source.
4. Pour un test ponctuel, ouvrir la synchronisation manuelle, renseigner la source,
   choisir Rspamd dans l’analyse et lancer en simulation. Le journal détaillé apparaît
   dans **Suivi d’exécution**. La destination n’est pas nécessaire.
5. Examiner scores et motifs, puis calibrer les seuils avant de désactiver la simulation
   globale. Pour déplacer dans un test manuel, choisir aussi le mode Tri.

La simulation globale impose une simulation de TOUT le prétraitement utilisant Rspamd :
aucun déplacement ni marquage comme traité, y compris listes blanche/noire et IA éventuelle.
Elle n’empêche pas une synchronisation IMAP activée de transférer des messages.
Les réglages globaux sont relus au début de chaque exécution. Les locataires ne peuvent
changer ni l’adresse interne, ni les seuils, ni lever la simulation obligatoire.

## Trois parcours

- IA seule : Mistral ou Gemini sans prétri, fonctionnement existant.
- Local seul : Rspamd, aucune clé IA nécessaire. Score inférieur au seuil bas : conservé.
  Score supérieur ou égal au seuil haut : suspect. Entre les deux : conservé comme incertain.
- Prétri puis IA : sélectionner Mistral/Gemini et cocher le prétri Rspamd. Seuls les scores
  intermédiaires sont envoyés au fournisseur, regroupés selon sa taille de lot globale.

Les règles blanche/noire s’appliquent avant Rspamd. La période, les messages non lus,
non supprimés et non déjà traités, les dossiers INBOX et sous-dossiers autorisés restent
les mêmes. Les quarantaines sont exclues de l’analyse et du transfert lorsque le
prétraitement est activé. Les messages locaux suspects vont dans **INBOX._03-Suspects**
(séparateur adapté au serveur), les suspects IA dans `_01-Arnaques`.

Le score Rspamd mesure une suspicion de spam, pas une preuve de fraude. Un score faible
ne garantit pas l’innocuité. Pour réexaminer des messages déjà traités avec d’autres
réglages, utiliser la case de réanalyse du test manuel. Les simulations ne modifient pas
le suivi ; hors simulation, un résultat conservé est marqué comme traité.

## Transport et diagnostics

Un seul scan Rspamd est envoyé à la fois par le processus de l’application. Chaque scan
contient le message RFC822 complet (limite 2 Mio), sans mot de passe IMAP ni jeton OAuth.
L’application refuse les redirections HTTP et n’utilise pas les proxys d’environnement.
Les données restent dirigées vers l’adresse choisie par l’administrateur. Rspamd peut
effectuer ses propres consultations DNS/réputation : à configurer sur ce service pour
un fonctionnement hors Internet.

Le contexte SMTP original n’est pas reconstitué depuis des en-têtes non fiables : les
contrôles SPF_CHECK et DMARC_CHECK sont désactivés pour ces scans IMAP. Les autres règles
restent celles de votre installation. L’application ne demande aucun apprentissage ;
conserver `autolearn = false` côté Rspamd pour éviter un apprentissage automatique partagé
entre locataires. Le drapeau `no_log` évite le journal de tâche détaillé côté Rspamd.

Le debug affiche le score, les seuils, l’action, le temps de réponse et les symboles avec
leur poids. Il n’affiche pas le corps, les URLs ni les options des symboles. Le résumé
affiche les compteurs Rspamd séparément de la consommation des fournisseurs IA.

Une réponse ignorée/invalide, un refus temporaire ou une panne de Rspamd arrête le
prétraitement sans appliquer le résultat concerné. Le transfert IMAP reste maintenu
si activé ; l’avertissement est enregistré et repris dans le rapport quotidien existant.
Un déplacement vérifie COPYUID, la date d’arrivée et UIDVALIDITY avant la suppression
ciblée. Un déplacement interrompu exige une vérification manuelle comme pour l’IA.

Référence du protocole : https://docs.rspamd.com/developers/protocol/
