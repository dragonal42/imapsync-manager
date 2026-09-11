(() => {
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
    document.querySelectorAll('.sender-delete').forEach(button => button.addEventListener('click', async () => {
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
    }));
})();
