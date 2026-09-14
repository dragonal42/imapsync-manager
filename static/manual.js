const manualForm = document.getElementById('manual-form');
const runButton = document.getElementById('bt-sync');
const aiButton = document.getElementById('bt-ai');
const sendersButton = document.getElementById('bt-senders');
const copySendersButton = document.getElementById('bt-copy-senders');
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
        document.getElementById('manual-status').textContent = result.sender_export && result.finished && result.status === 'Succès' ? `Extraction terminée : ${result.sender_count} adresses uniques. Copiez la liste ci-dessous.` : result.status;
        copySendersButton.hidden = !(result.sender_export && result.finished && result.status === 'Succès' && result.sender_count > 0);
        const link = document.getElementById('manual-log');
        link.href = '/logs/' + encodeURIComponent(runId); link.hidden = false;
        runButton.disabled = aiButton.disabled = sendersButton.disabled = !result.finished; stopButton.disabled = result.finished;
        if (result.finished) {
            sessionStorage.removeItem('imapsync-manual-run'); runId = null;
            showToast('Exécution : ' + result.status, result.status === 'Succès' ? 'success' : 'info');
        } else setTimeout(pollManual, 1500);
    } catch (error) {
        runButton.disabled = aiButton.disabled = sendersButton.disabled = false; stopButton.disabled = true;
        await Swal.fire({title: 'Suivi interrompu', text: error.message, icon: 'error'});
    } finally { polling = false; }
}
async function startManual(aiOnly, sendersOnly = false) {
    if (runButton.disabled) return;
    if (aiOnly) {
        for (const name of (sendersOnly ? ['host1', 'user1'] : ['host1', 'user1', 'source_folder', 'nPeriodeJours'])) {
            const field = manualForm.elements[name];
            if (!field.value.trim() || !field.reportValidity()) {
                await Swal.fire({title: 'Champ requis', text: 'Vérifiez la source, le dossier et la période.', icon: 'error'});
                field.focus(); return;
            }
        }
    }
    runButton.disabled = aiButton.disabled = sendersButton.disabled = true;
    try {
        if (!sendersOnly && (aiOnly ? manualForm.elements.ai_mode.value === 'move' : manualForm.elements.delete1.checked || manualForm.elements.delete2.checked)) {
            const result = await Swal.fire({title: aiOnly ? 'Confirmer le tri IA' : 'Confirmer les suppressions de messages', text: aiOnly ? 'Les messages détectés seront déplacés vers les quarantaines de la source. Aucun transfert vers la destination.' : 'Les options choisies peuvent supprimer définitivement des emails. Vérifiez les boîtes source et destination.', icon: 'warning', showCancelButton: true, confirmButtonText: 'Lancer avec ces options', cancelButtonText: 'Annuler', focusCancel: true});
            if (!result.isConfirmed) { runButton.disabled = aiButton.disabled = sendersButton.disabled = false; return; }
        }
        const body = new FormData(manualForm);
        if (aiOnly) {
            body.set('action', sendersOnly ? 'senders' : 'ai');
            for (const name of ['host2', 'user2', 'password2', 'oauth2_token2', 'delete1', 'delete2', 'dry', 'justlogin', 'justfolders', 'justfoldersizes']) body.delete(name);
        }
        copySendersButton.hidden = true;
        document.getElementById('output').textContent = '';
        document.getElementById('manual-status').textContent = 'Lancement en cours…';
        const response = await apiPost(manualForm.action, body);
        const result = await response.json();
        runId = result.run_id; sessionStorage.setItem('imapsync-manual-run', runId);
        stopButton.disabled = false;
        showToast(result.message);
        setTimeout(pollManual, 250);
    } catch (error) { runButton.disabled = aiButton.disabled = sendersButton.disabled = false; document.getElementById('manual-status').textContent = 'Lancement impossible'; await Swal.fire({title: 'Lancement impossible', text: error.message, icon: 'error'}); }
}
manualForm.addEventListener('submit', event => {
    event.preventDefault(); startManual(false);
});
aiButton.addEventListener('click', () => startManual(true));
sendersButton.addEventListener('click', () => startManual(true, true));
copySendersButton.addEventListener('click', async () => {
    try {
        await navigator.clipboard.writeText(document.getElementById('output').textContent);
        showToast('Adresses copiées');
    } catch (_) {
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(document.getElementById('output'));
        selection.removeAllRanges(); selection.addRange(range);
        showToast('Liste sélectionnée : utilisez Copier ou Ctrl+C', 'info');
    }
});
stopButton.addEventListener('click', async () => {
    if (!runId) return;
    stopButton.disabled = true;
    try {
        const body = new FormData(); body.set('abort', 'on'); body.set('run_id', runId);
        await apiPost('/cgi-bin/imapsync', body); showToast('Arrêt demandé', 'info');
    } catch (error) { stopButton.disabled = false; await Swal.fire({title: 'Arrêt impossible', text: error.message, icon: 'error'}); }
});
if (runId) { runButton.disabled = aiButton.disabled = sendersButton.disabled = true; pollManual(); }
