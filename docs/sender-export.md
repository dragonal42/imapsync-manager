# Extraire les destinataires des messages envoyés

Dans `/manual`, renseigner la Source puis cliquer sur **Extraire les destinataires To**.
La destination et les paramètres antispam ne sont pas utilisés.

- **Dossier Envoyés** : `Sent` par défaut, modifiable selon le serveur (`INBOX.Sent`,
  `[Gmail]/Sent Mail`, etc.). Un seul dossier est lu, sans parcourir ses sous-dossiers.
- **Nombre de messages envoyés à examiner** : 500 par défaut, de 1 à 10000.
  Les messages ayant les UID les plus élevés sont examinés en premier. Cet ordre
  correspond à leur ajout dans le dossier, pas nécessairement à leur date d’envoi
  lorsqu’une archive a été importée.
- **Exclure les destinataires déjà dans ma liste blanche** : coché par défaut,
  utilise uniquement la liste blanche de l’utilisateur connecté.

La limite porte sur les messages, avant déduplication et filtrage des adresses.
Seul l’en-tête **To** est extrait : ni From, ni Cc, ni Bcc. Les dossiers sont ouverts
 en lecture seule, les en-têtes récupérés par lots de 100 ; aucun message n’est
marqué comme lu et aucune analyse IA/Rspamd ou synchronisation n’est lancée.

À la fin, `output` contient uniquement les adresses valides, uniques, en minuscules
et triées, séparées par un point-virgule et un saut de ligne. **Copier les adresses**
permet de les coller dans la gestion des listes blanches. La limite existante
est de 1000 adresses par import ; découper les gros résultats. Aucun import automatique.

Le suivi affiche la progression, permet l’arrêt et conserve le résultat dans
l’historique privé selon les droits et la durée de conservation habituels.
Une erreur de dossier ou une annulation n’affiche pas de résultat partiel comme
une extraction complète. Les traces Docker indiquent le lancement et la fin
sans recopier les adresses extraites ni les mots de passe.
