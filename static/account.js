(() => {
    const sync = document.getElementById('bActiverSynchro');
    const ai = document.getElementById('bPretraitementIA');
    const update = () => {
        document.getElementById('side2').disabled = !sync.checked;
        document.getElementById('delete1').disabled = !sync.checked;
        document.getElementById('ai-options').hidden = !ai.checked;
        document.querySelectorAll('#ai-options input, #ai-options select').forEach(input => { input.disabled = !ai.checked; });
    };
    sync.addEventListener('change', update);
    ai.addEventListener('change', update);
    update();
})();
