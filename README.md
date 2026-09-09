# 🔄 IMAPSync Manager (Auto + Manuel)

Interface web autonome (Docker, FastAPI, imapsync) avec deux modes de fonctionnement :
1. **Automatique** : Scrute à intervalle régulier, synchronise et vide la source (optionnel), et envoie un rapport quotidien d'erreur.
2. **Manuel** : Interface graphique interactive (basée sur l'outil standard imapsync online) pour piloter visuellement et ponctuellement vos transferts complexes (Dry run, justlogin, etc.).

## 🚀 Installation 

```bash
git clone <votre-depot>
cd imapsync-manager
cp .env.example .env
# Editez .env avec vos informations SMTP pour les alertes
docker compose up -d --build
```

L'application est disponible sur **`http://localhost:8080`**.
