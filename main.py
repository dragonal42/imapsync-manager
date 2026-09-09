import os
import json
import asyncio
import subprocess
import requests
from urllib.parse import urlencode
from datetime import datetime
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

app = FastAPI()
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

CONFIG_FILE = "data/config.json"
DAILY_REPORT = "data/daily_errors.log"
manual_processes = {}

OAUTH_CONFIG = {
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scope": "https://mail.google.com/"
    },
    "microsoft": {
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
    }
}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        default = {
            "poll_interval": 5, "report_email": "", "accounts": [],
            "oauth_apps": {"google_client_id": "", "google_client_secret": "", "ms_client_id": "", "ms_client_secret": ""}
        }
        save_config(default)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(config):
    os.makedirs("data", exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

def log_error(label, message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(DAILY_REPORT, "a") as f:
        f.write(f"[{timestamp}] [{label}] {message}\n")

# --- OAUTH2 ENDPOINTS ---

@app.post("/oauth/settings")
async def save_oauth_settings(
    google_client_id: str = Form(""), google_client_secret: str = Form(""),
    ms_client_id: str = Form(""), ms_client_secret: str = Form("")
):
    config = load_config()
    if "oauth_apps" not in config: config["oauth_apps"] = {}
    config["oauth_apps"].update({
        "google_client_id": google_client_id, "google_client_secret": google_client_secret,
        "ms_client_id": ms_client_id, "ms_client_secret": ms_client_secret
    })
    save_config(config)
    return RedirectResponse(url="/", status_code=303)

@app.get("/oauth/login/{provider}")
async def oauth_login(request: Request, provider: str, target_field: str):
    config = load_config().get("oauth_apps", {})
    client_id = config.get("google_client_id") if provider == "google" else config.get("ms_client_id")
    if not client_id:
        return HTMLResponse("Veuillez configurer le Client ID dans les paramètres globaux.")
    
    redirect_uri = str(request.base_url).rstrip("/") + "/oauth/callback"
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": OAUTH_CONFIG[provider]["scope"],
        "access_type": "offline",
        "prompt": "consent",
        "state": f"{provider}|{target_field}"
    }
    return RedirectResponse(f"{OAUTH_CONFIG[provider]['auth_url']}?{urlencode(params)}")

@app.get("/oauth/callback")
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
    
    # Injection des jetons dans la page appelante via JS window.opener
    html_content = f"""
    <html><body>
        <h3>Authentification réussie !</h3>
        <p>Fermeture automatique...</p>
        <script>
            if(window.opener) {{
                window.opener.document.getElementById('{target_field}').value = '{tokens.get("access_token", "")}';
                let refresh_field = document.getElementById('refresh_{target_field}');
                if (window.opener.document.getElementById('refresh_{target_field}')) {{
                    window.opener.document.getElementById('refresh_{target_field}').value = '{tokens.get("refresh_token", "")}';
                }}
                window.opener.document.getElementById('provider_{target_field}').value = '{provider}';
                window.close();
            }}
        </script>
    </body></html>
    """
    return HTMLResponse(html_content)

def refresh_oauth_token(provider: str, refresh_token: str):
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

# --- ROUTES PRINCIPALES ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    config = load_config()
    logs = os.path.exists(DAILY_REPORT) and open(DAILY_REPORT).read() or ""
    return templates.TemplateResponse(request=request, name="index.html", context={"config": config, "logs": logs})

@app.get("/manual", response_class=HTMLResponse)
async def manual(request: Request):
    return templates.TemplateResponse(request=request, name="manual.html", context={})

@app.post("/settings")
async def update_settings(poll_interval: int = Form(...), report_email: str = Form(...)):
    config = load_config()
    config["poll_interval"] = poll_interval
    config["report_email"] = report_email
    save_config(config)
    return RedirectResponse(url="/", status_code=303)

@app.post("/account/add")
async def add_account(
    label: str = Form(...),
    host1: str = Form(...), user1: str = Form(...), authmech1: str = Form("PLAIN"), pass1: str = Form(""), oauth2_token1: str = Form(""), refresh_oauth2_token1: str = Form(""), provider_oauth2_token1: str = Form(""),
    host2: str = Form(...), user2: str = Form(...), authmech2: str = Form("PLAIN"), pass2: str = Form(""), oauth2_token2: str = Form(""), refresh_oauth2_token2: str = Form(""), provider_oauth2_token2: str = Form("")
):
    config = load_config()
    config["accounts"].append({
        "id": int(datetime.now().timestamp()), "label": label,
        "host1": host1, "user1": user1, "authmech1": authmech1, "pass1": pass1, "refresh1": refresh_oauth2_token1, "provider1": provider_oauth2_token1,
        "host2": host2, "user2": user2, "authmech2": authmech2, "pass2": pass2, "refresh2": refresh_oauth2_token2, "provider2": provider_oauth2_token2,
        "last_run": "Jamais", "status": "En attente"
    })
    save_config(config)
    return RedirectResponse(url="/", status_code=303)

@app.post("/account/delete/{account_id}")
async def delete_account(account_id: int):
    config = load_config()
    config["accounts"] = [acc for acc in config["accounts"] if acc["id"] != account_id]
    save_config(config)
    return RedirectResponse(url="/", status_code=303)

@app.post("/cgi-bin/imapsync")
async def cgi_imapsync(request: Request):
    form_data = await request.form()
    if form_data.get("abort") == "on":
        for pid, proc in manual_processes.items():
            try: proc.terminate()
            except: pass
        manual_processes.clear()
        return HTMLResponse(content="Aborted by user.\n")

    cmd = ["imapsync"]
    for f in ["host1", "user1", "host2", "user2"]:
        if form_data.get(f): cmd.extend([f"--{f}", form_data.get(f)])
            
    if form_data.get("authmech1") == "XOAUTH2":
        cmd.extend(["--authmech1", "XOAUTH2", "--oauth2_token1", form_data.get("oauth2_token1", "")])
    elif form_data.get("password1"):
        cmd.extend(["--password1", form_data.get("password1")])

    if form_data.get("authmech2") == "XOAUTH2":
        cmd.extend(["--authmech2", "XOAUTH2", "--oauth2_token2", form_data.get("oauth2_token2", "")])
    elif form_data.get("password2"):
        cmd.extend(["--password2", form_data.get("password2")])
    
    for f in ["delete1", "delete2", "dry", "justlogin", "justfolders", "justfoldersizes"]:
        if form_data.get(f) == "on": cmd.append(f"--{f}")
    
    for f in ["subfolder1", "subfolder2", "extra"]:
        if form_data.get(f):
            if f == "extra": cmd.extend(form_data.get(f).split())
            else: cmd.extend([f"--{f}", form_data.get(f)])

    async def stream_output():
        try:
            process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            manual_processes[process.pid] = process
            while True:
                line = await process.stdout.readline()
                if not line: break
                yield line.decode('utf-8', errors='replace')
        except Exception as e: yield f"Error: {str(e)}\n"
        finally:
            if 'process' in locals() and process.pid in manual_processes: del manual_processes[process.pid]
    return StreamingResponse(stream_output(), media_type="text/plain")

async def sync_loop():
    while True:
        config = load_config()
        interval = config.get("poll_interval", 5) * 60
        
        for acc in config.get("accounts", []):
            label = acc["label"]
            cmd = [
                "imapsync", "--host1", acc["host1"], "--user1", acc["user1"], "--host2", acc["host2"], "--user2", acc["user2"], "--delete1", "--ssl1", "--ssl2"
            ]
            
            # --- Auto-refresh token avant lancement ---
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
                process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdout, stderr = await process.communicate()
                acc["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                acc["status"] = "Succès" if process.returncode == 0 else "Erreur"
                if process.returncode != 0: log_error(label, (stderr.decode() if stderr else stdout.decode())[:300])
            except Exception as e:
                acc["status"], _ = "Erreur", log_error(label, str(e))
            save_config(config)
            
        await asyncio.sleep(interval)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(sync_loop())
