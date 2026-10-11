// Beekeeper — recent dashboard activity, refreshed while the tab is visible.
(function () {
    const runningEl = document.getElementById('running-activity');
    const queuedEl = document.getElementById('queued-activity');
    const recentEl = document.getElementById('recent-activity');
    const queuedCard = document.getElementById('queued-activity-card');
    const initialEl = document.getElementById('initial-activity');
    if (!runningEl || !queuedEl || !recentEl || !initialEl) return;

    let timer = null;
    let polling = true;

    function duration(seconds) {
        if (seconds === null || seconds === undefined) return '—';
        const value = Math.max(0, Math.floor(seconds));
        const h = Math.floor(value / 3600);
        const m = Math.floor((value % 3600) / 60);
        const s = value % 60;
        return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
    }

    function text(tag, className, value) {
        const el = document.createElement(tag);
        if (className) el.className = className;
        el.textContent = value;
        return el;
    }

    function empty(container, message) {
        container.replaceChildren(text('p', 'muted activity-empty', message));
    }

    function activityRow(item, detail, status) {
        const link = document.createElement('a');
        link.className = 'activity-row';
        link.href = item.url;
        const identity = document.createElement('span');
        identity.className = 'activity-identity';
        identity.appendChild(text('span', 'activity-kind', item.kind === 'pipeline' ? 'Pipeline' : 'Training'));
        identity.appendChild(text('span', 'activity-name', item.run_ref));
        link.appendChild(identity);
        link.appendChild(text('span', 'activity-detail muted', detail));
        if (status) link.appendChild(text('span', `status-badge status-${status}`, status));
        return link;
    }

    function render(data) {
        runningEl.replaceChildren();
        if (!data.running.length) {
            empty(runningEl, 'Nothing is running.');
        } else {
            data.running.forEach(item => {
                const detail = item.kind === 'pipeline'
                    ? `${item.current_step ? `Step: ${item.current_step} · ` : ''}${duration(item.elapsed)}`
                    : `${item.branch || 'default branch'} · ${duration(item.elapsed)}`;
                runningEl.appendChild(activityRow(item, detail, item.status));
            });
        }

        queuedEl.replaceChildren();
        queuedCard.hidden = data.queued.length === 0;
        data.queued.forEach(item => {
            const row = activityRow(
                {kind: 'training', run_ref: item.project, url: item.url},
                `${item.branch || 'default branch'} · position ${item.position}`,
                'queued'
            );
            queuedEl.appendChild(row);
        });

        recentEl.replaceChildren();
        if (!data.recent.length) {
            empty(recentEl, 'No finished runs yet.');
        } else {
            data.recent.forEach(item => {
                const ended = item.timestamp ? new Date(item.timestamp).toLocaleString() : '';
                recentEl.appendChild(activityRow(item, `${duration(item.duration)} · ${ended}`, item.status));
            });
        }
    }

    async function refresh() {
        if (!polling || document.hidden) return;
        try {
            const response = await fetch('/dashboard/activity');
            if (response.ok) render(await response.json());
        } catch (error) {
            console.warn('Could not refresh dashboard activity:', error);
        }
    }

    function schedule() {
        if (timer !== null) window.clearTimeout(timer);
        if (!polling || document.hidden) return;
        timer = window.setTimeout(async () => {
            await refresh();
            schedule();
        }, 5000);
    }

    document.addEventListener('visibilitychange', () => {
        if (document.hidden && timer !== null) {
            window.clearTimeout(timer);
            timer = null;
        } else if (!document.hidden) {
            refresh().finally(schedule);
        }
    });
    window.addEventListener('pagehide', () => {
        polling = false;
        if (timer !== null) window.clearTimeout(timer);
    });
    window.addEventListener('pageshow', event => {
        if (event.persisted) {
            polling = true;
            refresh().finally(schedule);
        }
    });

    render(JSON.parse(initialEl.textContent));
    schedule();
})();
