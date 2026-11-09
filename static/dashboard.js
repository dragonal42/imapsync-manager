(() => {
    const label = status => status === 'Succès' ? 'OK' : status;
    const poll = async () => {
        try {
            const response = await fetch('/api/task-status');
            if (!response.ok || response.redirected) return;
            const data = await response.json();
            document.querySelectorAll('[data-account-status]').forEach(element => {
                const account = data.accounts.find(a => a.id === element.dataset.accountStatus);
                if (account) {
                    element.textContent = label(account.status);
                    element.parentElement.querySelector('small').textContent = account.last_run;
                }
            });
            document.querySelectorAll('[data-execution-controls]').forEach(group => {
                const account = data.accounts.find(a => a.id === group.dataset.executionControls);
                if (!account) return;
                group.dataset.running = String(account.running);
                group.querySelector('[data-task-run]').hidden = account.running;
                group.querySelector('[data-task-stop]').hidden = !account.running;
                group.querySelector('.execution-running').hidden = !account.running;
            });
            for (const element of document.querySelectorAll('[data-running-log]')) {
                const result = await fetch('/api/logs/' + encodeURIComponent(element.dataset.runningLog));
                if (!result.ok || result.redirected) continue;
                const run = await result.json();
                element.textContent = label(run.status) + ' — Voir le journal';
                if (run.finished) delete element.dataset.runningLog;
            }
        } catch (_) { /* Retry after temporary network failures. */ }
        finally { setTimeout(poll, 5000); }
    };
    setTimeout(poll, 1000);
})();
