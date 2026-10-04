(() => {
    const form = document.querySelector('form[action="/admin/mail-verdict/save"]');
    const source = form.elements.user1;
    const sender = form.elements.smtp_from;
    const login = form.elements.smtp_user;
    const fillDefaults = () => {
        if (!sender.value && source.value.includes('@')) sender.value = source.value.trim();
        if (!login.value) login.value = source.value.trim();
    };
    source.addEventListener('change', fillDefaults);
    document.querySelectorAll('[data-service-test]').forEach(button => button.addEventListener('click', async () => {
        const kind = button.dataset.serviceTest;
        const buttons = [...document.querySelectorAll('[data-service-test]')];
        buttons.forEach(item => { item.disabled = true; });
        const original = button.textContent;
        button.textContent = 'Test en cours…';
        try {
            fillDefaults();
            const response = await apiPost('/admin/mail-verdict/test/' + kind, new FormData(form));
            const result = await response.json();
            await Swal.fire({title: kind === 'imap' ? 'Test IMAP réussi' : 'Test SMTP réussi', text: result.message, icon: 'success'});
        } catch (error) {
            await Swal.fire({title: kind === 'imap' ? 'Échec du test IMAP' : 'Échec du test SMTP', text: error.message, icon: 'error'});
        } finally {
            button.textContent = original;
            buttons.forEach(item => { item.disabled = false; });
        }
    }));
})();
