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
            backend=client('admin@example.com')
            context=browser.new_context()
            def serve(route):
                request=route.request
                response=backend.request(request.method,request.url,headers=request.all_headers(),content=request.post_data_buffer,follow_redirects=True)
                route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.content)
            context.route('**/*',serve)
            page=context.new_page()
            page.goto(main.PUBLIC_URL+'/admin')
            page.wait_for_load_state('networkidle')
            page.get_by_text('Alice — alice@example.com — Actif',exact=True).click()
            panel=page.locator('#users details').filter(has_text='alice@example.com')
            panel.locator('[name=pseudo]').fill('Alice modifiée')
            panel.get_by_role('button',name='Enregistrer les modifications').click()
            page.get_by_text('Alice modifiée — alice@example.com — Actif',exact=True).wait_for()
            page.get_by_text('Alice modifiée — alice@example.com — Actif',exact=True).click()
            panel.get_by_role('button',name='Désactiver',exact=True).click()
            page.get_by_text('Alice modifiée — alice@example.com — Désactivé',exact=True).wait_for()
            page.get_by_text('Alice modifiée — alice@example.com — Désactivé',exact=True).click()
            panel.get_by_role('button',name='Supprimer l’utilisateur et ses paramètres').click()
            page.locator('.swal2-confirm').click()
            page.wait_for_function("!document.querySelector('#users').textContent.includes('alice@example.com')")
            assert not any(u['email']=='alice@example.com' for u in main.load_config()['users'])
            context.close()
            browser.close()
    print('Admin user management UI passed: edit, disable and confirmed deletion.')

if __name__=='__main__': check()
