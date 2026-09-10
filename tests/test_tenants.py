import asyncio
import time
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(main, "AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setattr(main, "ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setattr(main, "PUBLIC_URL", "https://testserver")
    main.processes.clear()
    config = main.load_config()
    config["users"] += [{"email": "alice@example.com", "pseudo": "Alice", "role": "user"},
                        {"email": "bob@example.com", "pseudo": "Bob", "role": "user"}]
    config["accounts"] = [{"id": "a", "owner": "alice@example.com", "label": "ALICE_PRIVATE", "host1": "imap.example.com", "host2": "imap.example.com", "user1": "alice", "user2": "dest", "pass1": "secret", "pass2": "secret", "last_run": "Jamais", "status": "En attente"},
                          {"id": "b", "owner": "bob@example.com", "label": "BOB_PRIVATE", "host1": "imap.example.com", "host2": "imap.example.com", "user1": "bob", "user2": "dest", "last_run": "Jamais", "status": "En attente"}]
    config["runs"] = [{"id": "bobrun", "owner": "bob@example.com", "label": "BOB_LOG", "actor": "Bob", "started": "now", "status": "Succès", "log": "PRIVATE LOG"}]
    config["oauth_apps"] = {"google_client_id": "client", "google_client_secret": "ADMIN_SECRET"}
    main.save_config(config)
    return tmp_path


def client(email=None):
    result = TestClient(main.app, base_url="https://testserver", follow_redirects=False)
    result.headers["Origin"] = main.PUBLIC_URL
    if email:
        token = "session-" + email
        data = main.auth_data()
        data["sessions"][main.digest(token)] = {"email": email, "expires": time.time() + 3600}
        main.write_json(main.AUTH_FILE, data)
        result.cookies.set(main.COOKIE, token)
    return result


def form(**changes):
    return {"label": "New", "host1": "imap.example.com", "host2": "imap.example.com", "user1": "a", "user2": "b", "pass1": "test-password", "pass2": "test-password", **changes}


def test_public_and_private_routes(env):
    browser = client()
    for path in ["/", "/login", "/cgu", "/privacy", "/auth/verify?token=x"]:
        response = browser.get(path)
        assert response.status_code == 200
        assert "Logo_ImapSyncManager_32p.png" in response.text
        assert "ADMIN_SECRET" not in response.text
    for path in ["/admin", "/dashboard", "/manual", "/logs/bobrun", "/oauth/login/google?target_field=oauth2_token1"]:
        assert browser.get(path).headers["location"] == "/login"
    assert "Logo_ImapSyncManager.png" in browser.get("/").text


def test_tenant_read_isolation(env):
    alice = client("alice@example.com")
    for path in ["/dashboard", "/dashboard?owner=bob@example.com"]:
        response = alice.get(path)
        assert "ALICE_PRIVATE" in response.text
        assert not any(s in response.text for s in ["BOB_PRIVATE", "BOB_LOG", "ADMIN_SECRET"])
    for path in ["/account/edit/b", "/logs/bobrun"]:
        assert alice.get(path).status_code == 404
    assert alice.get("/admin").status_code == 403
    assert 'value="secret"' not in alice.get("/account/edit/a").text


@pytest.mark.parametrize("path", ["/account/edit/b", "/account/delete/b", "/account/run/b", "/account/stop/b"])
def test_tenant_mutation_isolation(env, path):
    assert client("alice@example.com").post(path, data=form()).status_code == 404
    assert len(main.load_config()["accounts"]) == 2


@pytest.mark.parametrize("path", ["/settings", "/oauth/settings", "/admin/users"])
def test_admin_only_mutations(env, path):
    assert client("alice@example.com").post(path, data=form()).status_code == 403


def test_create_edit_ownership(env):
    alice = client("alice@example.com")
    assert alice.post("/account/add", data=form(owner="bob@example.com")).status_code == 303
    assert main.load_config()["accounts"][-1]["owner"] == "alice@example.com"
    assert alice.post("/account/edit/a", data=form(owner="bob@example.com", pass1="", pass2="")).status_code == 303
    account = main.load_config()["accounts"][0]
    assert account["owner"] == "alice@example.com"
    assert account["pass1"] == "secret"
    assert client("admin@example.com").post("/account/add", data=form(owner="bob@example.com")).status_code == 303
    assert main.load_config()["accounts"][-1]["owner"] == "bob@example.com"


def test_admin_filter_and_users(env):
    admin = client("admin@example.com")
    response = admin.get("/dashboard?owner=bob@example.com")
    assert "BOB_PRIVATE" in response.text and "ALICE_PRIVATE" not in response.text
    assert admin.post("/admin/users", data={"email": "CHARLIE@example.com", "pseudo": "Charlie", "role": "admin"}).status_code == 303
    assert main.load_config()["users"][-1] == {"email": "charlie@example.com", "pseudo": "Charlie", "role": "user"}
    assert admin.post("/admin/users", data={"email": "charlie@example.com", "pseudo": "Charlie"}).status_code == 409


def test_csrf(env):
    alice = client("alice@example.com")
    assert alice.post("/account/delete/a", headers={"Origin": "https://evil.example"}).status_code == 403
    del alice.headers["Origin"]
    assert alice.post("/account/delete/a").status_code == 403


def test_form_referrer_policy_and_origin_fallback(env):
    browser = client()
    for path in ["/login", "/auth/verify?token=private"]:
        assert browser.get(path).headers["referrer-policy"] == "strict-origin"
    del browser.headers["Origin"]
    assert browser.post("/login", data={"email": "unknown@example.com"},
                        headers={"Referer": "https://testserver/"}).status_code == 200
    for headers in [
        {"Origin": "null", "Referer": "https://testserver/"},
        {"Origin": "https://evil.example", "Referer": "https://testserver/"},
        {"Referer": "https://evil.example/"},
        {},
    ]:
        assert browser.post("/login", data={"email": "unknown@example.com"}, headers=headers).status_code == 403


def test_magic_link_lifecycle(env, monkeypatch):
    deliveries = []
    async def deliver(email, token):
        deliveries.append((email, token))
    monkeypatch.setattr(main, "deliver_link", deliver)
    browser = client()
    known = browser.post("/login", data={"email": " Alice@EXAMPLE.com "})
    unknown = browser.post("/login", data={"email": "nobody@example.com"})
    assert known.text == unknown.text
    assert len(deliveries) == 1
    token = deliveries[0][1]
    assert token not in main.AUTH_FILE.read_text()
    assert browser.get("/auth/verify?token=" + token).status_code == 200
    assert main.digest(token) in main.auth_data()["links"]
    result = browser.post("/auth/verify", data={"token": token})
    assert result.headers["location"] == "/dashboard"
    cookie = result.headers["set-cookie"]
    assert all(flag in cookie for flag in ("HttpOnly", "Secure", "SameSite=lax"))
    assert "ALICE_PRIVATE" in browser.get("/dashboard").text
    assert "déjà utilisé" in browser.post("/auth/verify", data={"token": token}).text
    browser.post("/logout")
    assert browser.get("/dashboard").headers["location"] == "/login"


def test_expiry_rate_limit_and_removed_user(env, monkeypatch):
    data = main.auth_data()
    data["links"][main.digest("expired")] = {"email": "alice@example.com", "expires": time.time() - 1}
    main.write_json(main.AUTH_FILE, data)
    browser = client()
    assert "Lien invalide" in browser.post("/auth/verify", data={"token": "expired"}).text
    calls = []
    async def deliver(email, token):
        calls.append(token)
    monkeypatch.setattr(main, "deliver_link", deliver)
    for _ in range(5):
        browser.post("/login", data={"email": "alice@example.com"})
    assert len(calls) == 3
    alice = client("alice@example.com")
    config = main.load_config()
    config["users"] = [u for u in config["users"] if u["email"] != "alice@example.com"]
    main.save_config(config)
    assert alice.get("/dashboard").headers["location"] == "/login"


def test_oauth_session_binding(env):
    alice, bob = client("alice@example.com"), client("bob@example.com")
    response = alice.get("/oauth/login/google?target_field=oauth2_token1")
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["redirect_uri"] == ["https://testserver/oauth/callback"]
    assert "code_challenge" in params
    state = params["state"][0]
    assert bob.get("/oauth/callback?state=" + state + "&code=fake").status_code == 400
    assert main.digest(state) in main.auth_data()["oauth"]
    assert alice.get("/oauth/callback?state=" + state + "&error=access_denied").status_code == 400
    assert main.digest(state) not in main.auth_data()["oauth"]
    assert alice.get("/oauth/login/google?target_field=evil").status_code == 400
    assert alice.get("/oauth/login/evil?target_field=oauth2_token1").status_code == 400


def test_legacy_migration(env):
    main.write_json(main.CONFIG_FILE, {"accounts": [{"id": 42, "label": "Legacy"}], "poll_interval": 5, "oauth_apps": {}})
    config = main.load_config()
    assert config["accounts"][0]["owner"] == "admin@example.com"
    assert main.CONFIG_FILE.with_suffix(".pre-saas.json").exists()
    assert main.load_config() == config


def test_execution_preserves_concurrent_changes_and_redacts(env, monkeypatch):
    account = main.load_config()["accounts"][0]
    async def create(*args, **kwargs):
        assert "--nolog" in args and "--delete1" not in args
        config = main.load_config()
        config["accounts"].append({"id": "concurrent", "owner": "bob@example.com"})
        main.save_config(config)
        class Process:
            returncode = 0
            stdout = type("Output", (), {"read": AsyncMock(side_effect=[b"Copied 3 messages\nsecret\npassword hidden\n", b""])})()
            wait = AsyncMock()
        return Process()
    monkeypatch.setattr(main.asyncio, "create_subprocess_exec", create)
    main.processes["a"] = {"owner": account["owner"], "process": None, "cancelled": False}
    asyncio.run(main.execute(account, {"pseudo": "Alice"}))
    config = main.load_config()
    assert any(a["id"] == "concurrent" for a in config["accounts"])
    run = config["runs"][-1]
    assert run["owner"] == "alice@example.com" and run["actor"] == "Alice"
    assert run["status"] == "Succès"
    assert "Copied 3 messages" in run["log"]
    assert "secret" not in run["log"] and "password" not in run["log"]
    assert client("bob@example.com").get("/logs/" + run["id"]).status_code == 404


def test_stop_only_selected_account(env):
    main.processes.update(a={"owner": "alice@example.com", "process": None, "cancelled": False},
                          b={"owner": "bob@example.com", "process": None, "cancelled": False})
    assert client("alice@example.com").post("/account/stop/a").status_code == 303
    assert main.processes["a"]["cancelled"]
    assert not main.processes["b"]["cancelled"]
    assert client("alice@example.com").post("/cgi-bin/imapsync", data={"extra": "--any-option"}).status_code == 400


def test_expired_session_and_restart_persistence(env):
    alice = client("alice@example.com")
    restored = TestClient(main.app, base_url="https://testserver", follow_redirects=False)
    restored.cookies.update(alice.cookies)
    assert restored.get("/dashboard").status_code == 200
    data = main.auth_data()
    for session in data["sessions"].values():
        session["expires"] = time.time() - 1
    main.write_json(main.AUTH_FILE, data)
    assert restored.get("/dashboard").headers["location"] == "/login"


def test_smtp_failure_revokes_link(env, monkeypatch):
    data = main.auth_data()
    data["links"][main.digest("token")] = {"email": "alice@example.com", "expires": time.time() + 100}
    main.write_json(main.AUTH_FILE, data)
    def fail(*args):
        raise OSError("SMTP unavailable")
    monkeypatch.setattr(main, "send_magic_link", fail)
    asyncio.run(main.deliver_link("alice@example.com", "token"))
    assert main.digest("token") not in main.auth_data()["links"]


def test_smtp_reuses_backend_settings(env, monkeypatch):
    from unittest.mock import MagicMock
    for key, value in {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587", "SMTP_FROM": "sender@example.com", "SMTP_USER": "sender", "SMTP_PASS": "smtp-secret", "SMTP_SECURITY": "starttls"}.items():
        monkeypatch.setenv(key, value)
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    factory = MagicMock(return_value=smtp)
    monkeypatch.setattr(main.smtplib, "SMTP", factory)
    main.send_magic_link("alice@example.com", "opaque-token")
    factory.assert_called_once_with("smtp.example.com", 587, timeout=15)
    smtp.starttls.assert_called_once()
    smtp.login.assert_called_once_with("sender", "smtp-secret")
    message = smtp.send_message.call_args.args[0]
    assert message["To"] == "alice@example.com"
    assert "https://testserver/auth/verify?token=opaque-token" in message.get_content()


def test_oauth_success_escapes_tokens_and_consumes_state(env, monkeypatch):
    from unittest.mock import MagicMock
    alice = client("alice@example.com")
    response = alice.get("/oauth/login/google?target_field=oauth2_token1")
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    reply = MagicMock()
    reply.json.return_value = {"access_token": "</script><script>bad</script>", "refresh_token": "refresh"}
    exchange = MagicMock(return_value=reply)
    monkeypatch.setattr(main.requests, "post", exchange)
    result = alice.get("/oauth/callback", params={"state": state, "code": "code"})
    assert result.status_code == 200
    assert "</script><script>bad</script>" not in result.text
    assert "code_verifier" in exchange.call_args.kwargs["data"]
    assert alice.get("/oauth/callback", params={"state": state, "code": "code"}).status_code == 400
