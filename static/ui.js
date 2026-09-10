/* SweetAlert2 is served locally so confirmations do not depend on a CDN. */
window.showToast = (title, icon = 'success') => Swal.fire({toast: true, position: 'top-end', icon, title, showConfirmButton: false, timer: 3500, timerProgressBar: true});
window.apiPost = async (url, body) => {
    const response = await fetch(url, {method: 'POST', body, credentials: 'same-origin'});
    if (response.redirected && new URL(response.url).pathname === '/login') {
        location.assign('/login');
        throw new Error('Votre session a expiré. Reconnectez-vous.');
    }
    if (!response.ok) {
        const text = await response.text();
        let message = text;
        try { message = JSON.parse(text).detail || text; } catch (_) {}
        throw new Error(typeof message === 'string' ? message : 'Requête invalide');
    }
    return response;
};
document.addEventListener('submit', async event => {
    const form = event.target;
    if (!form.matches('.action-form')) return;
    event.preventDefault();
    if (form.dataset.busy) return;
    form.dataset.busy = 'true';
    try {
        if (form.dataset.confirm) {
            const result = await Swal.fire({title: 'Confirmer la suppression', text: form.dataset.confirm, icon: 'warning', showCancelButton: true, confirmButtonText: 'Supprimer', cancelButtonText: 'Annuler', confirmButtonColor: '#9a2934', focusCancel: true});
            if (!result.isConfirmed) return;
        }
        await apiPost(form.action, new FormData(form));
        sessionStorage.setItem('imapsync-toast', form.dataset.toast || 'Modification enregistrée');
        if (form.dataset.redirect) location.assign(form.dataset.redirect);
        else location.reload();
    } catch (error) {
        await Swal.fire({title: 'Action impossible', text: error.message, icon: 'error'});
    } finally { delete form.dataset.busy; }
});
const savedToast = sessionStorage.getItem('imapsync-toast');
if (savedToast) { sessionStorage.removeItem('imapsync-toast'); showToast(savedToast); }
