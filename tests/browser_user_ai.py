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
            for email,owner in [('alice@example.com','alice@example.com'),('admin@example.com','alice@example.com')]:
                backend=client(email)
                context=browser.new_context()
                def serve(route):
                    request=route.request
                    response=backend.request(request.method,request.url,headers=request.all_headers(),content=request.post_data_buffer,follow_redirects=True)
                    route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.content)
                context.route('**/*',serve)
                page=context.new_page()
                page.goto(main.PUBLIC_URL+'/ai/settings?owner='+owner)
                page.wait_for_load_state('networkidle')
                assert 'Alice' in page.locator('h1').inner_text()
                for width in (320,390,1280):
                    page.set_viewport_size({'width':width,'height':900})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                page.locator('[name=sApiKeyMistral]').fill('browser-test-secret')
                page.locator('[name=mistral_model]').fill('personal-model')
                page.get_by_role('button',name='Enregistrer les paramètres IA').click()
                page.wait_for_function("document.querySelector('[name=sApiKeyMistral]').value === ''")
                assert main.user_ai_settings(main.load_config(),owner)['mistral_model']=='personal-model'
                assert 'browser-test-secret' not in page.content()
                assert 'sApiKeyMistral' not in main.user_ai_settings(main.load_config(),'admin@example.com')
                context.close()
            browser.close()
    print('Personal AI settings browser test passed: self/admin edit, secrets hidden, mobile layout.')

if __name__=='__main__': check()
