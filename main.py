"""Single-process, JSON-backed multi-tenant IMAPSync service."""
import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import smtplib
import ssl
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

CONFIG_FILE = Path("data/config.json")
AUTH_FILE = Path("data/auth.json")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
COOKIE = "imapsync_session"
MAGIC_TTL = 900
SESSION_TTL = 43200
processes = {}
tasks = set()
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger(__name__)


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else copy.deepcopy(default)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_config():
    config = read_json(CONFIG_FILE, {"accounts": [], "poll_interval": 5,
                                     "report_email": "", "oauth_apps": {}})
    before = copy.deepcopy(config)
    config.setdefault("users", [])
    config.setdefault("runs", [])
    if ADMIN_EMAIL:
        admin = next((u for u in config["users"] if u["email"] == ADMIN_EMAIL), None)
        if admin is None:
            config["users"].append({"email": ADMIN_EMAIL, "pseudo": "Administrateur", "role": "admin"})
        else:
            admin["role"] = "admin"
        for account in config["accounts"]:
            account.setdefault("owner", ADMIN_EMAIL)
    if config != before or not CONFIG_FILE.exists():
        if CONFIG_FILE.exists() and "users" not in before:
            backup = CONFIG_FILE.with_suffix(".pre-saas.json")
            if not backup.exists():
                shutil.copy2(CONFIG_FILE, backup)
                os.chmod(backup, 0o600)
        save_config(config)
    return config


def save_config(config):
    # All read/modify/write sections run without await on the single event loop.
    write_json(CONFIG_FILE, config)


def auth_data():
    data = read_json(AUTH_FILE, {"links": {}, "sessions": {}, "oauth": {}, "limits": {}})
    now = time.time()
    for section in data:
        data[section] = {k: v for k, v in data[section].items() if v["expires"] > now}
    return data


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def user_for(request):
    session = auth_data()["sessions"].get(digest(request.cookies.get(COOKIE, "")))
    if session:
        return next((u for u in load_config()["users"] if u["email"] == session["email"]), None)


def require_admin(request):
    if request.state.user["role"] != "admin":
        raise HTTPException(403, "Accès administrateur requis")


def visible(user, item):
    return user["role"] == "admin" or item.get("owner") == user["email"]


def account_for(request, config, account_id):
    account = next((a for a in config["accounts"] if str(a["id"]) == account_id), None)
    if account is None or not visible(request.state.user, account):
        raise HTTPException(404, "Configuration introuvable")
    return account


def render(request, name, **context):
    return templates.TemplateResponse(request=request, name=name, context={
        "user": getattr(request.state, "user", None), "public_url": PUBLIC_URL, **context})


def spawn(coro):
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


@asynccontextmanager
async def lifespan(app):
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", ADMIN_EMAIL):
        raise RuntimeError("ADMIN_EMAIL doit être défini")
    parsed = urlsplit(PUBLIC_URL)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise RuntimeError("PUBLIC_URL doit être une origine HTTPS, sans chemin")
    config = load_config()
    for run in config["runs"]:
        if run["status"] == "Synchronisation...":
            run.update(status="Interrompue", log="Le service a redémarré pendant cette exécution.")
    for account in config["accounts"]:
        if account.get("status") == "Synchronisation...":
            account["status"] = "Interrompue"
    save_config(config)
    spawn(sync_loop())
    yield
    for task in list(tasks):
        task.cancel()
    await asyncio.gather(*list(tasks), return_exceptions=True)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def access_control(request, call_next):
    request.state.user = user_for(request)
    path = request.url.path
    public = path in {"/", "/login", "/auth/verify", "/cgu", "/privacy", "/health", "/favicon.ico"} or path.startswith("/static/")
    if not public and not request.state.user:
        return RedirectResponse("/login", status_code=303)
    # Same-origin verification protects every mutation, including login/logout.
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        referer = request.headers.get("referer", "")
        referer_origin = "{0.scheme}://{0.netloc}".format(urlsplit(referer)) if referer else ""
        if (origin or referer_origin) != PUBLIC_URL:
            return HTMLResponse("Origine de la requête refusée", status_code=403)
    response = await call_next(request)
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    if not path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/favicon.ico")
async def favicon():
    return RedirectResponse("/static/Logo_ImapSyncManager_32p.png")


@app.get("/")
async def home(request: Request):
    return render(request, "home.html")


@app.get("/cgu")
async def terms(request: Request):
    return render(request, "cgu.html")


@app.get("/privacy")
async def privacy(request: Request):
    return render(request, "privacy.html")


@app.get("/login")
async def login(request: Request):
    return render(request, "login.html")


def send_magic_link(email, token):
    message = EmailMessage()
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = email
    message["Subject"] = "Votre lien de connexion IMAPSync Manager"
    message.set_content(f"Connectez-vous à IMAPSync Manager :\n{PUBLIC_URL}/auth/verify?token={token}\n\n"
                        "Ce lien personnel expire dans 15 minutes et ne peut être utilisé qu'une fois.\n"
                        "Si vous n'avez pas demandé ce lien, ignorez ce message.")
    mode = os.getenv("SMTP_SECURITY", "starttls")
    if mode not in {"starttls", "ssl"}:
        raise ValueError("SMTP_SECURITY doit être starttls ou ssl")
    factory = smtplib.SMTP_SSL if mode == "ssl" else smtplib.SMTP
    kwargs = {"timeout": 15}
    if mode == "ssl":
        kwargs["context"] = ssl.create_default_context()
    with factory(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587")), **kwargs) as smtp:
        if mode == "starttls":
            smtp.starttls(context=ssl.create_default_context())
        if os.getenv("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        smtp.send_message(message)


async def deliver_link(email, token):
    try:
        await asyncio.to_thread(send_magic_link, email, token)
    except Exception:
        data = auth_data()
        data["links"].pop(digest(token), None)
        write_json(AUTH_FILE, data)
        logger.error("Échec de livraison SMTP du lien magique")


@app.post("/login")
async def request_link(request: Request):
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    data = auth_data()
    key = digest(email)
    ip_key = "ip:" + digest(request.client.host if request.client else "unknown")
    limited = False
    for limit_key, maximum in ((key, 3), (ip_key, 30)):
        limit = data["limits"].setdefault(limit_key, {"count": 0, "expires": time.time() + 900})
        limit["count"] += 1
        limited |= limit["count"] > maximum
    known = any(u["email"] == email for u in load_config()["users"])
    if known and not limited:
        token = secrets.token_urlsafe(32)
        data["links"][digest(token)] = {"email": email, "expires": time.time() + MAGIC_TTL}
    write_json(AUTH_FILE, data)
    if known and not limited:
        spawn(deliver_link(email, token))
    return render(request, "login.html", sent=True)


@app.get("/auth/verify")
async def verify(request: Request, token: str = ""):
    # GET does not consume the link: email scanners must not sign users in.
    return render(request, "verify.html", token=token)


@app.post("/auth/verify")
async def consume_link(request: Request):
    form = await request.form()
    data = auth_data()
    link = data["links"].pop(digest(str(form.get("token", ""))), None)
    user = next((u for u in load_config()["users"] if link and u["email"] == link["email"]), None)
    if not user:
        return render(request, "login.html", error="Lien invalide, expiré ou déjà utilisé. Demandez un nouveau lien.")
    token = secrets.token_urlsafe(32)
    data["sessions"].pop(digest(request.cookies.get(COOKIE, "")), None)
    data["sessions"][digest(token)] = {"email": user["email"], "expires": time.time() + SESSION_TTL}
    write_json(AUTH_FILE, data)
    response = RedirectResponse("/admin" if user["role"] == "admin" else "/dashboard", status_code=303)
    response.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True, secure=True, samesite="lax", path="/")
    return response


@app.post("/logout")
async def logout(request: Request):
    data = auth_data()
    data["sessions"].pop(digest(request.cookies.get(COOKIE, "")), None)
    write_json(AUTH_FILE, data)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="lax")
    return response


@app.get("/admin")
@app.get("/dashboard")
async def dashboard(request: Request, owner: str = ""):
    if request.url.path == "/admin":
        require_admin(request)
    config = load_config()
    config["accounts"] = [a for a in config["accounts"] if visible(request.state.user, a)
                          and (request.state.user["role"] != "admin" or not owner or a["owner"] == owner)]
    config["runs"] = [r for r in config["runs"] if visible(request.state.user, r)
                      and (request.state.user["role"] != "admin" or not owner or r["owner"] == owner)]
    if request.state.user["role"] != "admin":
        config["users"] = []
        config["oauth_apps"] = {}
        config["report_email"] = ""
    return render(request, "dashboard.html", config=config, owner=owner)


@app.post("/admin/users")
async def create_user(request: Request):
    require_admin(request)
    form = await request.form()
    email, pseudo = str(form.get("email", "")).strip().lower(), str(form.get("pseudo", "")).strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or not 1 <= len(pseudo) <= 80 or any(ord(c) < 32 for c in pseudo):
        raise HTTPException(400, "Email ou pseudo invalide")
    config = load_config()
    if any(u["email"] == email for u in config["users"]):
        raise HTTPException(409, "Utilisateur déjà enregistré")
    config["users"].append({"email": email, "pseudo": pseudo, "role": "user"})
    save_config(config)
    return RedirectResponse("/admin", status_code=303)


@app.post("/settings")
@app.post("/oauth/settings")
async def settings(request: Request):
    require_admin(request)
    form = await request.form()
    config = load_config()
    if request.url.path == "/settings":
        try:
            interval = int(form.get("poll_interval", 5))
            if not 1 <= interval <= 10080:
                raise ValueError()
        except ValueError:
            raise HTTPException(400, "Intervalle invalide")
        config.update(poll_interval=interval, report_email=str(form.get("report_email", "")))
    else:
        config["oauth_apps"] = {k: str(form.get(k, "")) for k in
                                ("google_client_id", "google_client_secret", "ms_client_id", "ms_client_secret")}
    save_config(config)
    return RedirectResponse("/admin", status_code=303)


@app.get("/account/new")
@app.get("/account/edit/{account_id}")
async def account_form(request: Request, account_id: str = ""):
    config = load_config()
    account = account_for(request, config, account_id) if account_id else {}
    return render(request, "account.html", account=account,
                  users=config["users"] if request.state.user["role"] == "admin" else [])


@app.post("/account/add")
@app.post("/account/edit/{account_id}")
async def save_account(request: Request, account_id: str = ""):
    config = load_config()
    account = account_for(request, config, account_id) if account_id else {"id": secrets.token_hex(12), "last_run": "Jamais", "status": "En attente"}
    if str(account["id"]) in processes:
        raise HTTPException(409, "Attendez la fin de l'exécution avant de modifier cette configuration")
    form = await request.form()
    owner = str(form.get("owner", request.state.user["email"])) if request.state.user["role"] == "admin" else request.state.user["email"]
    if not any(u["email"] == owner for u in config["users"]):
        raise HTTPException(400, "Propriétaire inconnu")
    account["owner"] = owner
    for key in ("label", "host1", "host2", "user1", "user2"):
        value = str(form.get(key, "")).strip()
        if not value or len(value) > 255 or value.startswith("-") or any(ord(c) < 32 for c in value):
            raise HTTPException(400, "Champ invalide : " + key)
        account[key] = value
    for side in ("1", "2"):
        mech = str(form.get("authmech" + side, "PLAIN"))
        if mech not in {"PLAIN", "XOAUTH2"}:
            raise HTTPException(400, "Authentification IMAP invalide")
        account["authmech" + side] = mech
        for prefix, field in (("pass", "pass"), ("refresh", "refresh_oauth2_token"), ("provider", "provider_oauth2_token")):
            value = str(form.get(field + side, ""))
            if value or not account_id:
                account[prefix + side] = value
        if mech == "XOAUTH2" and (account.get("provider" + side) not in OAUTH_CONFIG or not account.get("refresh" + side)):
            raise HTTPException(400, "Connectez le compte OAuth pour obtenir un jeton de renouvellement")
    if not account_id:
        config["accounts"].append(account)
    save_config(config)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/account/delete/{account_id}")
async def delete_account(request: Request, account_id: str):
    config = load_config()
    account = account_for(request, config, account_id)
    if account_id in processes:
        raise HTTPException(409, "Arrêtez la synchronisation avant de supprimer")
    config["accounts"].remove(account)
    save_config(config)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/account/run/{account_id}")
async def run_account(request: Request, account_id: str):
    account = account_for(request, load_config(), account_id)
    if account_id in processes:
        raise HTTPException(409, "Synchronisation déjà en cours")
    processes[account_id] = {"owner": account["owner"], "process": None, "cancelled": False}
    spawn(execute(copy.deepcopy(account), copy.deepcopy(request.state.user)))
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/account/stop/{account_id}")
async def stop_account(request: Request, account_id: str):
    account_for(request, load_config(), account_id)
    active = processes.get(account_id)
    if active:
        active["cancelled"] = True
        if active["process"] and active["process"].returncode is None:
            active["process"].terminate()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logs/{run_id}")
async def run_logs(request: Request, run_id: str):
    run = next((r for r in load_config()["runs"] if r["id"] == run_id), None)
    if not run or not visible(request.state.user, run):
        raise HTTPException(404, "Historique introuvable")
    return render(request, "logs.html", run=run)


@app.get("/manual")
async def manual(request: Request):
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/cgi-bin/imapsync")
async def retired_cgi(request: Request):
    raise HTTPException(410, "Créez une configuration puis lancez-la depuis votre tableau de bord")


OAUTH_CONFIG = {
    "google": {"auth_url": "https://accounts.google.com/o/oauth2/v2/auth", "token_url": "https://oauth2.googleapis.com/token", "scope": "https://mail.google.com/"},
    "microsoft": {"auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize", "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token", "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"},
}


def oauth_credentials(provider):
    config = load_config().get("oauth_apps", {})
    prefix = "google" if provider == "google" else "ms"
    return {"client_id": config.get(prefix + "_client_id", ""), "client_secret": config.get(prefix + "_client_secret", "")}


@app.get("/oauth/login/{provider}")
async def oauth_login(request: Request, provider: str, target_field: str):
    if provider not in OAUTH_CONFIG or target_field not in {"oauth2_token1", "oauth2_token2"}:
        raise HTTPException(400, "Paramètres OAuth invalides")
    credentials = oauth_credentials(provider)
    if not credentials["client_id"]:
        raise HTTPException(400, "Le fournisseur OAuth n'est pas configuré")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    import base64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    data = auth_data()
    data["oauth"][digest(state)] = {"provider": provider, "target": target_field, "verifier": verifier,
                                  "session": digest(request.cookies[COOKIE]), "expires": time.time() + 600}
    write_json(AUTH_FILE, data)
    params = {"client_id": credentials["client_id"], "response_type": "code", "redirect_uri": PUBLIC_URL + "/oauth/callback",
              "scope": OAUTH_CONFIG[provider]["scope"], "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    if provider == "google":
        params.update(access_type="offline", prompt="consent")
    return RedirectResponse(OAUTH_CONFIG[provider]["auth_url"] + "?" + urlencode(params))


@app.get("/oauth/callback")
async def oauth_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    auth = auth_data()
    item = auth["oauth"].get(digest(state))
    if not item or item["session"] != digest(request.cookies[COOKIE]):
        raise HTTPException(400, "Session OAuth invalide ou expirée")
    del auth["oauth"][digest(state)]
    write_json(AUTH_FILE, auth)
    if error or not code:
        raise HTTPException(400, "Autorisation OAuth non accordée")
    data = {**oauth_credentials(item["provider"]), "code": code, "grant_type": "authorization_code",
            "redirect_uri": PUBLIC_URL + "/oauth/callback", "code_verifier": item["verifier"]}
    try:
        response = await asyncio.to_thread(requests.post, OAUTH_CONFIG[item["provider"]]["token_url"], data=data, timeout=20)
        response.raise_for_status()
        tokens = response.json()
        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise ValueError("Missing tokens")
    except (requests.RequestException, ValueError):
        raise HTTPException(502, "Impossible d'obtenir les jetons OAuth")
    return render(request, "oauth.html", target=item["target"], provider=item["provider"], tokens=tokens)


async def execute(account, actor):
    account_id = str(account["id"])
    active = processes[account_id]
    config = load_config()
    run = {"id": secrets.token_hex(12), "account_id": account_id, "owner": account["owner"],
           "actor": actor["pseudo"], "label": account["label"], "started": datetime.now(timezone.utc).isoformat(),
           "status": "Synchronisation...", "log": ""}
    config["runs"].append(run)
    for current in config["accounts"]:
        if str(current["id"]) == account_id:
            current["status"] = run["status"]
    save_config(config)
    status = "Erreur"
    output = bytearray()
    truncated = False
    private_values = [str(account.get(prefix + side, "")) for prefix in ("pass", "refresh") for side in ("1", "2")]
    try:
        cmd = ["imapsync", "--nolog", "--ssl1", "--ssl2"]
        for side in ("1", "2"):
            cmd += ["--host" + side, account["host" + side], "--user" + side, account["user" + side]]
            if account.get("authmech" + side) == "XOAUTH2":
                provider = account["provider" + side]
                response = await asyncio.to_thread(requests.post, OAUTH_CONFIG[provider]["token_url"],
                    data={**oauth_credentials(provider), "refresh_token": account["refresh" + side], "grant_type": "refresh_token"}, timeout=20)
                response.raise_for_status()
                tokens = response.json()
                token = tokens["access_token"]
                private_values.append(token)
                if tokens.get("refresh_token"):
                    fresh = load_config()
                    for current in fresh["accounts"]:
                        if str(current["id"]) == account_id:
                            current["refresh" + side] = tokens["refresh_token"]
                    save_config(fresh)
                cmd += ["--authmech" + side, "XOAUTH2", "--oauth2_token" + side, token]
            else:
                cmd += ["--password" + side, account.get("pass" + side, "")]
        if active["cancelled"]:
            status = "Annulée"
        else:
            process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            active["process"] = process
            # Keep a bounded tail; never expose passwords or OAuth tokens in history.
            while chunk := await process.stdout.read(8192):
                output.extend(chunk)
                if len(output) > 65536:
                    truncated = True
                    del output[:-65536]
            await process.wait()
            status = "Annulée" if active["cancelled"] else ("Succès" if process.returncode == 0 else "Erreur")
            text = output.decode("utf-8", errors="replace")
            if truncated:
                text = text.partition("\n")[2]
            for value in sorted(filter(None, private_values), key=len, reverse=True):
                text = text.replace(value, "[SECRET MASQUÉ]")
            text = "\n".join(line for line in text.splitlines() if not re.search(r"password|oauth|token|command line|auth.*plain", line, re.I))
            run["log"] = text + f"\nCode de retour imapsync : {process.returncode}."
    except asyncio.CancelledError:
        status = "Interrompue"
        process = active["process"]
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise
    except Exception:
        run["log"] = "Échec de connexion, d'authentification ou de lancement. Vérifiez les paramètres IMAP/OAuth."
    finally:
        config = load_config()
        finished = datetime.now(timezone.utc).isoformat()
        for current in config["runs"]:
            if current["id"] == run["id"]:
                current.update(status=status, finished=finished, log=run["log"])
        for current in config["accounts"]:
            if str(current["id"]) == account_id:
                current.update(status=status, last_run=finished)
        save_config(config)
        if status == "Erreur":
            with CONFIG_FILE.with_name("daily_errors.log").open("a", encoding="utf-8") as report:
                report.write(f"[{finished}] [{account['owner']}] [{actor['pseudo']}] Synchronisation {account_id} en erreur.\n")
        processes.pop(account_id, None)


async def sync_loop():
    # Never persist an old snapshot after awaiting a subprocess.
    while True:
        config = load_config()
        for previous in config["accounts"]:
            # Re-read after each execution so deletion/reassignment is respected.
            account = next((a for a in load_config()["accounts"] if a["id"] == previous["id"]), None)
            if account is None:
                continue
            account_id = str(account["id"])
            if account_id not in processes and any(u["email"] == account.get("owner") for u in config["users"]):
                processes[account_id] = {"owner": account["owner"], "process": None, "cancelled": False}
                await execute(copy.deepcopy(account), {"pseudo": "Planificateur"})
        await asyncio.sleep(max(1, load_config().get("poll_interval", 5)) * 60)
