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
        document.getElementById('manual-status').textContent = result.sender_export && result.finished && result.status === 'Succès' ? `Extraction terminée — dossier ${result.resolved_sent_folder || "Envoyés"} : ${result.scanned_messages} messages examinés${result.sent_total_messages != null ? ` sur ${result.sent_total_messages} présents` : ""} ; ${result.sender_count} adresses uniques après déduplication et filtre liste blanche. Copiez la liste ci-dessous.` : result.status;
        copySendersButton.hidden = !(result.sender_export && result.finished && result.status === 'Succès' && result.sender_count > 0);
        const link = document.getElementById('manual-log');
        link.href = '/logs/' + encodeURIComponent(runId); link.hidden = result.retained === false;
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
        for (const name of (sendersOnly ? ['host1', 'user1', 'sent_folder', 'sent_limit'] : ['host1', 'user1', 'source_folder', 'nPeriodeJours'])) {
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

const folderButton = document.getElementById('bt-folders');
const folderList = document.getElementById('folder-list');
const folderStatus = document.getElementById('folder-list-status');
const folderActions = document.getElementById('folder-list-actions');
folderButton.addEventListener('click', async () => {
    const body = new FormData();
    const saved = manualForm.elements.saved_source;
    if (saved) {
        body.set('saved_source', saved.value);
    } else {
        const credential = manualForm.elements.authmech1.value === 'XOAUTH2' ? 'oauth2_token1' : 'password1';
        for (const name of ['host1', 'user1', 'authmech1', credential]) {
            body.set(name, manualForm.elements[name].value);
        }
    }
    folderButton.disabled = true;
    folderList.replaceChildren();
    folderActions.hidden = true;
    folderStatus.textContent = 'Lecture des dossiers source…';
    try {
        const {folders} = await (await apiPost('/manual/folders', body)).json();
        for (const folder of folders) {
            const label = document.createElement('label');
            label.className = 'check';
            const input = document.createElement('input');
            input.type = 'checkbox';
            input.value = '=' + folder;
            input.disabled = [...input.value].length > 200;
            if (input.disabled) label.title = 'Ce chemin dépasse la longueur autorisée pour une exclusion.';
            label.append(input, document.createTextNode(folder));
            folderList.append(label);
        }
        folderStatus.textContent = folders.length ? folders.length + ' dossier(s) sélectionnable(s).' : 'Aucun dossier sélectionnable.';
        folderActions.hidden = !folders.length;
    } catch (error) {
        folderStatus.textContent = error.message;
    } finally {
        folderButton.disabled = false;
    }
});
function selectedFolderExclusions() {
    const rules = [...folderList.querySelectorAll('input:checked')].map(input => input.value);
    if (!rules.length) showToast('Sélectionnez au moins un dossier.', 'info');
    return rules;
}
document.getElementById('bt-copy-folders').addEventListener('click', async () => {
    const rules = selectedFolderExclusions();
    if (!rules.length) return;
    try {
        await navigator.clipboard.writeText(rules.join('\n'));
        showToast('Exclusions copiées');
    } catch (_) {
        await Swal.fire({title: 'Exclusions à copier', input: 'textarea', inputValue: rules.join('\n'),
                         inputAttributes: {readonly: 'readonly'}, confirmButtonText: 'Fermer'});
    }
});
document.getElementById('bt-add-folders').addEventListener('click', () => {
    const rules = selectedFolderExclusions();
    if (!rules.length) return;
    const field = manualForm.elements.folder_exclusions;
    const combined = [...new Set([...field.value.split(/\r?\n/).map(s => s.trim()).filter(Boolean), ...rules])];
    if (combined.length > 40 || combined.join('\n').length > 8000) {
        showToast('La liste accepte au plus 40 motifs et 8 000 caractères.', 'error');
        return;
    }
    field.value = combined.join('\n');
    field.focus();
    showToast('Exclusions ajoutées au transfert manuel');
});
