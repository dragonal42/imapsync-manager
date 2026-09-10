"""Optional browser regression: pip install playwright; playwright install chromium.

Run from the repository root: python tests/browser_magic_link.py
Uses an isolated browser and temporary storage; no network, SMTP or IMAP calls.
"""
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
sys.stdout.reconfigure(encoding='utf-8')


def check():
    with tempfile.TemporaryDirectory() as directory:
        main.CONFIG_FILE = Path(directory) / "config.json"
        main.AUTH_FILE = Path(directory) / "auth.json"
        main.PUBLIC_URL = "https://testserver"
        main.ADMIN_EMAIL = "admin@example.com"
        deliveries = []

        async def deliver(email, token):
            deliveries.append(token)

        main.deliver_link = deliver
        client = TestClient(main.app, base_url=main.PUBLIC_URL, follow_redirects=False)
        observed = []
        policy = None

        def serve(route):
            request = route.request
            if request.method == "POST":
                observed.append((request.url, request.all_headers().get("origin")))
            # Do not let TestClient's cookie jar mask a browser cookie failure.
            client.cookies.clear()
            response = client.request(request.method, request.url,
                                      headers=request.all_headers(), content=request.post_data_buffer,
                                      follow_redirects=not request.is_navigation_request())
            headers = dict(response.headers)
            if policy:
                headers["referrer-policy"] = policy
            route.fulfill(status=response.status_code, headers=headers, body=response.content)

        with sync_playwright() as playwright:
            options = {"headless": True}
            if sys.platform == "win32":
                options["channel"] = os.getenv("BROWSER_CHANNEL", "msedge")
            browser = playwright.chromium.launch(**options)
            context = browser.new_context()
            context.route("**/*", serve)
            page = context.new_page()
            page.on('pageerror', lambda error: print('Browser error:', error))
            page.on('requestfailed', lambda request: print('Request failed:', request.url, request.failure, flush=True))

            # Demonstrate the original failure with the browser-generated header.
            policy = "no-referrer"
            page.goto(main.PUBLIC_URL + "/login")
            page.get_by_label("Email", exact=True).fill("admin@example.com")
            page.get_by_role("button", name="Recevoir mon lien de connexion").click()
            page.wait_for_load_state()
            assert "Origine de la requête refusée" in page.content()
            assert observed[-1][1] == "null"
            assert not deliveries

            # Exercise the application's actual policy, delivery and secure cookie.
            policy = None
            page.goto(main.PUBLIC_URL + "/login")
            page.get_by_label("Email", exact=True).fill("admin@example.com")
            page.get_by_role("button", name="Recevoir mon lien de connexion").click()
            page.wait_for_load_state()
            assert "un lien de connexion vous sera envoyé" in page.content()
            assert observed[-1][1] == main.PUBLIC_URL
            assert len(deliveries) == 1
            page.goto(main.PUBLIC_URL + "/auth/verify?token=" + deliveries[0])
            page.get_by_role("button", name="Confirmer ma connexion").click()
            page.wait_for_url(main.PUBLIC_URL + "/dashboard")
            assert "Erreurs aujourd’hui" in page.content()
            page.get_by_role("link", name="Administration", exact=True).click()
            assert "Créer un utilisateur" in page.content()
            cookie = next(c for c in context.cookies() if c["name"] == main.COOKIE)
            assert cookie["secure"] and cookie["httpOnly"]

            # Dashboard controls, confirmation cancellation, filtering and manual UI.
            config = main.load_config()
            stamp = datetime.now(timezone.utc).isoformat()
            config["accounts"].append({"id": "sample", "owner": main.ADMIN_EMAIL, "label": "Compte de test", "host1": "imap.example.com", "host2": "imap.example.com", "user1": "source@example.com", "user2": "dest@example.com", "status": "Succès", "last_run": stamp, "schedule_state": "RUNNING"})
            config["runs"].append({"id": "sample-run", "account_id": "sample", "owner": main.ADMIN_EMAIL, "label": "Compte de test", "actor": "Test", "status": "Succès", "started": stamp, "log": "Messages copiés"})
            main.save_config(config)
            page.get_by_role("link", name="Tableau de bord", exact=True).click()
            row = page.locator('#configurations tbody tr').first
            assert row.get_by_role('link', name='Modifier').evaluate("el => getComputedStyle(el).backgroundColor") == 'rgb(192, 82, 5)'
            row.get_by_role('button', name='Mettre en pause').click()
            try:
                page.wait_for_function("document.querySelector('.schedule-paused') !== null", timeout=10000)
            except Exception:
                print('Last requests:', observed[-3:])
                print('Page:', page.locator('body').inner_text())
                raise
            assert main.load_config()['accounts'][0]['schedule_state'] == 'PAUSED'
            row.get_by_role('button', name='Supprimer', exact=True).click()
            page.get_by_role('button', name='Annuler', exact=True).click()
            assert len(main.load_config()['accounts']) == 1
            row.get_by_role('button', name='Vider les logs', exact=True).click()
            page.locator('.swal2-confirm').click()
            page.wait_for_function("document.querySelector('#historique tbody').textContent.includes('Aucune exécution')")
            assert main.load_config()['runs'] == []
            page.get_by_role('link', name='Synchronisation manuelle', exact=True).click()
            page.get_by_label('Serveur IMAP source', exact=True).fill('imap.example.com')
            page.get_by_label('Serveur IMAP destination', exact=True).fill('imap.example.com')
            page.get_by_label('Identifiant source', exact=True).fill('source@example.com')
            page.get_by_label('Identifiant destination', exact=True).fill('destination@example.com')
            page.get_by_label('Mot de passe source', exact=True).fill('test-secret')
            page.get_by_label('Mot de passe destination', exact=True).fill('test-secret')
            class Process:
                returncode = 0
                stdout = type('Output', (), {'read': AsyncMock(side_effect=[b'Copied 3 messages\n', b''])})()
                wait = AsyncMock()
            main.asyncio.create_subprocess_exec = AsyncMock(return_value=Process())
            page.get_by_role('button', name='Lancer la synchronisation', exact=True).click()
            page.wait_for_function("document.getElementById('manual-status').textContent === 'Succès'")
            assert 'Copied 3 messages' in page.locator('#output').inner_text()
            page.get_by_role('link', name='Tableau de bord', exact=True).click()
            page.locator('#historique tbody tr').first.get_by_role('button', name='Supprimer', exact=True).click()
            page.locator('.swal2-confirm').click()
            page.wait_for_function("document.querySelector('#historique tbody').textContent.includes('Aucune exécution')")
            page.locator('#configurations tbody tr').first.get_by_role('button', name='Supprimer', exact=True).click()
            page.locator('.swal2-confirm').click()
            page.wait_for_function("document.querySelector('#configurations tbody').textContent.includes('Aucune configuration')")
            assert main.load_config()['accounts'] == []
            page.get_by_role("button", name="Déconnexion").click()
            page.wait_for_url(main.PUBLIC_URL + "/")
            assert not any(c["name"] == main.COOKIE for c in context.cookies())
            browser.close()
        print("Browser regression passed: original Origin:null reproduced; login, verification and logout work.")


if __name__ == "__main__":
    check()
