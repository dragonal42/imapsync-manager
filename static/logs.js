(() => {
    const log = document.getElementById('run-log');
    const follow = document.getElementById('follow-run-log');
    function scrollToEnd() {
        if (follow.checked) log.scrollTop = log.scrollHeight;
    }
    follow.addEventListener('change', scrollToEnd);
    scrollToEnd();
    if (log.dataset.active !== '1') return;
    const poll = async () => {
        try {
            const response = await fetch('/api/logs/' + encodeURIComponent(log.dataset.runId));
            if (!response.ok || response.redirected) return;
            const run = await response.json();
            const previousTop = log.scrollTop;
            log.textContent = run.log || 'Exécution en cours.';
            if (follow.checked) scrollToEnd();
            else log.scrollTop = previousTop;
            document.getElementById('run-status').textContent = run.status === 'Succès' ? 'OK' : run.status;
            if (run.finished) return;
        } catch (_) { /* A temporary connection failure will be retried. */ }
        setTimeout(poll, 3000);
    };
    poll();
})();
