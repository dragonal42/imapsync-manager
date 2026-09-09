import os
import json
import asyncio
import subprocess
from datetime import datetime
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# Mount static files
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

CONFIG_FILE = "data/config.json"
DAILY_REPORT = "data/daily_errors.log"

manual_processes = {} # Store pids for manual sync aborts

def load_config():
    if not os.path.exists(CONFIG_FILE):
        default_config = {
            "poll_interval": 5,
            "report_email": "admin@example.com",
            "accounts": []
        }
        save_config(default_config)
        return default_config
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(config):
    os.makedirs("data", exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

def log_error(account_label, message):
    os.makedirs("data", exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] [Compte: {account_label}] {message}\n"
    with open(DAILY_REPORT, "a") as f:
        f.write(entry)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    config = load_config()
    logs = ""
    if os.path.exists(DAILY_REPORT):
        with open(DAILY_REPORT, "r") as f:
            logs = f.read()
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={"config": config, "logs": logs}
    )

@app.get("/manual", response_class=HTMLResponse)
async def manual(request: Request):
    return templates.TemplateResponse(
        request=request, 
        name="manual.html", 
        context={}
    )

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
    host1: str = Form(...), user1: str = Form(...), pass1: str = Form(...),
    host2: str = Form(...), user2: str = Form(...), pass2: str = Form(...)
):
    config = load_config()
    config["accounts"].append({
        "id": int(datetime.now().timestamp()),
        "label": label,
        "host1": host1, "user1": user1, "pass1": pass1,
        "host2": host2, "user2": user2, "pass2": pass2,
        "last_run": "Jamais",
        "status": "En attente"
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
            try:
                proc.terminate()
            except:
                pass
        manual_processes.clear()
        return HTMLResponse(content="Aborted by user.\n")

    cmd = ["imapsync"]
    for f in ["host1", "user1", "password1", "host2", "user2", "password2"]:
        if form_data.get(f):
            cmd.extend([f"--{f}", form_data.get(f)])
    
    if form_data.get("delete1") == "on": cmd.append("--delete1")
    if form_data.get("delete2") == "on": cmd.append("--delete2")
    if form_data.get("dry") == "on": cmd.append("--dry")
    if form_data.get("justlogin") == "on": cmd.append("--justlogin")
    if form_data.get("justfolders") == "on": cmd.append("--justfolders")
    if form_data.get("justfoldersizes") == "on": cmd.append("--justfoldersizes")
    
    for f in ["subfolder1", "subfolder2", "extra"]:
        if form_data.get(f):
            # for extra parameters we just append as is if provided manually
            if f == "extra":
                extras = form_data.get(f).split()
                cmd.extend(extras)
            else:
                cmd.extend([f"--{f}", form_data.get(f)])

    async def stream_output():
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            manual_processes[process.pid] = process
            
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                yield line.decode('utf-8', errors='replace')
        except Exception as e:
            yield f"Error starting imapsync: {str(e)}\n"
        finally:
            if 'process' in locals() and process.pid in manual_processes:
                del manual_processes[process.pid]

    return StreamingResponse(stream_output(), media_type="text/plain")


async def sync_loop():
    while True:
        config = load_config()
        interval = config.get("poll_interval", 5) * 60
        
        for acc in config.get("accounts", []):
            label = acc["label"]
            cmd = [
                "imapsync",
                "--host1", acc["host1"], "--user1", acc["user1"], "--password1", acc["pass1"],
                "--host2", acc["host2"], "--user2", acc["user2"], "--password2", acc["pass2"],
                "--delete1", "--ssl1", "--ssl2"
            ]
            
            acc["status"] = "Synchronisation..."
            save_config(config)
            
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                
                acc["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if process.returncode == 0:
                    acc["status"] = "Succès"
                else:
                    acc["status"] = "Erreur"
                    err_msg = stderr.decode() if stderr else stdout.decode()
                    log_error(label, f"Échec de la synchro (Code {process.returncode}) : {err_msg[:300]}")
            except Exception as e:
                acc["status"] = "Erreur"
                log_error(label, f"Erreur système : {str(e)}")
            
            save_config(config)
            
        await asyncio.sleep(interval)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(sync_loop())
