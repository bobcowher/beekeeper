// static/js/pipeline.js — Pipeline page: info, run view with step chain and log, history.
(function () {
    const header = document.getElementById('pipeline-header');
    if (!header) return;
    const NAME = header.dataset.name;
    const API = `/api/v1/pipelines/${encodeURIComponent(NAME)}`;
    const $ = id => document.getElementById(id);

    let runs = [];
    let selectedRun = null;  // run_id being viewed; null = latest
    // Step whose detail is shown; only ever set by a click, so the panel starts closed.
    let selectedStep = null; // {runId, name}
    let shownLog = null;     // {runId, name, status, text} last log fetched into #step-log
    let definition = { steps: [] };
    let refreshInFlight = null;
    let refreshAgain = false;
    let pollTimer = null;
    let polling = true;

    function errorMessage(data, fallback) {
        return data && data.error && data.error.message ? data.error.message : fallback;
    }

    function clearPageError(source = null) {
        const el = $('pipeline-page-error');
        if (source && el.dataset.source !== source) return;
        el.textContent = '';
        el.hidden = true;
        delete el.dataset.source;
    }

    function showPageError(message, source = 'action') {
        if (!message) {
            clearPageError();
            return;
        }
        const el = $('pipeline-page-error');
        el.textContent = message;
        el.dataset.source = source;
        el.hidden = false;
    }

    async function api(url, opts = {}) {
        let response;
        try {
            response = await fetch(url, opts);
        } catch (error) {
            throw new Error(`Network request failed: ${error.message}`);
        }
        let data;
        try {
            data = await response.json();
        } catch (error) {
            throw new Error(`Server returned an invalid response (HTTP ${response.status})`);
        }
        if (!response.ok && data.success !== false) {
            throw new Error(`Request failed (HTTP ${response.status})`);
        }
        return data;
    }

    function duration(start, end) {
        if (!start) return '';
        const secs = ((end ? new Date(end) : new Date()) - new Date(start)) / 1000;
        const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = Math.floor(secs % 60);
        return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
    }

    function statusClass(status) {
        return String(status || 'pending').replace(/[^a-z0-9_-]/gi, '') || 'pending';
    }

    function textElement(tag, className, text) {
        const el = document.createElement(tag);
        if (className) el.className = className;
        el.textContent = text;
        return el;
    }

    function badge(status) {
        return textElement('span', `status-badge status-${statusClass(status)}`, status);
    }

    function stepDefinition(name) {
        return (definition.steps || []).find(step => step.name === name) || {};
    }

    function viewedRun() {
        return selectedRun ? runs.find(run => run.run_id === selectedRun) : runs[0];
    }

    // ── Step selection ──────────────────────────────────────────────────────

    // Drop a selection that no longer points at a step of the run being viewed.
    function resolveSelectedStep(run) {
        if (!selectedStep) return;
        const runId = run ? run.run_id : null;
        const steps = run ? run.steps : definition.steps;
        if (selectedStep.runId !== runId || !steps.some(s => s.name === selectedStep.name)) {
            selectedStep = null;
        }
    }

    // Clicking the open step again closes it.
    function chooseStep(runId, name) {
        const same = selectedStep && selectedStep.runId === runId && selectedStep.name === name;
        selectedStep = same ? null : { runId, name };
        render();
    }

    // ── Rendering ───────────────────────────────────────────────────────────

    function stepDetailLine(step) {
        const def = stepDefinition(step.name);
        if (step.type === 'train') {
            const branch = def.branch ? ` @ ${def.branch}` : '';
            return textElement('div', 'step-detail', `${step.project || def.project || ''}${branch}`);
        }
        const line = textElement('div', 'step-detail', '');
        const code = textElement('code', '', def.command || '');
        code.title = def.command || '';
        line.appendChild(code);
        return line;
    }

    function renderChain(run) {
        const chain = $('step-chain');
        chain.replaceChildren();
        const steps = run ? run.steps : definition.steps.map((step, index) => ({
            ...step, index, status: 'notrun',
        }));
        steps.forEach(step => {
            const li = document.createElement('li');
            const isSelected = selectedStep && selectedStep.name === step.name &&
                selectedStep.runId === (run ? run.run_id : null);
            li.className = `step-node step-${statusClass(step.status)}${isSelected ? ' selected' : ''}`;

            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'step-card';
            button.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
            button.setAttribute('aria-controls', 'step-panel');
            if (step.type === 'task') button.dataset.stepLog = step.name;
            button.addEventListener('click', () => chooseStep(run ? run.run_id : null, step.name));

            const name = textElement('div', 'step-name', step.name);
            name.append(' ', textElement('span', 'step-type', step.type));
            button.appendChild(name);
            button.appendChild(stepDetailLine(step));

            if (run) {
                const statusLine = textElement('div', 'step-status-line', '');
                statusLine.appendChild(badge(step.status));
                const took = duration(step.started_at, step.ended_at);
                if (took) statusLine.append(' ', textElement('span', 'muted', took));
                if (Number(step.attempt) > 1) {
                    statusLine.append(' ', textElement('span', 'muted', `attempt ${step.attempt}`));
                }
                button.appendChild(statusLine);
                if (step.error) button.appendChild(textElement('div', 'pipeline-error', step.error));
            }
            li.appendChild(button);
            chain.appendChild(li);
        });
    }

    function renderPanel(run) {
        const panel = $('step-panel');
        const body = $('step-panel-body');
        const pre = $('step-log');
        body.replaceChildren();
        $('step-hint').hidden = Boolean(selectedStep) || !definition.steps.length;
        if (!selectedStep) {
            panel.hidden = true;
            return;
        }
        const step = run ? run.steps.find(s => s.name === selectedStep.name)
            : definition.steps.find(s => s.name === selectedStep.name);
        if (!step) {
            panel.hidden = true;
            return;
        }
        panel.hidden = false;
        const def = stepDefinition(step.name);
        $('step-panel-title').textContent = step.name;
        $('step-panel-note').textContent = step.type === 'task'
            ? (step.has_log ? (step.status === 'running' ? 'log · live' : 'log') : '')
            : `train · ${step.project || def.project || ''}`;

        if (step.type === 'task' && run && step.has_log) {
            pre.hidden = false;
            return;   // the log itself is filled by loadLog()
        }
        pre.hidden = true;
        if (step.type === 'task') {
            const command = textElement('pre', 'step-command', def.command || '');
            body.appendChild(textElement('div', 'muted', run ? 'Not started yet. Command:' : 'Command:'));
            body.appendChild(command);
            return;
        }
        if (step.run_id) {
            const p = textElement('p', '', 'Training run ');
            const link = document.createElement('a');
            link.href = `/projects/${encodeURIComponent(step.project)}/history/${step.run_id}/log`;
            link.textContent = step.run_ref;
            p.appendChild(link);
            p.append(step.status === 'running' ? ' is running.' : ` ${step.status}.`);
            body.appendChild(p);
        } else if (step.status === 'queued') {
            body.appendChild(textElement('p', '', `Waiting in ${step.project}'s queue` +
                (step.wait_reason ? `: ${step.wait_reason}` : '.')));
        } else {
            const branch = def.branch ? ` on branch ${def.branch}` : '';
            body.appendChild(textElement('p', 'muted',
                `${run ? 'Not started yet. ' : ''}Queues a training run on ${def.project || step.project}${branch}.`));
        }
        const env = def.env_vars || {};
        if (Object.keys(env).length) {
            body.appendChild(textElement('pre', 'step-command',
                Object.entries(env).map(([k, v]) => `${k}=${v}`).join('\n')));
        }
    }

    async function loadLog(run) {
        const pre = $('step-log');
        if (!run || !selectedStep || pre.hidden) return;
        const step = run.steps.find(s => s.name === selectedStep.name);
        if (!step || !step.has_log) return;
        const same = shownLog && shownLog.runId === run.run_id && shownLog.name === step.name;
        // A finished log doesn't change; only refetch a live one.
        if (same && shownLog.status === step.status && step.status !== 'running') {
            if (pre.textContent !== shownLog.text) pre.textContent = shownLog.text;
            return;
        }
        if (!same) pre.textContent = 'Loading log…';
        pre.classList.remove('log-error');
        try {
            const data = await api(
                `${API}/runs/${run.run_id}/steps/${encodeURIComponent(step.name)}/log?tail=500`
            );
            if (!data.success) throw new Error(errorMessage(data, 'Could not load step log'));
            // The selection may have moved while the request was in flight.
            if (!selectedStep || selectedStep.name !== step.name || selectedStep.runId !== run.run_id) return;
            const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
            const keepScroll = same ? pre.scrollTop : null;
            pre.textContent = data.data.log;
            shownLog = { runId: run.run_id, name: step.name, status: step.status, text: data.data.log };
            if (!same || atBottom) pre.scrollTop = pre.scrollHeight;
            else pre.scrollTop = keepScroll;
        } catch (error) {
            // Shown in the panel it belongs to; refetched on the next poll.
            shownLog = null;
            pre.textContent = `Could not load log: ${error.message}`;
            pre.classList.add('log-error');
        }
    }

    function renderHeader(run) {
        const latest = runs[0];
        $('btn-pipeline-start').hidden = Boolean(latest && latest.status === 'running');
        $('btn-pipeline-cancel').hidden = !(run && run.status === 'running');
        $('btn-pipeline-resume').hidden = !(
            run && latest && run.run_id === latest.run_id && ['failed', 'cancelled'].includes(run.status)
        );
        const status = $('pipeline-run-status');
        status.textContent = latest ? latest.status : 'never run';
        status.className = `status-badge status-${statusClass(latest ? latest.status : 'notrun')}`;
    }

    function setInfo(id, text) {
        const el = $(id);
        el.replaceChildren();
        if (text) el.textContent = text;
        else el.appendChild(textElement('span', 'muted', 'none'));
    }

    function renderInfo() {
        const steps = definition.steps || [];
        const trains = steps.filter(s => s.type === 'train').length;
        setInfo('info-description', definition.description);
        setInfo('info-folder', definition.folder);
        const kinds = [[trains, 'train'], [steps.length - trains, 'task']].filter(([n]) => n)
            .map(([n, kind]) => `${n} ${kind}`).join(', ');
        setInfo('info-steps', steps.length ? `${steps.length} (${kinds})` : '');
        setInfo('info-projects', [...new Set(steps.filter(s => s.project).map(s => s.project))].join(', '));
        setInfo('info-runs', String(runs.length));
    }

    function renderSummary(run) {
        const isLatest = !run || run.run_id === runs[0].run_id;
        $('run-title').textContent = run ? `Run #${run.run_id}${isLatest ? '' : ' (older run)'}` : 'Run';
        $('btn-show-latest').hidden = !run || isLatest;
        const summary = $('run-summary');
        summary.replaceChildren();
        if (!run) {
            summary.appendChild(textElement('span', 'muted',
                'Never run. Steps run in order, left to right.'));
            $('pipeline-run-error').textContent = '';
            return;
        }
        const done = run.steps.filter(s => s.status === 'succeeded').length;
        const parts = [
            `started ${new Date(run.started_at).toLocaleString()}`,
            run.status === 'running' ? `running for ${duration(run.started_at)}` : duration(run.started_at, run.ended_at),
            `${done} of ${run.steps.length} steps succeeded`,
        ];
        summary.appendChild(textElement('span', '', parts.join(' · ')));
        $('pipeline-run-error').textContent = run.error || '';
    }

    function renderHistory(run) {
        const body = $('pipeline-runs').querySelector('tbody');
        body.replaceChildren();
        $('pipeline-runs-empty').hidden = runs.length > 0;
        $('pipeline-runs').hidden = runs.length === 0;
        runs.forEach(item => {
            const stepName = (item.steps[item.current_step] || {}).name || '';
            let progress = '';
            if (item.status === 'running' && stepName) progress = `Running: ${stepName}`;
            if (['failed', 'cancelled'].includes(item.status) && stepName) progress = `Stopped at ${stepName}`;
            if (item.status === 'succeeded') progress = `${item.steps.length}/${item.steps.length} steps`;

            const row = document.createElement('tr');
            row.dataset.run = item.run_id;
            if (run && item.run_id === run.run_id) row.classList.add('selected');
            const runCell = document.createElement('td');
            const runButton = textElement('button', 'run-history-link', `#${item.run_id}`);
            runButton.type = 'button';
            runButton.setAttribute('aria-label', `Show pipeline run #${item.run_id}`);
            runCell.appendChild(runButton);
            const statusCell = document.createElement('td');
            statusCell.appendChild(badge(item.status));
            [
                runCell,
                statusCell,
                textElement('td', '', new Date(item.started_at).toLocaleString()),
                textElement('td', '', duration(item.started_at, item.ended_at)),
                textElement('td', '', progress),
            ].forEach(cell => row.appendChild(cell));
            row.addEventListener('click', () => selectRun(item.run_id));
            body.appendChild(row);
        });
    }

    function render() {
        const run = viewedRun();
        resolveSelectedStep(run);
        renderHeader(run);
        renderInfo();
        renderSummary(run);
        renderChain(run);
        renderPanel(run);
        renderHistory(run);
        return loadLog(run);
    }

    function selectRun(runId) {
        selectedRun = runs[0] && runId === runs[0].run_id ? null : runId;
        selectedStep = null;
        render();
        $('run-card').scrollIntoView({ block: 'nearest' });
    }

    // ── Data ────────────────────────────────────────────────────────────────

    async function refreshOnce() {
        const [def, list] = await Promise.all([api(API), api(`${API}/runs?limit=50`)]);
        if (!def.success) throw new Error(errorMessage(def, 'Could not load pipeline definition'));
        if (!list.success) throw new Error(errorMessage(list, 'Could not load pipeline runs'));
        definition = def.data.pipeline;
        runs = list.data.runs;
        if (selectedRun && !runs.some(run => run.run_id === selectedRun)) selectedRun = null;
        clearPageError('refresh');
        await render();
    }

    function refresh() {
        if (refreshInFlight) {
            refreshAgain = true;
            return refreshInFlight;
        }
        refreshInFlight = refreshOnce()
            .catch(error => showPageError(`Could not refresh pipeline: ${error.message}`, 'refresh'))
            .finally(() => {
                refreshInFlight = null;
                if (refreshAgain && polling) {
                    refreshAgain = false;
                    refresh();
                }
            });
        return refreshInFlight;
    }

    async function action(url, label, button) {
        button.disabled = true;
        try {
            const data = await api(url, { method: 'POST' });
            if (!data.success) throw new Error(errorMessage(data, `Could not ${label}`));
            selectedRun = null;
            selectedStep = null;
            showPageError();
            await refresh();
        } catch (error) {
            showPageError(`Could not ${label}: ${error.message}`);
        } finally {
            button.disabled = false;
        }
    }

    $('btn-pipeline-start').addEventListener('click', event => {
        action(`${API}/runs`, 'start pipeline', event.currentTarget);
    });
    $('btn-pipeline-cancel').addEventListener('click', event => {
        const run = viewedRun();
        if (run && confirm(`Cancel ${run.run_ref}?`)) {
            action(`${API}/runs/${run.run_id}/cancel`, 'cancel pipeline', event.currentTarget);
        }
    });
    $('btn-pipeline-resume').addEventListener('click', event => {
        const run = runs[0];
        if (run) action(`${API}/runs/${run.run_id}/resume`, 'resume pipeline', event.currentTarget);
    });
    $('btn-show-latest').addEventListener('click', () => selectRun(runs[0].run_id));
    $('btn-step-close').addEventListener('click', () => {
        selectedStep = null;
        render();
    });
    $('btn-pipeline-delete').addEventListener('click', async event => {
        if (!confirm(`Delete pipeline ${NAME} and its run history? Training runs are kept.`)) return;
        const button = event.currentTarget;
        button.disabled = true;
        try {
            const data = await api(API, { method: 'DELETE' });
            if (!data.success) throw new Error(errorMessage(data, 'Delete failed'));
            location.href = '/pipelines';
        } catch (error) {
            showPageError(`Could not delete pipeline: ${error.message}`);
            button.disabled = false;
        }
    });

    // ── Polling ─────────────────────────────────────────────────────────────

    function clearPoll() {
        if (pollTimer !== null) window.clearTimeout(pollTimer);
        pollTimer = null;
    }

    // Poll faster while a run is live so the chain and log keep up with the engine.
    function schedulePoll() {
        clearPoll();
        if (!polling || document.hidden) return;
        const live = runs[0] && runs[0].status === 'running';
        pollTimer = window.setTimeout(async () => {
            await refresh();
            schedulePoll();
        }, live ? 2000 : 5000);
    }

    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            clearPoll();
        } else {
            refresh().finally(schedulePoll);
        }
    });
    window.addEventListener('pagehide', () => {
        polling = false;
        clearPoll();
    });
    window.addEventListener('pageshow', event => {
        if (event.persisted) {
            polling = true;
            refresh().finally(schedulePoll);
        }
    });

    refresh().finally(schedulePoll);
})();
