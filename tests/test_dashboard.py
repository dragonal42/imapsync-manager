import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import main
from test_tenants import env, client, form


def test_pages_are_distinct_and_navigation_restored(env):
    admin = client("admin@example.com")
    dashboard = admin.get("/dashboard").text
    assert "Créer un utilisateur" not in dashboard and "ADMIN_SECRET" not in dashboard
    assert "Erreurs aujourd’hui" in dashboard and 'href="/manual"' in dashboard
    administration = admin.get("/admin").text
    assert "Créer un utilisateur" in administration and "Paramètres globaux" in administration
    assert "ALICE_PRIVATE" not in administration and "Historique des exécutions" not in administration
    assert 'id="manual-form"' in admin.get("/manual").text
    assert client("alice@example.com").get("/admin").status_code == 403


def seed_runs():
    today = datetime.now(main.local_zone()).date()
    config = main.load_config()
    config["runs"] = []
    for days, owner, key in [(0, "alice@example.com", "today"), (4, "alice@example.com", "included"), (5, "alice@example.com", "excluded"), (0, "bob@example.com", "bob")]:
        day = today - timedelta(days=days)
        config["runs"].append({"id": key, "owner": owner, "account_id": "a" if owner.startswith("alice") else "b", "label": "RUN_" + key,
            "actor": "Test", "status": "Erreur", "started": day.isoformat() + "T12:34:56.123456+00:00", "log": "log"})
    main.save_config(config)
    return today


def test_date_filter_stats_and_second_precision(env):
    today = seed_runs()
    response = client("alice@example.com").get("/dashboard")
    assert "RUN_today" in response.text and "RUN_included" in response.text
    assert "RUN_excluded" not in response.text and "RUN_bob" not in response.text
    assert ".123456" not in response.text
    assert 'Erreurs aujourd’hui</span><strong>1</strong>' in response.text
    assert 'Utilisateurs dans mon espace</span><strong>1</strong>' in response.text
    admin = client("admin@example.com").get("/dashboard").text
    assert 'Erreurs aujourd’hui</span><strong>2</strong>' in admin
    assert 'Utilisateurs</span><strong>3</strong>' in admin
    old = (today - timedelta(days=5)).isoformat()
    result = client("alice@example.com").get("/dashboard", params={"date_from": old, "date_to": old}).text
    assert "RUN_excluded" in result and "RUN_today" not in result
    assert client("alice@example.com").get("/dashboard?date_from=invalid").status_code == 400
    assert client("alice@example.com").get("/dashboard?date_from=2030-01-01&date_to=2020-01-01").status_code == 400


def test_timezone_day_boundaries(env, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Paris")
    assert str(main.run_date({"started": "2026-09-09T22:30:00+00:00"})) == "2026-09-10"
    assert main.display_time("2026-09-09T22:30:00.123456+00:00") == "10/09/2026 00:30:00"
    assert main.display_time("2026-09-09 22:30:00") == "09/09/2026 22:30:00"


def test_pause_persistence_and_ownership(env):
    alice = client("alice@example.com")
    assert main.load_config()["accounts"][0]["schedule_state"] == "RUNNING"
    assert alice.post("/account/schedule/a", data={"schedule_state": "PAUSED"}).status_code == 303
    assert main.load_config()["accounts"][0]["schedule_state"] == "PAUSED"
    assert alice.post("/account/schedule/b", data={"schedule_state": "PAUSED"}).status_code == 404
    assert alice.post("/account/schedule/a", data={"schedule_state": "EVIL"}).status_code == 400
    assert alice.post("/account/edit/a", data=form()).status_code == 303
    assert main.load_config()["accounts"][0]["schedule_state"] == "PAUSED"
    assert alice.post("/account/add", data=form(schedule_state="PAUSED")).status_code == 303
    assert main.load_config()["accounts"][-1]["schedule_state"] == "PAUSED"


def test_scheduler_skips_paused_and_manual_launch_allowed(env, monkeypatch):
    config = main.load_config()
    config["accounts"][0]["schedule_state"] = "PAUSED"
    main.save_config(config)
    called = []
    async def execute(account, actor):
        called.append(account["id"])
        main.processes.pop(str(account["id"]), None)
    async def stop_loop(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(main, "execute", execute)
    original_sleep = main.asyncio.sleep
    monkeypatch.setattr(main.asyncio, "sleep", stop_loop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.sync_loop())
    assert called == ["b"]
    monkeypatch.setattr(main.asyncio, "sleep", original_sleep)
    assert client("alice@example.com").post("/account/run/a").status_code == 303
    assert "a" in called


def test_delete_and_clear_logs_respect_historical_owner(env):
    seed_runs()
    alice = client("alice@example.com")
    assert alice.post("/logs/delete/bob").status_code == 404
    assert alice.post("/account/logs/clear/b").status_code == 404
    assert alice.post("/logs/delete/included").status_code == 303
    assert not any(r["id"] == "included" for r in main.load_config()["runs"])
    # A configuration transfer does not transfer the old logs.
    config = main.load_config()
    config["runs"].append({"id": "old-owner", "account_id": "a", "owner": "bob@example.com", "status": "Succès"})
    main.save_config(config)
    assert alice.post("/account/logs/clear/a").status_code == 303
    assert {r["id"] for r in main.load_config()["runs"]} == {"bob", "old-owner"}


def test_active_log_cannot_be_deleted(env):
    seed_runs()
    config = main.load_config()
    config["runs"][0]["status"] = "Synchronisation..."
    main.save_config(config)
    alice = client("alice@example.com")
    assert alice.post("/logs/delete/today").status_code == 409
    assert alice.post("/account/logs/clear/a").status_code == 409


def test_manual_ephemeral_run_and_isolation(env, monkeypatch):
    observed = []
    async def execute(account, actor):
        observed.append(account)
        state = main.processes[account["id"]]
        config = main.load_config()
        config["runs"].append({"id": state["run_id"], "account_id": account["id"], "owner": account["owner"], "status": "Synchronisation...", "log": "live"})
        main.save_config(config)
    monkeypatch.setattr(main, "execute", execute)
    alice, bob = client("alice@example.com"), client("bob@example.com")
    payload = form(password1="p1", password2="p2", owner="bob@example.com", dry="on", subfolder1="Inbox")
    result = alice.post("/cgi-bin/imapsync", data=payload)
    assert result.status_code == 200
    run_id = result.json()["run_id"]
    assert observed[0]["owner"] == "alice@example.com"
    assert observed[0]["options"] == ["--dry", "--subfolder1", "Inbox"]
    assert len(main.load_config()["accounts"]) == 2
    assert "p1" not in main.CONFIG_FILE.read_text()
    assert alice.get("/api/logs/" + run_id).json()["log"] == "live"
    assert bob.get("/api/logs/" + run_id).status_code == 404
    assert bob.post("/cgi-bin/imapsync", data={"abort": "on", "run_id": run_id}).status_code == 404
    assert alice.post("/cgi-bin/imapsync", data=payload).status_code == 409
    assert alice.post("/cgi-bin/imapsync", data={"abort": "on", "run_id": run_id}).status_code == 200
    assert main.processes[observed[0]["id"]]["cancelled"]


def test_manual_rejects_missing_credentials_and_arguments(env):
    alice = client("alice@example.com")
    assert alice.post("/cgi-bin/imapsync", data=form()).status_code == 400
    assert alice.post("/cgi-bin/imapsync", data=form(extra="--passwordfile /data/config.json")).status_code == 400
    assert alice.post("/cgi-bin/imapsync", data=form(host1="--bad", password1="p", password2="p")).status_code == 400
    assert alice.post("/cgi-bin/imapsync", data=form(password1="p", password2="p", delete1="on", delete2="on")).status_code == 400


def test_configuration_requires_password_or_oauth(env):
    response = client("alice@example.com").post("/account/add", data=form(pass1="", pass2=""))
    assert response.status_code == 400
    assert "mot de passe IMAP" in response.json()["detail"]
    assert len(main.load_config()["accounts"]) == 2


def test_log_redaction_keeps_actionable_errors(env):
    result = main.clean_log(b"Command line: --password1 secret\nError: missing --password2\nAUTH PLAIN abc\nsecret\n", ["secret"])
    assert "secret" not in result and "AUTH PLAIN" not in result
    assert "Error: missing --password2" in result
