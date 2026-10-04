"""Isolated admin service UI smoke test; no external provider or mail calls."""
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
        main.save_config(main.load_config())
        sent = []
        main.test_imap_connection = lambda account, token: None
        main.send_verdict_smtp = lambda mail, settings: sent.append((mail, settings))
        backend = client(main.ADMIN_EMAIL)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, **({'channel': 'msedge'} if sys.platform == 'win32' else {}))
            context = browser.new_context()
            def serve(route):
                req = route.request
                response = backend.request(req.method, req.url, headers=req.all_headers(), content=req.post_data_buffer, follow_redirects=True)
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
            context.route('**/*', serve)
            page = context.new_page()
            page.goto(main.PUBLIC_URL + '/admin/mail-verdict')
            page.wait_for_load_state('networkidle')
            assert page.locator('[name=simulation]').is_checked()
            assert page.locator('#pass1').is_enabled()
            assert page.locator('.oauth').first.is_disabled()
            page.locator('[name=authmech1]').select_option('XOAUTH2')
            assert page.locator('#pass1').is_disabled()
            assert page.locator('.oauth').first.is_enabled()
            page.locator('[name=authmech1]').select_option('PLAIN')
            for width in (320, 390, 1280):
                page.set_viewport_size({'width': width, 'height': 900})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.locator('[name=host1]').fill('imap.example.org')
            page.locator('[name=user1]').fill('test@example.org')
            page.locator('#pass1').fill('test-secret')
            page.locator('[name=smtp_host]').fill('smtp.example.org')
            page.locator('[name=smtp_from]').fill('test@example.org')
            page.locator('[name=smtp_user]').fill('test@example.org')
            page.locator('[name=smtp_password]').fill('smtp-secret')
            page.locator('[data-service-test=imap]').click()
            page.get_by_text('Test IMAP réussi', exact=True).wait_for()
            page.get_by_role('button', name='OK', exact=True).click()
            page.locator('[data-service-test=smtp]').click()
            page.get_by_text('Test SMTP réussi', exact=True).wait_for()
            page.get_by_role('button', name='OK', exact=True).click()
            assert sent[0][0]['From'] == 'test@example.org'
            assert not main.load_config().get('mail_verdict_service')
            page.get_by_role('button', name='Enregistrer', exact=False).click()
            page.wait_for_url('**/dashboard')
            assert main.load_config()['mail_verdict_service']['schedule_state'] == 'PAUSED'
            assert page.evaluate('document.querySelector("#mail-verdict-service").getBoundingClientRect().top < document.querySelector("#configurations").getBoundingClientRect().top')
            page.locator('#mail-verdict-service').get_by_role('button', name='Activer', exact=True).click()
            page.wait_for_function('document.querySelector("#mail-verdict-service .schedule-running") !== null')
            page.locator('#mail-verdict-service').get_by_role('button', name='Mettre en pause').click()
            page.wait_for_function('document.querySelector("#mail-verdict-service .schedule-paused") !== null')
            config = main.load_config()
            config['mail_verdict_service'].update(status='Succès', last_run='2026-10-02T12:34:56+00:00')
            main.save_config(config)
            page.wait_for_function('document.querySelector("#mail-verdict-service small").textContent.includes("14:34:56")')
            assert page.locator('[data-account-status="admin-mail-verdict"]').inner_text() == 'OK'
            assert 'test-secret' not in page.content() and 'smtp-secret' not in page.content()
            context.close()
            browser.close()
    print('Admin service browser test passed: IMAP/SMTP test popups, defaults, OAuth controls, mobile layout, placement, save and pause.')


if __name__ == '__main__':
    check()
