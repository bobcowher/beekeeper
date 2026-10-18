// Beekeeper — run activity. The dashboard's Now card polls what's running or queued;
// the History page lists finished runs grouped by day, a page at a time.
(function () {
    const nowEl = document.getElementById('activity-now');
    const historyEl = document.getElementById('history-list');

    const OK = new Set(['completed', 'succeeded']);
    const BAD = new Set(['failed', 'crashed', 'lost']);


    function duration(seconds) {
        if (seconds === null || seconds === undefined) return '—';
        const value = Math.max(0, Math.floor(seconds));
        const h = Math.floor(value / 3600);
        const m = Math.floor((value % 3600) / 60);
        const s = value % 60;
        if (h) return `${h}h ${m}m`;
        if (m) return `${m}m ${s}s`;
        return `${s}s`;
    }

    function money(value) {
        return '$' + value.toFixed(value < 1 ? 4 : 2);
    }

    function el(tag, className, value) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (value !== undefined) node.textContent = value;
        return node;
    }

    function outcome(status) {
        if (OK.has(status)) return 'ok';
        if (BAD.has(status)) return 'bad';
        return 'neutral';
    }

    function where(info) {
        if (!info) return null;
        return el('span', info.remote ? 'activity-where remote' : 'activity-where', info.label);
    }

    const STEP_MARK = {succeeded: '✓', failed: '✕', running: '●', queued: '◌', cancelled: '■'};

    function chain(steps) {
        const box = el('span', 'activity-chain');
        steps.forEach((step, i) => {
            if (i) box.appendChild(el('span', 'activity-arrow', '→'));
            const chip = el('span', `activity-step step-${step.status}`);
            if (STEP_MARK[step.status]) chip.appendChild(el('span', 'activity-step-mark', STEP_MARK[step.status]));
            chip.appendChild(document.createTextNode(step.name));
            const loc = where(step.where);
            if (loc) chip.appendChild(loc);
            box.appendChild(chip);
        });
        return box;
    }

    function nameLine(item, ...extras) {
        const line = el('span', 'activity-line');
        line.appendChild(el('span', 'activity-name', item.run_ref));
        extras.filter(Boolean).forEach(extra => line.appendChild(extra));
        return line;
    }

    function row(className, href, mark, main, right) {
        const link = el('a', className);
        link.href = href;
        link.appendChild(mark);
        link.appendChild(main);
        link.appendChild(right);
        return link;
    }

    function rightCol(top, bottom) {
        const box = el('span', 'activity-right');
        box.appendChild(el('span', 'activity-when', top));
        if (bottom) box.appendChild(el('span', null, bottom));
        return box;
    }

    function liveRow(item) {
        const main = el('span', 'activity-main');
        if (item.kind === 'pipeline') {
            main.appendChild(nameLine(item));
            main.appendChild(chain(item.steps || []));
        } else {
            main.appendChild(nameLine(item, where(item.where), item.branch ? el('span', 'activity-meta', item.branch) : null));
        }
        const started = item.status === 'starting' ? 'starting' : `running ${duration(item.elapsed)}`;
        const cost = item.cost ? `${money(item.cost)} so far` : '';
        return row(`activity-row live activity-${item.kind}`, item.url, el('span', 'activity-pulse'), main,
                   rightCol(started, cost));
    }

    // A queue entry a pipeline created is shown as that pipeline's run, with the settings
    // (env vars) that tell its experiments apart.
    function queuedRow(item) {
        const env = Object.entries(item.env || {}).map(([key, value]) => `${key}=${value}`).join(' ');
        const parts = [`${item.branch || 'default branch'}`, `position ${item.position}`];
        if (item.pipeline_ref) parts.unshift(item.project);
        if (env) parts.push(env);
        const identity = item.pipeline_ref ? {run_ref: item.pipeline_ref} : {run_ref: item.project};
        const main = el('span', 'activity-main');
        main.appendChild(nameLine(identity, el('span', 'activity-meta', parts.join(' · '))));
        return row(`activity-row queued activity-${item.pipeline_ref ? 'pipeline' : 'training'}`,
                   item.pipeline_ref ? item.pipeline_url : item.url,
                   el('span', 'activity-pulse'), main, rightCol('queued'));
    }

    function recentRow(item) {
        const state = outcome(item.status);
        const main = el('span', 'activity-main');
        if (item.kind === 'pipeline') {
            main.appendChild(nameLine(item));
            if (item.steps && item.steps.length) main.appendChild(chain(item.steps));
            if (item.error) main.appendChild(el('span', 'activity-error', item.error));
        } else {
            const meta = state === 'ok' ? item.branch : item.status;
            main.appendChild(nameLine(item, where(item.where), meta ? el('span', 'activity-meta', meta) : null));
        }
        const mark = el('span', `activity-mark ${state}`, state === 'ok' ? '✓' : state === 'bad' ? '✕' : '■');
        mark.title = item.status;
        const time = item.timestamp ? new Date(item.timestamp).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}) : '';
        const detail = [duration(item.duration), item.cost ? money(item.cost) : ''].filter(Boolean).join(' · ');
        return row(`activity-row activity-${item.kind}${state === 'bad' ? ' bad' : ''}`, item.url, mark, main,
                   rightCol(time, detail));
    }

    function dayLabel(date) {
        const today = new Date();
        const startOf = d => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
        const days = Math.round((startOf(today) - startOf(date)) / 86400000);
        if (days === 0) return 'Today';
        if (days === 1) return 'Yesterday';
        const opts = {weekday: 'short', month: 'short', day: 'numeric'};
        if (date.getFullYear() !== today.getFullYear()) opts.year = 'numeric';
        return date.toLocaleDateString([], opts);
    }

    function dayHeader(label, items) {
        const failed = items.filter(item => outcome(item.status) === 'bad').length;
        const spend = items.reduce((sum, item) => sum + (item.cost || 0), 0);
        const parts = [`${items.length} run${items.length === 1 ? '' : 's'}`];
        if (failed) parts.push(`${failed} failed`);
        if (spend) parts.push(`${money(spend)} Runpod`);
        const head = el('div', 'activity-day');
        head.appendChild(el('span', null, label));
        head.appendChild(el('span', null, parts.join(' · ')));
        return head;
    }

    function renderNow(data) {
        nowEl.replaceChildren();
        if (!data.running.length && !data.queued.length) {
            const idle = el('div', 'activity-idle');
            idle.appendChild(el('span', 'activity-idle-dot'));
            idle.appendChild(document.createTextNode('Nothing running or queued.'));
            nowEl.appendChild(idle);
            return;
        }
        data.running.forEach(item => nowEl.appendChild(liveRow(item)));
        data.queued.forEach(item => nowEl.appendChild(queuedRow(item)));
    }

    function renderHistory(items, filter) {
        historyEl.replaceChildren();
        if (!items.length) {
            historyEl.appendChild(el('p', 'muted activity-empty',
                filter === 'all' ? 'No finished runs yet.' : 'Nothing matches this filter.'));
            return;
        }
        const days = new Map();
        items.forEach(item => {
            const label = item.timestamp ? dayLabel(new Date(item.timestamp)) : 'Earlier';
            if (!days.has(label)) days.set(label, []);
            days.get(label).push(item);
        });
        days.forEach((dayItems, label) => {
            historyEl.appendChild(dayHeader(label, dayItems));
            dayItems.forEach(item => historyEl.appendChild(recentRow(item)));
        });
    }

    function initNow() {
        const initialEl = document.getElementById('initial-activity');
        let timer = null;
        let polling = true;

        async function refresh() {
            if (!polling || document.hidden) return;
            try {
                const response = await fetch('/dashboard/activity');
                if (response.ok) renderNow(await response.json());
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
                void refresh().finally(schedule);
            }
        });
        window.addEventListener('pagehide', () => {
            polling = false;
            if (timer !== null) window.clearTimeout(timer);
        });
        window.addEventListener('pageshow', event => {
            if (event.persisted) {
                polling = true;
                void refresh().finally(schedule);
            }
        });

        renderNow(JSON.parse(initialEl.textContent));
        schedule();
    }

    function initHistory() {
        const initial = JSON.parse(document.getElementById('initial-history').textContent);
        const moreButton = document.getElementById('history-more');
        const filterButtons = document.querySelectorAll('.activity-filters button');
        let filter = initial.filter;
        let items = initial.page.items;
        let next = initial.page.next;
        let request = 0;   // a newer filter click wins over an older page still loading

        function show() {
            renderHistory(items, filter);
            moreButton.hidden = !next;
        }

        async function load(before) {
            const mine = ++request;
            const params = new URLSearchParams({filter});
            if (before) params.set('before', before);
            moreButton.disabled = true;
            try {
                const response = await fetch(`/history/data?${params}`);
                if (!response.ok || mine !== request) return;
                const page = await response.json();
                items = before ? items.concat(page.items) : page.items;
                next = page.next;
                show();
            } catch (error) {
                console.warn('Could not load history:', error);
            } finally {
                moreButton.disabled = false;
            }
        }

        filterButtons.forEach(button => {
            button.addEventListener('click', () => {
                filter = button.dataset.filter;
                filterButtons.forEach(b => b.setAttribute('aria-pressed', String(b === button)));
                const url = new URL(window.location.href);
                if (filter === 'all') url.searchParams.delete('filter');
                else url.searchParams.set('filter', filter);
                window.history.replaceState(null, '', url);
                void load(null);
            });
        });
        moreButton.addEventListener('click', () => void load(next));
        show();
    }

    if (nowEl) initNow();
    if (historyEl) initHistory();
})();
