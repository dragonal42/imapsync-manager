(() => {
    const search = document.getElementById('sender-search');
    const input = search.elements.q;
    const results = document.getElementById('sender-results');
    const status = document.getElementById('sender-search-status');
    let timer, controller, revision = 0;
    function schedule(immediate = false) {
        clearTimeout(timer);
        if (controller) controller.abort();
        const version = ++revision;
        const query = input.value.trim();
        const url = new URL(location.href);
        if (query) url.searchParams.set('q', query); else url.searchParams.delete('q');
        history.replaceState(null, '', url);
        results.removeAttribute('aria-busy');
        if (query.length < 3) {
            results.querySelectorAll('.sender-addresses').forEach(list => list.remove());
            results.querySelectorAll('section').forEach(section => {
                let message = section.querySelector('p');
                if (!message) { message = document.createElement('p'); section.append(message); }
                message.hidden = false;
                message.textContent = 'Adresses masquées. Recherche de 3 caractères minimum.';
            });
            status.textContent = 'Saisissez au moins 3 caractères.';
            return;
        }
        status.textContent = 'Recherche en cours…';
        results.setAttribute('aria-busy', 'true');
        timer = setTimeout(async () => {
            controller = new AbortController();
            try {
                const response = await fetch('/sender-lists/search?' + new URLSearchParams({q: query}), {signal: controller.signal, credentials: 'same-origin'});
                if (response.redirected && new URL(response.url).pathname === '/login') {
                    location.assign('/login'); return;
                }
                if (!response.ok) throw new Error('Recherche impossible. Réessayez.');
                const html = await response.text();
                if (version !== revision) return;
                results.innerHTML = html;
                status.textContent = 'Recherche terminée.';
            } catch (error) {
                if (version === revision && error.name !== 'AbortError') status.textContent = 'Recherche impossible. Réessayez.';
            } finally {
                if (version === revision) results.removeAttribute('aria-busy');
            }
        }, immediate ? 0 : 300);
    }
    input.addEventListener('input', () => schedule());
    search.addEventListener('submit', event => { event.preventDefault(); schedule(true); });
    document.getElementById('sender-search-clear').addEventListener('click', event => {
        event.preventDefault(); input.value = ''; schedule(true); input.focus();
    });
    const form = document.getElementById('sender-import');
    form.addEventListener('submit', async event => {
        event.preventDefault();
        const button = form.querySelector('button');
        if (button.disabled) return;
        button.disabled = true;
        try {
            const payload = new FormData(form);
            const preview = await (await apiPost('/sender-lists/preview', payload)).json();
            const name = payload.get('kind') === 'whitelist' ? 'WhiteList' : 'BlackList';
            const result = await Swal.fire({title: 'Importer dans ' + name + ' ?',
                text: preview.emails.length + ' adresses trouvées ; ' + preview.added + ' à ajouter ; ' + preview.existing + ' déjà présentes.',
                input: 'textarea', inputValue: preview.emails.join('\n'), inputAttributes: {readonly: 'readonly', 'aria-label': 'Adresses à importer'},
                showCancelButton: true, confirmButtonText: 'Confirmer l’import', cancelButtonText: 'Annuler'});
            if (!result.isConfirmed) return;
            payload.set('emails', preview.emails.join('\n'));
            const saved = await (await apiPost('/sender-lists/import', payload)).json();
            sessionStorage.setItem('imapsync-toast', saved.added + ' adresses ajoutées à ' + name);
            location.reload();
        } catch (error) {
            await Swal.fire({title: 'Import impossible', text: error.message, icon: 'error'});
        } finally { button.disabled = false; }
    });
    document.getElementById('sender-results').addEventListener('click', async event => {
        const button = event.target.closest('.sender-delete');
        if (!button || button.disabled) return;
        button.disabled = true;
        try {
            const data = new FormData();
            data.set('kind', button.dataset.kind); data.set('email', button.dataset.email);
            await apiPost('/sender-lists/delete', data);
            const section = button.closest('section');
            button.closest('li').remove();
            section.querySelector('[data-total]').textContent = Math.max(0, Number(section.querySelector('[data-total]').textContent) - 1);
            section.querySelector('[data-empty]').hidden = !!section.querySelector('li');
            showToast('Adresse supprimée');
        } catch (error) { showToast(error.message, 'error'); button.disabled = false; }
    });
})();
