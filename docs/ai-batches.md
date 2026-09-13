# Analyse IA par lots

Dans `/admin`, chaque moteur possède son champ **Emails maximum par lot** :
`mistral_batch_size` et `gemini_batch_size`, chacun à **5** par défaut, entre **1 et 50**.
Ces valeurs globales s’appliquent à toutes les configurations et aux analyses manuelles.
Elles sont lues au démarrage de chaque exécution. Un réglage à 1 conserve le protocole
historique avec un seul verdict par appel.

Les emails sont regroupés par configuration et dossier, sans mélange de locataires.
Les filtres de période, de lecture et de suivi restent appliqués, ainsi que les listes
blanche et noire qui ne consomment aucun appel IA. Le dernier lot peut être incomplet.
La limite de 64 Kio de métadonnées et le budget TPM peuvent réduire un lot davantage.
Un message qui dépasse seul le budget TPM arrête le contrôle IA avec un avertissement.
Les quotas et délais après HTTP 429 restent partagés entre les tâches.

Exemple de contenu envoyé dans un appel Mistral ou Gemini (hors enveloppe propre à l’API) :

```json
{
  "emails": [
    {"id": "mail-001", "headers": {"from": ["facturation@example.org"]}, "subject": "Facture", "urls": []},
    {"id": "mail-002", "headers": {"from": ["inconnu@example.net"]}, "subject": "Votre gain", "urls": ["https://example.net/gain"]}
  ]
}
```

Réponse attendue, dont l’ordre est libre :

```json
{"results": [{"id": "mail-002", "verdict": "scam"}, {"id": "mail-001", "verdict": "legitimate"}]}
```

Les identifiants sont associés localement aux UID IMAP, au dossier et à sa UIDVALIDITY.
Tous les résultats doivent être valides avant d’appliquer le premier : aucun identifiant
absent, ajouté ou dupliqué ; verdict parmi `spam`, `scam`, `legitimate`, `uncertain`.
Une réponse tronquée ou invalide ne déplace ni ne marque comme traité aucun email du lot.
Les erreurs IMAP pendant l’application peuvent arrêter un lot partiellement appliqué ;
le suivi persistant et les vérifications de copie/date protègent les originaux.

Les logs affichent le moteur, le modèle, les UID du lot et le verdict de chaque email.
La consommation retournée par le fournisseur est cumulée **une fois par appel**, y compris
une réponse invalide si son usage est disponible. Les compteurs d’emails restent individuels.
Un lot réduit le nombre d’appels et mutualise les instructions ; il ne contourne pas les
quotas de tokens. Un échec IA continue de laisser s’effectuer le transfert IMAP configuré.
