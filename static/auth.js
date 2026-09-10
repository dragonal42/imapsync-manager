/* Authentication controls shared by saved configurations and manual runs. */
(() => {
    const pending = new Map();
    const status = (target, text, kind) => {
        const element = document.getElementById('status_' + target);
        element.textContent = text;
        element.className = 'oauth-status oauth-' + kind;
    };
    document.querySelectorAll('[data-auth-side]').forEach(select => {
        const side = select.dataset.authSide;
        const target = 'oauth2_token' + side;
        const update = () => {
            const oauth = select.value === 'XOAUTH2';
            const password = document.querySelector('[name="pass' + side + '"], [name="password' + side + '"]');
            password.disabled = oauth;
            document.querySelectorAll('.oauth[data-target="' + target + '"]').forEach(button => { button.disabled = !oauth; });
            [target, 'refresh_' + target, 'provider_' + target].forEach(id => { document.getElementById(id).disabled = !oauth; });
            document.getElementById('status_' + target).hidden = !oauth;
            if (!oauth && pending.has(target)) {
                pending.get(target).close(); pending.delete(target);
            }
        };
        select.addEventListener('change', update);
        update();
    });
    document.querySelectorAll('.oauth').forEach(button => button.addEventListener('click', () => {
        if (button.disabled) return;
        const target = button.dataset.target;
        if (pending.has(target)) pending.get(target).close();
        const popup = window.open('/oauth/login/' + button.dataset.provider + '?target_field=' + target,
            'OAuth_' + target, 'width=550,height=700');
        if (!popup) { status(target, 'Fenêtre bloquée. Autorisez les fenêtres de connexion et réessayez avant d’enregistrer.', 'error'); return; }
        pending.set(target, popup);
        status(target, 'Connexion en cours…', 'pending');
        const timer = setInterval(() => {
            if (pending.get(target) !== popup) { clearInterval(timer); return; }
            if (popup.closed) {
                pending.delete(target); clearInterval(timer);
                status(target, 'Connexion interrompue. Recommencez avant d’enregistrer.', 'error');
            }
        }, 500);
    }));
    window.addEventListener('message', event => {
        if (event.origin !== window.location.origin || !event.data || event.data.type !== 'imapsync-oauth') return;
        const entry = [...pending].find(([, popup]) => popup === event.source);
        if (!entry) return;
        const [target] = entry;
        const data = event.data;
        if (data.target && data.target !== target) return;
        pending.delete(target);
        if (data.error) { status(target, 'Échec de connexion. ' + data.error, 'error'); return; }
        if (!data.tokens || !data.tokens.access_token || !data.tokens.refresh_token) {
            status(target, 'Réponse OAuth incomplète. Recommencez avant d’enregistrer.', 'error'); return;
        }
        document.getElementById(target).value = data.tokens.access_token;
        document.getElementById('refresh_' + target).value = data.tokens.refresh_token;
        document.getElementById('provider_' + target).value = data.provider;
        status(target, document.getElementById('manual-form')
            ? 'Connexion réussie. Vous pouvez lancer la synchronisation !'
            : 'Connexion réussie. Vous pouvez enregistrer votre configuration !', 'success');
    });
})();
