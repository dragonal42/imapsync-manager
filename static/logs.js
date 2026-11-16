(() => {
    const log = document.getElementById('run-log');
    const follow = document.getElementById('follow-run-log');
    const status = document.getElementById('run-status');
    let frame;
    function followEnd() {
        cancelAnimationFrame(frame);
        if (!follow.checked) return;
        frame = requestAnimationFrame(() => {
            if (follow.checked) log.scrollTop = log.scrollHeight - log.clientHeight;
        });
    }
    follow.addEventListener('change', followEnd);
    window.addEventListener('pageshow', followEnd);
    const resize = new ResizeObserver(followEnd);
    resize.observe(log);
    followEnd();
    if (log.dataset.active !== '1') return;

    const poll = async () => {
        try {
            const response = await fetch('/api/logs/' + encodeURIComponent(log.dataset.runId), {cache: 'no-store'});
            if (response.redirected) return;
            if (response.ok) {
                const run = await response.json();
                const previousTop = log.scrollTop;
                log.textContent = run.log || 'Exécution en cours.';
                if (follow.checked) followEnd();
                else log.scrollTop = previousTop;
                status.textContent = run.status === 'Succès' ? 'OK' : run.status;
                if (run.finished) return;
            } else if ([401, 403, 404].includes(response.status)) {
                return;
            }
        } catch (_) { /* Retry temporary connection failures. */ }
        setTimeout(poll, 3000);
    };
    poll();
})();
