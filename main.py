import os
import json
import asyncio
import subprocess
import requests
import auth
from urllib.parse import urlencode
from datetime import datetime
from fastapi import FastAPI, Request, Form, Depends, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

# Initialisation de l'application FastAPI
app = FastAPI()

# Création du dossier "static" s'il n'existe pas et montage pour servir CSS/JS
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Configuration du moteur de templates Jinja2
templates = Jinja2Templates(directory="templates")

# Fichiers de persistance des données
CONFIG_FILE = "data/config.json"
DAILY_REPORT = "data/daily_errors.log"

# Dictionnaire pour garder la trace des processus manuels en cours d'exécution
# Cela permet de les "tuer" (kill) si l'utilisateur clique sur le bouton "Stop!"
manual_processes = {}

# --- CONFIGURATION OAUTH2 ---
# Définition des endpoints officiels pour Google et Microsoft
OAUTH_CONFIG = {
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scope": "https://mail.google.com/" # Permission complète sur la boîte mail
    },
    "microsoft": {
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access" # IMAP + accès hors ligne (pour le refresh token)
    }
}

# --- FONCTIONS UTILITAIRES ---

def load_config():
    """Charge la configuration depuis le fichier JSON. Initialise les valeurs par défaut si inexistant."""
    if not os.path.exists(CONFIG_FILE):
        default = {
            "poll_interval": 5, 
            "report_email": "", 
            "accounts": [],
            "oauth_apps": {
                "google_client_id": "", "google_client_secret": "", 
                "ms_client_id": "", "ms_client_secret": ""
            }
        }
        save_config(default)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(config):
    """Sauvegarde l'état actuel de la configuration sur le disque."""
    os.makedirs("data", exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

def log_error(label, message):
    """Ajoute une ligne d'erreur dans le fichier de log quotidien (qui sera envoyé par email à 23h59)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(DAILY_REPORT, "a") as f:
        f.write(f"[{timestamp}] [{label}] {message}\n")


# --- MECANISME OAUTH2 ---

@app.post("/oauth/settings", dependencies=[Depends(auth.require_auth)])
async def save_oauth_settings(
    google_client_id: str = Form(""), google_client_secret: str = Form(""),
    ms_client_id: str = Form(""), ms_client_secret: str = Form("")
):
    """Enregistre les clés d'API (Client ID et Secret) configurées par l'administrateur depuis l'interface."""
    config = load_config()
    if "oauth_apps" not in config: 
        config["oauth_apps"] = {}
    config["oauth_apps"].update({
        "google_client_id": google_client_id, "google_client_secret": google_client_secret,
        "ms_client_id": ms_client_id, "ms_client_secret": ms_client_secret
    })
    save_config(config)
    return RedirectResponse(url="/", status_code=303)

@app.get("/oauth/login/{provider}", dependencies=[Depends(auth.require_auth)])
async def oauth_login(request: Request, provider: str, target_field: str):
    """
    Étape 1 d'OAuth2 : Redirection vers le portail de connexion de Google/Microsoft.
    target_field permet de se souvenir pour quel champ (ex: oauth2_token1 ou oauth2_token2)
    l'utilisateur est en train de s'authentifier.
    """
    config = load_config().get("oauth_apps", {})
    client_id = config.get("google_client_id") if provider == "google" else config.get("ms_client_id")
    
    if not client_id:
        return HTMLResponse("Erreur : Veuillez d'abord configurer le Client ID dans les paramètres globaux.")
    
    redirect_uri = str(request.base_url).rstrip("/") + "/oauth/callback"
    
    params = {
        "client_id": client_id,
        "response_type": "code", # On demande un "code" d'autorisation temporaire
        "redirect_uri": redirect_uri,
        "scope": OAUTH_CONFIG[provider]["scope"],
        "access_type": "offline", # Indispensable chez Google pour forcer l'envoi d'un refresh_token
        "prompt": "consent",      # Force l'écran de consentement pour être sûr d'avoir le refresh_token
        "state": f"{provider}|{target_field}" # On fait transiter le fournisseur et la cible via le paramètre d'état
    }
    # Redirige le navigateur du client vers l'URL officielle
    return RedirectResponse(f"{OAUTH_CONFIG[provider]['auth_url']}?{urlencode(params)}")

@app.get("/oauth/callback", dependencies=[Depends(auth.require_auth)])
async def oauth_callback(request: Request, code: str, state: str):
    provider, target_field = state.split("|")
    config = load_config().get("oauth_apps", {})
    
    client_id = config.get("google_client_id") if provider == "google" else config.get("ms_client_id")
    client_secret = config.get("google_client_secret") if provider == "google" else config.get("ms_client_secret")
    redirect_uri = str(request.base_url).rstrip("/") + "/oauth/callback"

    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri
    }

    r = requests.post(OAUTH_CONFIG[provider]["token_url"], data=data)
    tokens = r.json()
    
    # Sérialisation sécurisée des tokens via json.dumps() pour éviter toute rupture JS ou XSS
    access_token_js = json.dumps(tokens.get("access_token", ""))
    refresh_token_js = json.dumps(tokens.get("refresh_token", ""))
    provider_js = json.dumps(provider)

    html_content = f"""
    <html><body>
        <h3>Authentification réussie !</h3>
        <p>Fermeture automatique...</p>
        <script>
            if(window.opener) {{
                // Utilisation directe de la variable JSON injectée sans guillemets superflus
                window.opener.document.getElementById('{target_field}').value = {access_token_js};
                
                let refElem = window.opener.document.getElementById('refresh_{target_field}');
                if (refElem) {{
                    refElem.value = {refresh_token_js};
                }}
                
                let provElem = window.opener.document.getElementById('provider_{target_field}');
                if (provElem) {{
                    provElem.value = {provider_js};
                }}
                
                window.close();
            }}
        </script>
    </body></html>
    """
    return HTMLResponse(html_content)

def refresh_oauth_token(provider: str, refresh_token: str):
    """
    Utilise le jeton de rafraîchissement permanent pour obtenir un nouveau jeton d'accès temporaire (valable 1h).
    Appelé automatiquement par la boucle de synchronisation avant de lancer imapsync.
    """
    config = load_config().get("oauth_apps", {})
    client_id = config.get("google_client_id") if provider == "google" else config.get("ms_client_id")
    client_secret = config.get("google_client_secret") if provider == "google" else config.get("ms_client_secret")
    
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }
    r = requests.post(OAUTH_CONFIG[provider]["token_url"], data=data)
    return r.json().get("access_token", "")


# --- INTERFACES UTILISATEUR ---
@app.get("/health")
async def health():
    return {"status": "ok"}
    
@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth.require_auth)])
async def index(request: Request):
    """Affiche le tableau de bord principal (Mode Automatique)."""
    config = load_config()
    logs = os.path.exists(DAILY_REPORT) and open(DAILY_REPORT).read() or ""
    # On passe explicitement request=request (obligatoire sur FastAPI récent)
    return templates.TemplateResponse(request=request, name="index.html", context={"config": config, "logs": logs})

@app.get("/manual", response_class=HTMLResponse, dependencies=[Depends(auth.require_auth)])
async def manual(request: Request):
    """Affiche l'interface de synchronisation manuelle."""
    return templates.TemplateResponse(request=request, name="manual.html", context={})

@app.post("/settings", dependencies=[Depends(auth.require_auth)])
async def update_settings(poll_interval: int = Form(...), report_email: str = Form(...)):
    """Met à jour les paramètres globaux (fréquence de synchro, email)."""
    config = load_config()
    config["poll_interval"] = poll_interval
    config["report_email"] = report_email
    save_config(config)
    return RedirectResponse(url="/", status_code=303)

@app.post("/account/add", dependencies=[Depends(auth.require_auth)])
async def add_account(
    label: str = Form(...),
    host1: str = Form(...), user1: str = Form(...), authmech1: str = Form("PLAIN"), pass1: str = Form(""), oauth2_token1: str = Form(""), refresh_oauth2_token1: str = Form(""), provider_oauth2_token1: str = Form(""),
    host2: str = Form(...), user2: str = Form(...), authmech2: str = Form("PLAIN"), pass2: str = Form(""), oauth2_token2: str = Form(""), refresh_oauth2_token2: str = Form(""), provider_oauth2_token2: str = Form("")
):
    """Ajoute une nouvelle paire de comptes (Source -> Destination) à la boucle automatique."""
    config = load_config()
    config["accounts"].append({
        "id": int(datetime.now().timestamp()), "label": label,
        "host1": host1, "user1": user1, "authmech1": authmech1, "pass1": pass1, "refresh1": refresh_oauth2_token1, "provider1": provider_oauth2_token1,
        "host2": host2, "user2": user2, "authmech2": authmech2, "pass2": pass2, "refresh2": refresh_oauth2_token2, "provider2": provider_oauth2_token2,
        "last_run": "Jamais", "status": "En attente"
    })
    save_config(config)
    return RedirectResponse(url="/", status_code=303)

@app.post("/account/delete/{account_id}", dependencies=[Depends(auth.require_auth)])
async def delete_account(account_id: int):
    """Supprime une paire de comptes de la configuration."""
    config = load_config()
    config["accounts"] = [acc for acc in config["accounts"] if acc["id"] != account_id]
    save_config(config)
    return RedirectResponse(url="/", status_code=303)

@app.exception_handler(auth.AuthRedirect)
async def _auth_redirect(request: Request, exc: auth.AuthRedirect):
    return RedirectResponse(url="/login")

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, sent: str = "", error: str = ""):
    return templates.TemplateResponse(request=request, name="login.html",
        context={"sent": sent, "error": error})

@app.post("/login/request")
async def login_request():
    email = load_config().get("report_email", "")
    if not email:
        return RedirectResponse(url="/login?error=noemail", status_code=303)
    auth.generate_and_send_code(email)
    return RedirectResponse(url="/login?sent=1", status_code=303)

@app.post("/login/verify")
async def login_verify(code: str = Form(...)):
    email = load_config().get("report_email", "")
    if not auth.verify_code(email, code):
        return RedirectResponse(url="/login?error=1", status_code=303)
    token = auth.create_session()
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(auth.SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=auth.SESSION_TTL)
    return resp

@app.get("/logout")
async def logout(imapsync_session: str = Cookie(default=None)):
    if imapsync_session:
        auth.destroy_session(imapsync_session)
    resp = RedirectResponse(url="/login")
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp

# --- MOTEUR DE SYNCHRONISATION ---
@app.post("/cgi-bin/imapsync", dependencies=[Depends(auth.require_auth)])
async def cgi_imapsync(request: Request):
    """
    Simule l'ancien script CGI Perl d'imapsync online.
    Cette route est appelée par le formulaire manuel via AJAX.
    Elle génère la commande CLI en fonction des cases cochées et renvoie le flux texte en temps réel.
    """
    form_data = await request.form()
    
    # Gestion du bouton d'annulation (Stop!)
    if form_data.get("abort") == "on":
        for pid, proc in manual_processes.items():
            try: proc.terminate() # Tue le processus système sous-jacent
            except: pass
        manual_processes.clear()
        return HTMLResponse(content="Synchronisation annulée par l'utilisateur.\n")

    # Construction dynamique de la commande imapsync
    cmd = ["imapsync"]
    for f in ["host1", "user1", "host2", "user2"]:
        if form_data.get(f): cmd.extend([f"--{f}", form_data.get(f)])
            
    # Authentification Source
    if form_data.get("authmech1") == "XOAUTH2":
        cmd.extend(["--authmech1", "XOAUTH2", "--oauth2_token1", form_data.get("oauth2_token1", "")])
    elif form_data.get("password1"):
        cmd.extend(["--password1", form_data.get("password1")])

    # Authentification Destination
    if form_data.get("authmech2") == "XOAUTH2":
        cmd.extend(["--authmech2", "XOAUTH2", "--oauth2_token2", form_data.get("oauth2_token2", "")])
    elif form_data.get("password2"):
        cmd.extend(["--password2", form_data.get("password2")])
    
    # Options (Flags)
    for f in ["delete1", "delete2", "dry", "justlogin", "justfolders", "justfoldersizes"]:
        if form_data.get(f) == "on": cmd.append(f"--{f}")
    
    # Dossiers cibles et extra paramètres textuels
    for f in ["subfolder1", "subfolder2", "extra"]:
        if form_data.get(f):
            if f == "extra": cmd.extend(form_data.get(f).split()) # Divise la chaîne texte en arguments
            else: cmd.extend([f"--{f}", form_data.get(f)])

    async def stream_output():
        """Exécute imapsync et diffuse (yield) chaque ligne de la console (stdout) au navigateur."""
        try:
            # Lancement asynchrone pour ne pas bloquer le serveur FastAPI
            process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            manual_processes[process.pid] = process
            
            while True:
                line = await process.stdout.readline()
                if not line: break
                yield line.decode('utf-8', errors='replace')
        except Exception as e: 
            yield f"Erreur de lancement : {str(e)}\n"
        finally:
            if 'process' in locals() and process.pid in manual_processes: 
                del manual_processes[process.pid]
                
    # Renvoie une réponse HTTP en streaming (chunked)
    return StreamingResponse(stream_output(), media_type="text/plain")

async def sync_loop():
    """
    Tâche de fond (Background Task) qui tourne en boucle infinie.
    Elle parcourt tous les comptes configurés, met à jour les jetons OAuth si nécessaire,
    et lance les synchronisations silencieusement.
    """
    while True:
        config = load_config()
        interval = config.get("poll_interval", 5) * 60
        
        for acc in config.get("accounts", []):
            label = acc["label"]
            cmd = [
                "imapsync", "--host1", acc["host1"], "--user1", acc["user1"], 
                "--host2", acc["host2"], "--user2", acc["user2"], 
                "--delete1", "--ssl1", "--ssl2"
            ]
            
            # --- Auto-refresh du token avant lancement ---
            # Si le compte utilise XOAUTH2, on demande un jeton tout neuf valable 1 heure
            # pour éviter les échecs d'expiration en cours de transfert long.
            if acc.get("authmech1") == "XOAUTH2" and acc.get("refresh1"):
                new_token = refresh_oauth_token(acc["provider1"], acc["refresh1"])
                cmd.extend(["--authmech1", "XOAUTH2", "--oauth2_token1", new_token])
            else:
                cmd.extend(["--password1", acc.get("pass1", "")])
                
            if acc.get("authmech2") == "XOAUTH2" and acc.get("refresh2"):
                new_token = refresh_oauth_token(acc["provider2"], acc["refresh2"])
                cmd.extend(["--authmech2", "XOAUTH2", "--oauth2_token2", new_token])
            else:
                cmd.extend(["--password2", acc.get("pass2", "")])
            
            acc["status"] = "Synchronisation..."
            save_config(config)
            
            try:
                # Exécution bloquante au niveau de la boucle (mais asynchrone pour FastAPI)
                process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdout, stderr = await process.communicate()
                
                acc["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                acc["status"] = "Succès" if process.returncode == 0 else "Erreur"
                
                # Si erreur (Code de retour != 0), on log les 300 premiers caractères de la sortie d'erreur
                if process.returncode != 0: 
                    log_error(label, (stderr.decode() if stderr else stdout.decode())[:300])
            except Exception as e:
                acc["status"] = "Erreur"
                log_error(label, str(e))
            
            save_config(config)
            
        # Pause avant le prochain cycle (convertie en secondes)
        await asyncio.sleep(interval)

@app.on_event("startup")
async def startup_event():
    """Démarre la boucle de synchronisation dès que le serveur Uvicorn est prêt."""
    asyncio.create_task(sync_loop())

