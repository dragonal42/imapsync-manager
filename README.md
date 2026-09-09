# 🔄 IMAPSync ManagerInterface web autonome basée sur Docker, FastAPI et imapsync pour automatiser et piloter la synchronisation, la migration et le nettoyage de comptes IMAP en toute simplicité.  🚀 Fonctionnalités principalesMode Automatique : Scrute à intervalle régulier, synchronise les boîtes mail, vide la source si demandé et envoie un rapport quotidien d'erreurs par e-mail à 23h59.Mode Manuel : Interface graphique interactive reprenant l'outil standard avec barres de progression en temps réel, logs dynamiques et bouton d'annulation d'urgence.Support OAuth2 (XOAUTH2) : Intégration complète pour Gmail et Office 365 avec un système de pop-up d'authentification et un renouvellement automatique du refresh_token.Persistance robuste : Stockage des configurations et des rapports sur l'hôte pour survivre aux mises à jour de conteneurs.🛠️ Installation & Déploiement (Dockhand / Docker Compose)Clonez ce dépôt sur votre serveur :Bashgit clone https://github.com/dragonal42/imapsync-manager.git
cd imapsync-manager
Préparez le dossier de persistance sur l'hôte :Bashsudo mkdir -p /opt/imapsyncmanager
sudo chmod -R 777 /opt/imapsyncmanager
Créez un fichier .env basé sur l'exemple pour configurer l'envoi des e-mails SMTP :Extrait de codeSMTP_HOST=smtp.exemple.com
SMTP_PORT=587
SMTP_FROM=imapsync@exemple.com
SMTP_USER=mon_utilisateur_smtp
SMTP_PASS=mon_mot_de_passe_secret
TZ=Europe/Paris
Lancez la stack via votre gestionnaire (comme Dockhand ou en ligne de commande) :YAMLservices:
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
Accédez ensuite à l'application sur http://<ip-du-serveur>:8080.🙏 RemerciementsUn immense merci à Gilles LAMIRAL pour la création et la maintenance de l'outil universel imapsync, dont le code et la licence ouverte rendent ce gestionnaire possible.  
