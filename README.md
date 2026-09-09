# 🔄 IMAPSync Manager

**IMAPSync Manager** est une interface web autonome basée sur **Docker**, **FastAPI** et **imapsync** permettant d’automatiser et de piloter facilement la synchronisation, la migration et le nettoyage de comptes IMAP.

---

## ✨ Fonctionnalités

### 🤖 Mode automatique

- Scrute les comptes à intervalle régulier.
- Synchronise automatiquement les boîtes mail.
- Peut vider la boîte source après synchronisation si cette option est activée.
- Génère un rapport quotidien des erreurs.
- Envoie automatiquement ce rapport par e-mail à **23h59**.

### 🖥️ Mode manuel

- Interface graphique interactive.
- Utilisation d’**imapsync** directement depuis l’interface web.
- Barres de progression en temps réel.
- Affichage dynamique des logs.
- Bouton d’annulation d’urgence pour interrompre une synchronisation en cours.

### 🔐 OAuth2 / XOAUTH2

Prise en charge de l’authentification moderne pour :

- **Gmail**
- **Microsoft Office 365**

L’interface intègre :

- une fenêtre d’authentification OAuth2 ;
- la récupération du `refresh_token` ;
- le renouvellement automatique des jetons d’accès.

### 💾 Persistance des données

Les configurations et rapports sont stockés sur l’hôte afin de rester disponibles après :

- un redémarrage du conteneur ;
- une recréation de la stack Docker ;
- une mise à jour de l’application.

---

## 🧰 Prérequis

Avant l’installation, assurez-vous de disposer de :

- Docker ;
- Docker Compose ou un gestionnaire compatible comme **Dockhand** ;
- un accès aux serveurs IMAP à synchroniser ;
- éventuellement un serveur SMTP pour l’envoi des rapports.

---

## 🚀 Installation

### 1. Cloner le dépôt

```bash
git clone https://github.com/dragonal42/imapsync-manager.git
cd imapsync-manager
```

### 2. Préparer le dossier de persistance

Créez le dossier utilisé pour conserver les données de l’application :

```bash
sudo mkdir -p /opt/imapsyncmanager
sudo chmod -R 777 /opt/imapsyncmanager
```

> [!NOTE]
> Le dossier `/opt/imapsyncmanager` est monté dans le conteneur sous `/app/data`.

### 3. Configurer les variables d’environnement

Créez un fichier `.env` à la racine du projet.

Exemple :

```env
SMTP_HOST=smtp.exemple.com
SMTP_PORT=587
SMTP_FROM=imapsync@exemple.com
SMTP_USER=mon_utilisateur_smtp
SMTP_PASS=mon_mot_de_passe_secret
TZ=Europe/Paris
```

> [!IMPORTANT]
> Ne versionnez jamais votre fichier `.env` s’il contient des identifiants ou mots de passe réels.

---

## 🐳 Docker Compose

Exemple de configuration :

```yaml
services:
  imapsync-manager:
    build: .
    container_name: imapsync-manager
    ports:
      - "8080:8080"
    environment:
      - TZ=Europe/Paris
      - SMTP_HOST=${SMTP_HOST}
      - SMTP_PORT=${SMTP_PORT:-587}
      - SMTP_FROM=${SMTP_FROM}
      - SMTP_USER=${SMTP_USER}
      - SMTP_PASS=${SMTP_PASS}
    volumes:
      - /opt/imapsyncmanager:/app/data
    restart: unless-stopped
```

Lancez ensuite la stack avec votre gestionnaire Docker habituel ou en ligne de commande :

```bash
docker compose up -d
```

---

## 🌐 Accès à l’interface

Une fois le conteneur démarré, ouvrez :

```text
http://<ip-du-serveur>:8080
```

Exemple :

```text
http://192.168.1.10:8080
```

---

## 📂 Persistance

Les données persistantes sont stockées dans :

```text
/opt/imapsyncmanager
```

et montées dans le conteneur sous :

```text
/app/data
```

Cela permet de conserver les configurations et rapports indépendamment du cycle de vie du conteneur Docker.

---

## 🔧 Mise à jour

Pour récupérer la dernière version du projet :

```bash
git pull
docker compose build
docker compose up -d
```

---

## 📜 Logs

Pour consulter les logs du conteneur :

```bash
docker compose logs -f imapsync-manager
```

---

## 🙏 Remerciements

Un immense merci à **Gilles LAMIRAL** pour la création et la maintenance de **imapsync**.

Son travail, ainsi que la licence ouverte d’imapsync, rendent possible la création de ce gestionnaire web.

Pour en savoir plus sur imapsync :

https://imapsync.lamiral.info/

---

## ❤️ Projet

Si ce projet vous est utile, n’hésitez pas à lui laisser une ⭐ sur GitHub.
