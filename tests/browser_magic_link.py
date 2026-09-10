"""Optional browser regression: pip install playwright; playwright install chromium.

Run from the repository root: python tests/browser_magic_link.py
Uses an isolated browser and temporary storage; no network, SMTP or IMAP calls.
"""
import os
from pathlib import Path
import sys
import tempfile

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


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
                                      headers=request.all_headers(), content=request.post_data_buffer)
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
            page.wait_for_url(main.PUBLIC_URL + "/admin")
            assert "Créer un utilisateur" in page.content()
            cookie = next(c for c in context.cookies() if c["name"] == main.COOKIE)
            assert cookie["secure"] and cookie["httpOnly"]
            page.get_by_role("button", name="Déconnexion").click()
            page.wait_for_url(main.PUBLIC_URL + "/")
            assert not any(c["name"] == main.COOKIE for c in context.cookies())
            browser.close()
        print("Browser regression passed: original Origin:null reproduced; login, verification and logout work.")


if __name__ == "__main__":
    check()
