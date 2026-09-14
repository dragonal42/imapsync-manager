"""Optional isolated UI test: python tests/browser_sender_export.py (Playwright/Edge)."""
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from test_tenants import client


def check():
    with tempfile.TemporaryDirectory() as directory:
        main.CONFIG_FILE = Path(directory) / 'config.json'
        main.AUTH_FILE = Path(directory) / 'auth.json'
        main.ADMIN_EMAIL = 'admin@example.com'
        main.PUBLIC_URL = 'https://testserver'
        main.load_config()
        backend = client(main.ADMIN_EMAIL)
        calls = []
        async def extract(account, whitelist, active, progress):
            calls.append(account['exclude_whitelist'])
            assert 'host2' not in account
            progress('Lecture en cours')
            return ['a@example.com', 'b@example.com'], 12
        main.extract_senders = extract
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, **({'channel': 'msedge'} if sys.platform == 'win32' else {}))
            context = browser.new_context()
            def serve(route):
                request = route.request
                response = backend.request(request.method, request.url, headers=request.all_headers(),
                                           content=request.post_data_buffer, follow_redirects=True)
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
            context.route('**/*', serve)
            page = context.new_page()
            page.goto(main.PUBLIC_URL + '/manual')
            page.wait_for_load_state('networkidle')
            for width in (320, 390, 1280):
                page.set_viewport_size({'width': width, 'height': 900})
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
            page.locator('[name="host1"]').fill('imap.example.com')
            page.locator('[name="user1"]').fill('alice@example.com')
            page.locator('[name="password1"]').fill('test-secret')
            assert not page.locator('#bt-copy-senders').is_visible()
            for excluded in (True, False):
                page.locator('[name="exclude_whitelist"]').set_checked(excluded)
                page.get_by_role('button', name='Extraire les expéditeurs', exact=True).click()
                page.wait_for_function("!document.getElementById('bt-copy-senders').hidden")
                assert page.locator('#output').inner_text() == 'a@example.com;\nb@example.com'
                assert '2 adresses uniques' in page.locator('#manual-status').inner_text()
            assert calls == [True, False]
            page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => {window.copiedSenders = text;}}})")
            page.get_by_role('button', name='Copier les adresses', exact=True).click()
            page.wait_for_function("window.copiedSenders === 'a@example.com;\\nb@example.com'")
            browser.close()
    print('Sender export browser test passed: source only, both filter modes, copy, mobile widths.')


if __name__ == '__main__':
    check()
