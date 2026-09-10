const manualForm = document.getElementById('manual-form');
const runButton = document.getElementById('bt-sync');
const stopButton = document.getElementById('bt-abort');
let runId = sessionStorage.getItem('imapsync-manual-run');
let polling = false;
async function pollManual() {
    if (!runId || polling) return;
    polling = true;
    try {
        const response = await fetch('/api/logs/' + encodeURIComponent(runId));
        if (!response.ok || response.redirected) throw new Error('Ce journal est inaccessible. Vérifiez votre connexion.');
        const result = await response.json();
        document.getElementById('output').textContent = result.log;
        document.getElementById('manual-status').textContent = result.status;
        const link = document.getElementById('manual-log');
        link.href = '/logs/' + encodeURIComponent(runId); link.hidden = false;
        runButton.disabled = !result.finished; stopButton.disabled = result.finished;
        if (result.finished) {
            sessionStorage.removeItem('imapsync-manual-run'); runId = null;
            showToast('Synchronisation : ' + result.status, result.status === 'Succès' ? 'success' : 'info');
        } else setTimeout(pollManual, 1500);
    } catch (error) {
        runButton.disabled = false; stopButton.disabled = true;
        await Swal.fire({title: 'Suivi interrompu', text: error.message, icon: 'error'});
    } finally { polling = false; }
}
manualForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (runButton.disabled) return;
    runButton.disabled = true;
    try {
        if (manualForm.elements.delete1.checked || manualForm.elements.delete2.checked) {
            const result = await Swal.fire({title: 'Confirmer les suppressions de messages', text: 'Les options choisies peuvent supprimer définitivement des emails. Vérifiez les boîtes source et destination.', icon: 'warning', showCancelButton: true, confirmButtonText: 'Lancer avec ces options', cancelButtonText: 'Annuler', focusCancel: true});
            if (!result.isConfirmed) { runButton.disabled = false; return; }
        }
        const response = await apiPost(manualForm.action, new FormData(manualForm));
        const result = await response.json();
        runId = result.run_id; sessionStorage.setItem('imapsync-manual-run', runId);
        stopButton.disabled = false;
        showToast(result.message);
        setTimeout(pollManual, 250);
    } catch (error) { runButton.disabled = false; await Swal.fire({title: 'Lancement impossible', text: error.message, icon: 'error'}); }
});
stopButton.addEventListener('click', async () => {
    if (!runId) return;
    stopButton.disabled = true;
    try {
        const body = new FormData(); body.set('abort', 'on'); body.set('run_id', runId);
        await apiPost('/cgi-bin/imapsync', body); showToast('Arrêt demandé', 'info');
    } catch (error) { stopButton.disabled = false; await Swal.fire({title: 'Arrêt impossible', text: error.message, icon: 'error'}); }
});
document.querySelectorAll('.oauth').forEach(button => button.addEventListener('click', () => {
    const side = button.dataset.target.slice(-1);
    document.getElementById('authmech' + side).value = 'XOAUTH2';
    window.open('/oauth/login/' + button.dataset.provider + '?target_field=' + button.dataset.target, 'OAuth', 'width=550,height=700');
}));
if (runId) { runButton.disabled = true; pollManual(); }
