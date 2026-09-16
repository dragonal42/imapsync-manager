"""Isolated personal AI settings UI test; no provider requests."""
import sys
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import main
from test_tenants import client


def check():
    with tempfile.TemporaryDirectory() as directory:
        main.CONFIG_FILE=Path(directory)/'config.json'
        main.AUTH_FILE=Path(directory)/'auth.json'
        main.ADMIN_EMAIL='admin@example.com'
        main.PUBLIC_URL='https://testserver'
        config=main.load_config()
        config['users'].append({'email':'alice@example.com','pseudo':'Alice','role':'user'})
        main.save_config(config)
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,**({'channel':'msedge'} if sys.platform=='win32' else {}))
            backend=client('alice@example.com')
            backend.post('/sender-lists/import',data={'kind':'whitelist','emails':'unique@example.org;other@example.org'})
            context=browser.new_context()
            def serve(route):
                request=route.request
                response=backend.request(request.method,request.url,headers=request.all_headers(),content=request.post_data_buffer,follow_redirects=True)
                route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.content)
            context.route('**/*',serve)
            page=context.new_page()
            page.goto(main.PUBLIC_URL+'/sender-lists')
            page.wait_for_load_state('networkidle')
            page.evaluate('window.noReloadMarker = 42')
            assert page.locator('.sender-delete').count()==0
            field=page.locator('[name=q]')
            field.fill('uni')
            page.wait_for_function("document.querySelectorAll('.sender-delete').length === 1")
            assert page.evaluate('window.noReloadMarker')==42
            assert page.locator('[data-sender-list=whitelist] .sender-addresses').inner_text().startswith('unique@example.org')
            page.locator('.sender-delete').click()
            page.wait_for_function("document.querySelectorAll('.sender-delete').length === 0")
            assert page.locator('[data-sender-list=whitelist] [data-total]').inner_text()=='1'
            field.fill('oth')
            page.wait_for_function("document.querySelectorAll('.sender-delete').length === 1")
            field.fill('ot')
            assert page.locator('.sender-delete').count()==0
            page.locator('#sender-search-clear').click()
            assert field.input_value()=='' and page.evaluate('window.noReloadMarker')==42
            context.close()
            browser.close()
    print('AJAX sender search passed: no reload, threshold, clear, dynamic deletion and totals.')

if __name__=='__main__': check()

