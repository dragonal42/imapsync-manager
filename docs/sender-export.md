# Extraire les expéditeurs de la source

Dans `/manual`, renseigner les identifiants de la **Source** (mot de passe ou OAuth2),
puis cliquer sur **Extraire les expéditeurs**. La destination et les paramètres
d’analyse antispam ne sont pas utilisés.

La case **Exclure les expéditeurs déjà dans ma liste blanche** est cochée par défaut.
Le filtre utilise la liste blanche de l’utilisateur connecté, même pour un administrateur.
Décocher pour récupérer toutes les adresses. La liste noire n’est pas un filtre d’export.

L’extraction parcourt tous les dossiers sélectionnables, y compris les dossiers envoyés,
spam et quarantaine, et tous les messages, lus ou non, sans limite de date. Elle ouvre
les dossiers en lecture seule et récupère seulement les en-têtes `From` par lots de
100 messages. Elle ne marque aucun message comme lu et ne lance ni synchronisation
ni analyse IA/Rspamd. Les adresses invalides ne sont pas exportées.

Le **Suivi d’exécution** affiche la progression. À la fin, `output` contient uniquement
les adresses uniques, normalisées en minuscules, triées et séparées par un point-virgule
et un saut de ligne. Le bouton **Copier les adresses** copie ce résultat ; si le navigateur
refuse l’accès au presse-papiers, le texte est sélectionné pour une copie manuelle.

Coller les adresses dans **Mes listes d’expéditeurs**, choisir WhiteList puis confirmer
l’import. La limite existante est de 1000 adresses par import : découper les gros exports.
L’extraction n’importe aucune adresse automatiquement. Un expéditeur présent dans la
boîte n’est pas nécessairement un expéditeur auquel accorder une exemption de contrôle.

Le bouton Arrêter interrompt l’extraction entre les commandes IMAP. En cas d’erreur ou
d’annulation, aucun résultat partiel n’est présenté comme une liste complète. L’historique
est accessible selon les mêmes droits de propriétaire/administrateur que les autres
exécutions, et suit la durée de conservation des journaux. Les identifiants IMAP ne sont
pas enregistrés comme configuration. Les traces Docker indiquent le lancement et la fin
avec le nombre d’adresses, sans y recopier la liste complète.
