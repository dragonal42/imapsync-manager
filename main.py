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
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import requests
from ai_preprocessing import preprocess, PreprocessingError
from run_logging import RunMetrics
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


def local_zone():
    return ZoneInfo(os.getenv("TZ", "Europe/Paris"))


def parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # Legacy last_run values were written in the container's local timezone.
        return parsed.replace(tzinfo=local_zone()) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


def display_time(value):
    parsed = parse_timestamp(value)
    return parsed.astimezone(local_zone()).strftime("%d/%m/%Y %H:%M:%S") if parsed else value


templates.env.filters["datetime_seconds"] = display_time


def run_date(run):
    parsed = parse_timestamp(run.get("finished") or run.get("started"))
    return parsed.astimezone(local_zone()).date() if parsed else None


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
    config.setdefault("log_debug", False)
    config.setdefault("log_retention_days", 90)
    for account in config["accounts"]:
        account.setdefault("schedule_state", "RUNNING")
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
            run.update(status="Interrompue", log=run.get("log", "") + "\nLe service a redémarré pendant cette exécution.")
    for account in config["accounts"]:
        if account.get("status") == "Synchronisation...":
            account["status"] = "Interrompue"
    save_config(config)
    rotate_logs()
    spawn(log_rotation_loop())
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
    # no-referrer makes browsers send Origin: null on native form POSTs,
    # which our CSRF check correctly rejects. Preserve the origin, but never
    # disclose paths or query strings (magic links / OAuth codes) in Referer.
    response.headers["Referrer-Policy"] = "strict-origin"
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
    response = RedirectResponse("/dashboard", status_code=303)
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


@app.get("/dashboard")
async def dashboard(request: Request, owner: str = "", date_from: str = "", date_to: str = ""):
    config = load_config()
    today = datetime.now(local_zone()).date()
    try:
        start = date.fromisoformat(date_from) if date_from else today - timedelta(days=4)
        end = date.fromisoformat(date_to) if date_to else today
        if start > end:
            raise ValueError()
    except ValueError:
        raise HTTPException(400, "Période de dates invalide")
    config["accounts"] = [a for a in config["accounts"] if visible(request.state.user, a)
                          and (request.state.user["role"] != "admin" or not owner or a["owner"] == owner)]
    config["runs"] = [r for r in config["runs"] if visible(request.state.user, r)
                      and (request.state.user["role"] != "admin" or not owner or r["owner"] == owner)]
    stats = {"users": len(config["users"]) if request.state.user["role"] == "admin" else 1,
             "accounts": len(config["accounts"]),
             "errors": sum(r.get("status") == "Erreur" and run_date(r) == today for r in config["runs"])}
    config["runs"] = [r for r in config["runs"] if run_date(r) and start <= run_date(r) <= end]
    if request.state.user["role"] != "admin":
        config["users"] = []
    config["oauth_apps"] = {}
    config.pop("sApiKeyMistral", None)
    config.pop("sApiKeyGemini", None)
    config["report_email"] = ""
    return render(request, "dashboard.html", config=config, owner=owner, stats=stats,
                  date_from=start.isoformat(), date_to=end.isoformat(), timezone_name=str(local_zone()))


@app.get("/api/task-status")
async def task_status(request: Request):
    config = load_config()
    return {"accounts": [{"id": str(a["id"]), "status": a.get("status", ""), "last_run": display_time(a.get("last_run", ""))}
                         for a in config["accounts"] if visible(request.state.user, a)]}


@app.get("/admin")
async def administration(request: Request):
    require_admin(request)
    return render(request, "admin.html", config=load_config())


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


@app.post("/logs/settings")
@app.post("/ai/settings")
@app.post("/settings")
@app.post("/oauth/settings")
async def settings(request: Request):
    require_admin(request)
    form = await request.form()
    config = load_config()
    if request.url.path == "/logs/settings":
        try:
            days = int(form.get("log_retention_days", 90))
            if not 1 <= days <= 3650:
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(400, "Conservation invalide (1 à 3650 jours)")
        config.update(log_debug=form.get("log_debug") == "on", log_retention_days=days)
    elif request.url.path == "/ai/settings":
        for key in ("sApiKeyMistral", "sApiKeyGemini"):
            value = str(form.get(key, "")).strip()
            if len(value) > 4096 or any(ord(c) < 32 for c in value):
                raise HTTPException(400, "Clé API invalide")
            if form.get("clear_" + key):
                config.pop(key, None)
            elif value:
                config[key] = value
    elif request.url.path == "/settings":
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
    if request.url.path == "/logs/settings":
        rotate_logs()
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
    form = await request.form()
    config = load_config()
    account = account_for(request, config, account_id) if account_id else {"id": secrets.token_hex(12), "last_run": "Jamais", "status": "En attente"}
    if str(account["id"]) in processes:
        raise HTTPException(409, "Attendez la fin de l'exécution avant de modifier cette configuration")
    owner = str(form.get("owner", request.state.user["email"])) if request.state.user["role"] == "admin" else request.state.user["email"]
    if not any(u["email"] == owner for u in config["users"]):
        raise HTTPException(400, "Propriétaire inconnu")
    account["owner"] = owner
    schedule_state = str(form.get("schedule_state", account.get("schedule_state", "RUNNING")))
    if schedule_state not in {"RUNNING", "PAUSED"}:
        raise HTTPException(400, "État de planification invalide")
    account["schedule_state"] = schedule_state
    # A hidden marker distinguishes the new unchecked checkbox from legacy clients.
    sync = form.get("bActiverSynchro") == "on" if form.get("task_options") else account.get("bActiverSynchro", True)
    ai = form.get("bPretraitementIA") == "on"
    try:
        days = int(form.get("nPeriodeJours", 5))
        if not 1 <= days <= 365:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, "Période IA invalide (1 à 365 jours)")
    engine = str(form.get("sMoteurIA", "Mistral"))
    if engine not in {"Mistral", "Gemini"}:
        raise HTTPException(400, "Moteur IA invalide")
    if ai and not config.get("sApiKey" + engine):
        raise HTTPException(400, "Clé API IA absente : contactez l’administrateur")
    folder = str(form.get("source_folder", account.get("source_folder", "INBOX"))).strip()
    if not folder or len(folder) > 255 or any(ord(c) < 32 or ord(c) > 126 or c in '\\"*%' for c in folder) or "_01-Arnaques" in folder:
        raise HTTPException(400, "Dossier source invalide (nom IMAP ASCII, hors quarantaine)")
    account.update(bActiverSynchro=sync, bPretraitementIA=ai, nPeriodeJours=days, sMoteurIA=engine, source_folder=folder)
    for key in (("label", "host1", "user1", "host2", "user2") if sync else ("label", "host1", "user1")):
        value = str(form.get(key, "")).strip()
        if not value or len(value) > 255 or value.startswith("-") or any(ord(c) < 32 for c in value):
            raise HTTPException(400, "Champ invalide : " + key)
        account[key] = value
    
    # Gérer les options de suppression
    account["delete1"] = str(form.get("delete1", "off"))
    account["delete2"] = "off"  # Désactivé par défaut pour la destination
    for side in (("1", "2") if sync else ("1",)):
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
        if mech == "PLAIN" and not account.get("pass" + side):
            raise HTTPException(400, "Renseignez le mot de passe IMAP " + side + " ou choisissez OAuth2")
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


@app.post("/account/schedule/{account_id}")
async def schedule_account(request: Request, account_id: str):
    form = await request.form()
    config = load_config()
    account = account_for(request, config, account_id)
    state = str(form.get("schedule_state", ""))
    if state not in {"RUNNING", "PAUSED"}:
        raise HTTPException(400, "État de planification invalide")
    account["schedule_state"] = state
    save_config(config)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/account/logs/clear/{account_id}")
async def clear_account_logs(request: Request, account_id: str):
    config = load_config()
    account_for(request, config, account_id)
    if account_id in processes:
        raise HTTPException(409, "Attendez la fin de l'exécution avant de vider les journaux")
    removable = [r for r in config["runs"] if str(r.get("account_id")) == account_id and visible(request.state.user, r)]
    if any(r["status"] == "Synchronisation..." for r in removable):
        raise HTTPException(409, "Attendez la fin de l'exécution avant de vider les journaux")
    config["runs"] = [r for r in config["runs"] if r not in removable]
    save_config(config)
    return RedirectResponse("/dashboard", status_code=303)


def run_for(request, config, run_id):
    run = next((r for r in config["runs"] if r["id"] == run_id), None)
    if not run or not visible(request.state.user, run):
        raise HTTPException(404, "Historique introuvable")
    return run


@app.post("/logs/delete/{run_id}")
async def delete_run(request: Request, run_id: str):
    config = load_config()
    run = run_for(request, config, run_id)
    if run["status"] == "Synchronisation...":
        raise HTTPException(409, "Attendez la fin de l'exécution avant de supprimer son journal")
    config["runs"].remove(run)
    save_config(config)
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
    run = run_for(request, load_config(), run_id)
    return render(request, "logs.html", run=run)


@app.get("/manual")
async def manual(request: Request):
    return render(request, "manual.html")


@app.post("/cgi-bin/imapsync")
async def manual_sync(request: Request):
    form = await request.form()
    if form.get("abort") == "on":
        run = run_for(request, load_config(), str(form.get("run_id", "")))
        active = processes.get(run["account_id"])
        if active:
            active["cancelled"] = True
            if active["process"] and active["process"].returncode is None:
                active["process"].terminate()
        return {"message": "Arrêt demandé"}
    if form.get("extra"):
        raise HTTPException(400, "Utilisez les options proposées ; les arguments libres ne sont pas autorisés")
    if any(p["owner"] == request.state.user["email"] and key.startswith("manual-") for key, p in processes.items()):
        raise HTTPException(409, "Une synchronisation manuelle est déjà en cours dans votre espace")
    account = {"id": "manual-" + secrets.token_hex(12), "owner": request.state.user["email"], "label": "Synchronisation manuelle"}
    for key in ("host1", "host2", "user1", "user2"):
        value = str(form.get(key, "")).strip()
        if not value or len(value) > 255 or value.startswith("-") or any(ord(c) < 32 for c in value):
            raise HTTPException(400, "Champ invalide : " + key)
        account[key] = value
    for side in ("1", "2"):
        mech = str(form.get("authmech" + side, "PLAIN"))
        if mech not in {"PLAIN", "XOAUTH2"}:
            raise HTTPException(400, "Authentification IMAP invalide")
        account["authmech" + side] = mech
        account["pass" + side] = str(form.get("password" + side, ""))
        account["token" + side] = str(form.get("oauth2_token" + side, ""))
        if not account[("token" if mech == "XOAUTH2" else "pass") + side]:
            raise HTTPException(400, "Renseignez les identifiants de la messagerie " + side)
    account["options"] = ["--" + f for f in ("delete1", "delete2", "dry", "justlogin", "justfolders", "justfoldersizes") if form.get(f) == "on"]
    if "--delete1" in account["options"] and "--delete2" in account["options"]:
        raise HTTPException(400, "Choisissez une seule option de suppression : source ou destination")
    for key in ("subfolder1", "subfolder2"):
        value = str(form.get(key, ""))
        if value:
            if len(value) > 255 or value.startswith("-") or any(ord(c) < 32 for c in value):
                raise HTTPException(400, "Dossier invalide")
            account["options"] += ["--" + key, value]
    run_id = secrets.token_hex(12)
    processes[account["id"]] = {"owner": account["owner"], "process": None, "cancelled": False, "run_id": run_id}
    spawn(execute(account, copy.deepcopy(request.state.user)))
    return {"run_id": run_id, "message": "Synchronisation manuelle lancée"}


@app.get("/api/logs/{run_id}")
async def poll_run(request: Request, run_id: str):
    run = run_for(request, load_config(), run_id)
    return {"status": run["status"], "log": run["log"], "finished": run["status"] != "Synchronisation..."}


OAUTH_CONFIG = {
    "google": {"auth_url": "https://accounts.google.com/o/oauth2/v2/auth", "token_url": "https://oauth2.googleapis.com/token", "scope": "https://mail.google.com/"},
    "microsoft": {"auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize", "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token", "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"},
}


def oauth_credentials(provider):
    config = load_config().get("oauth_apps", {})
    prefix = "google" if provider == "google" else "ms"
    return {"client_id": config.get(prefix + "_client_id", ""), "client_secret": config.get(prefix + "_client_secret", "")}


def oauth_result(request, target="", provider="", tokens=None, error="", status_code=200):
    response = render(request, "oauth.html", target=target, provider=provider, tokens=tokens or {}, error=error)
    response.status_code = status_code
    return response


@app.get("/oauth/login/{provider}")
async def oauth_login(request: Request, provider: str, target_field: str):
    if provider not in OAUTH_CONFIG or target_field not in {"oauth2_token1", "oauth2_token2"}:
        raise HTTPException(400, "Paramètres OAuth invalides")
    credentials = oauth_credentials(provider)
    if not credentials["client_id"]:
        return oauth_result(request, target_field, provider, error="Le fournisseur OAuth n'est pas configuré. Contactez l’administrateur.", status_code=400)
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
        return oauth_result(request, error="Session OAuth invalide ou expirée. Recommencez la connexion.", status_code=400)
    del auth["oauth"][digest(state)]
    write_json(AUTH_FILE, auth)
    if error or not code:
        return oauth_result(request, item["target"], item["provider"], error="Autorisation OAuth non accordée. Recommencez la connexion.", status_code=400)
    data = {**oauth_credentials(item["provider"]), "code": code, "grant_type": "authorization_code",
            "redirect_uri": PUBLIC_URL + "/oauth/callback", "code_verifier": item["verifier"]}
    try:
        response = await asyncio.to_thread(requests.post, OAUTH_CONFIG[item["provider"]]["token_url"], data=data, timeout=20)
        response.raise_for_status()
        tokens = response.json()
        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise ValueError("Missing tokens")
    except (requests.RequestException, ValueError):
        return oauth_result(request, item["target"], item["provider"], error="Impossible d’obtenir les jetons OAuth. Recommencez la connexion avant d’enregistrer.", status_code=502)
    return oauth_result(request, item["target"], item["provider"], tokens=tokens)


async def refresh_access_token(account, side, private_values):
    provider = account["provider" + side]
    response = await asyncio.to_thread(requests.post, OAUTH_CONFIG[provider]["token_url"],
        data={**oauth_credentials(provider), "refresh_token": account["refresh" + side], "grant_type": "refresh_token"}, timeout=20)
    response.raise_for_status()
    tokens = response.json()
    token = tokens["access_token"]
    private_values.append(token)
    if tokens.get("refresh_token"):
        account["refresh" + side] = tokens["refresh_token"]
        private_values.append(tokens["refresh_token"])
        fresh = load_config()
        for current in fresh["accounts"]:
            if str(current["id"]) == str(account["id"]):
                current["refresh" + side] = tokens["refresh_token"]
        save_config(fresh)
    return token


async def execute(account, actor):
    account_id = str(account["id"])
    active = processes[account_id]
    config = load_config()
    run = {"id": active.get("run_id", secrets.token_hex(12)), "account_id": account_id, "owner": account["owner"],
           "actor": actor["pseudo"], "label": account["label"], "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "status": "Synchronisation...", "log": ""}
    config["runs"].append(run)
    for current in config["accounts"]:
        if str(current["id"]) == account_id:
            current["status"] = run["status"]
    save_config(config)
    status = "Erreur"
    debug = bool(config.get("log_debug", False))
    metrics = RunMetrics(account)
    note = ""
    output = bytearray()
    truncated = False
    private_values = [str(account.get(prefix + side, "")) for prefix in ("pass", "refresh", "token") for side in ("1", "2")]
    def log_snapshot(current_status, complete=True):
        summary = metrics.summary(current_status, note)
        details = clean_log(output, private_values, truncated, complete=complete)
        return (details + "\n\n" if debug and details else "") + summary

    execution_dir = tempfile.TemporaryDirectory(prefix="imapsync-")
    try:
        cmd = ["imapsync", "--nolog", "--ssl1", "--ssl2", "--tmpdir", execution_dir.name,
               "--pidfile", str(Path(execution_dir.name) / "imapsync.pid")]
        cmd += account.get("options", [])
        sync = account.get("bActiverSynchro", True)
        if sync and account.get("delete1") == "on" and "--delete1" not in cmd:
            cmd.append("--delete1")
        if sync and account.get("delete2") == "on" and "--delete2" not in cmd:
            cmd.append("--delete2")
        auth_started = time.monotonic()
        source_token = account.get("token1", "")
        for side in (("1", "2") if sync else ("1",)):
            cmd += ["--host" + side, account["host" + side], "--user" + side, account["user" + side]]
            if account.get("authmech" + side) == "XOAUTH2":
                if account.get("token" + side):
                    cmd += ["--authmech" + side, "XOAUTH2", "--oauthaccesstoken" + side, account["token" + side]]
                    continue
                token = await refresh_access_token(account, side, private_values)
                if side == "1":
                    source_token = token
                cmd += ["--authmech" + side, "XOAUTH2", "--oauthaccesstoken" + side, token]
            else:
                cmd += ["--password" + side, account.get("pass" + side, "")]
        if account.get("bPretraitementIA") or not sync:
            state_file = CONFIG_FILE.with_name("ai_state.json")
            def read_state(identity):
                return read_json(state_file, {}).get(identity, {})
            def save_state(identity, state):
                data = read_json(state_file, {})
                data[identity] = state
                write_json(state_file, data)
            def progress(message):
                if isinstance(message, dict):
                    metrics.event(message)
                else:
                    output.extend((message + "\n").encode())
                    del output[:-65536]
                run["log"] = log_snapshot("Synchronisation...")
                latest = load_config()
                for current in latest["runs"]:
                    if current["id"] == run["id"]:
                        current.update(log=run["log"], metrics=copy.deepcopy(metrics.data), log_debug=debug)
                save_config(latest)
            await preprocess(account, source_token, load_config().get("sApiKey" + account.get("sMoteurIA", "Mistral"), ""),
                             active, read_state, save_state, progress)
            if sync and account.get("bPretraitementIA"):
                cmd += ["--exclude", "_01-Arnaques"]
                # A long preprocessing phase must not launch imapsync with expired credentials.
                if time.monotonic() - auth_started >= 60 and not active["cancelled"]:
                    for side in ("1", "2"):
                        if account.get("authmech" + side) == "XOAUTH2":
                            cmd[cmd.index("--oauthaccesstoken" + side) + 1] = await refresh_access_token(account, side, private_values)
        if active["cancelled"]:
            status = "Annulée"
        elif not sync:
            status = "Succès"
        else:
            process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            active["process"] = process
            # Keep a bounded tail; never expose passwords or OAuth tokens in history.
            last_update = 0
            while chunk := await process.stdout.read(8192):
                metrics.feed(chunk)
                output.extend(chunk)
                if len(output) > 65536:
                    truncated = True
                    del output[:-65536]
                if time.monotonic() - last_update >= 1:
                    latest = load_config()
                    for current in latest["runs"]:
                        if current["id"] == run["id"]:
                            current.update(log=log_snapshot("Synchronisation...", complete=False), metrics=copy.deepcopy(metrics.data), log_debug=debug)
                    save_config(latest)
                    last_update = time.monotonic()
            await process.wait()
            metrics.feed(b"", final=True)
            metrics.data["returncode"] = process.returncode
            status = "Annulée" if active["cancelled"] else ("Succès" if process.returncode == 0 else "Erreur")
            if status == "Erreur":
                note = "Échec imapsync. Activez le niveau Debug pour détailler une prochaine exécution."
    except asyncio.CancelledError:
        status = "Interrompue"
        note = "Le service a interrompu cette exécution."
        process = active["process"]
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise
    except PreprocessingError as error:
        status = "Annulée" if active["cancelled"] else "Erreur"
        note = str(error)
    except Exception:
        note = "Échec de connexion, d'authentification ou de lancement. Vérifiez les paramètres IMAP/OAuth."
    finally:
        run["log"] = log_snapshot(status)
        config = load_config()
        finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for current in config["runs"]:
            if current["id"] == run["id"]:
                current.update(status=status, finished=finished, log=run["log"], metrics=copy.deepcopy(metrics.data), log_debug=debug)
        for current in config["accounts"]:
            if str(current["id"]) == account_id:
                current.update(status=status, last_run=finished)
        save_config(config)
        if status == "Erreur":
            with CONFIG_FILE.with_name("daily_errors.log").open("a", encoding="utf-8") as report:
                for line in metrics.summary(status, note).splitlines():
                    report.write(f"[{finished}] [{account['owner']}] [{actor['pseudo']}] Tâche {account_id} : {line}\n")
        processes.pop(account_id, None)
        execution_dir.cleanup()


def clean_log(output, private_values, truncated=False, complete=True):
    text = output.decode("utf-8", errors="replace")
    if truncated:
        text = text.partition("\n")[2]
    if not complete:
        text = text.rpartition("\n")[0] if "\n" in text else ""
    for value in sorted(filter(None, private_values), key=len, reverse=True):
        text = text.replace(value, "[SECRET MASQUÉ]")
    lines = []
    for line in text.splitlines():
        sensitive = re.search(r"command line|auth.*plain|password|oauth|token", line, re.I)
        diagnostic = re.search(r"\b(error|missing|required|failed|failure|invalid|cannot|can't|unknown|unrecognized|mandatory|supplementary)\b", line, re.I)
        if not sensitive or diagnostic:
            lines.append(line)
    return "\n".join(lines)


async def sync_loop():
    # Never persist an old snapshot after awaiting a subprocess.
    while True:
        config = load_config()
        for previous in config["accounts"]:
            # Re-read after each execution so deletion/reassignment is respected.
            account = next((a for a in load_config()["accounts"] if a["id"] == previous["id"]), None)
            if account is None or account.get("schedule_state") == "PAUSED":
                continue
            account_id = str(account["id"])
            if account_id not in processes and any(u["email"] == account.get("owner") for u in config["users"]):
                processes[account_id] = {"owner": account["owner"], "process": None, "cancelled": False}
                await execute(copy.deepcopy(account), {"pseudo": "Planificateur"})
        await asyncio.sleep(max(1, load_config().get("poll_interval", 5)) * 60)


def rotate_logs(now=None):
    """Age-based rotation of JSON run records; never rotate config.json itself."""
    now = now or datetime.now(timezone.utc)
    config = load_config()
    cutoff = now - timedelta(days=config.get("log_retention_days", 90))
    active_ids = {item.get("run_id") for item in processes.values()}
    def keep(run):
        timestamp = parse_timestamp(run.get("finished") or run.get("started"))
        return (run.get("status") == "Synchronisation..." or run.get("id") in active_ids
                or timestamp is None or timestamp >= cutoff)
    retained = [run for run in config["runs"] if keep(run)]
    removed = len(config["runs"]) - len(retained)
    if removed:
        config["runs"] = retained
        save_config(config)
    report = CONFIG_FILE.with_name("daily_errors.log")
    if report.exists():
        original = report.read_text(encoding="utf-8")
        lines = []
        for line in original.splitlines(keepends=True):
            match = re.match(r"^\[([^]]+)\]", line)
            stamp = parse_timestamp(match[1]) if match else None
            if stamp is None or stamp >= cutoff:
                lines.append(line)
        text = "".join(lines)
        if text != original:
            report.write_text(text, encoding="utf-8")
    return removed


async def log_rotation_loop():
    while True:
        await asyncio.sleep(3600)
        try:
            rotate_logs()
        except Exception:
            logger.exception("Échec de la purge des journaux ; nouvelle tentative dans une heure")
