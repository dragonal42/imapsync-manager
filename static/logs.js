(() => {
    const log = document.getElementById('run-log');
    if (log.dataset.active !== '1') return;
    const poll = async () => {
        try {
            const response = await fetch('/api/logs/' + encodeURIComponent(log.dataset.runId));
            if (!response.ok || response.redirected) return;
            const run = await response.json();
            log.textContent = run.log || 'Exécution en cours.';
            document.getElementById('run-status').textContent = run.status === 'Succès' ? 'OK' : run.status;
            if (run.finished) return;
        } catch (_) { /* A temporary connection failure will be retried. */ }
        setTimeout(poll, 3000);
    };
    poll();
})();
