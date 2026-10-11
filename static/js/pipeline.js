// static/js/pipeline.js — Pipeline page: step chain, run controls, YAML editor.
(function () {
    const header = document.getElementById('pipeline-header');
    if (!header) return;
    const NAME = header.dataset.name;
    const API = `/api/v1/pipelines/${encodeURIComponent(NAME)}`;
    const $ = id => document.getElementById(id);

    let runs = [];
    let selected = null;   // run_id shown in the chain; null = latest
    let expandedLog = null;
    let definition = { steps: [] };
    let yamlLoaded = false;
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

    async function showStepLog(run, stepName, button) {
        const pre = $('step-log');
        button.disabled = true;
        try {
            const data = await api(
                `${API}/runs/${run.run_id}/steps/${encodeURIComponent(stepName)}/log?tail=500`
            );
            if (!data.success) throw new Error(errorMessage(data, 'Could not load step log'));
            expandedLog = {
                runId: run.run_id,
                stepName,
                content: data.data.log,
                scrollTop: 0,
            };
            pre.hidden = false;
            pre.textContent = expandedLog.content;
            $('step-chain').querySelectorAll('[data-step-log]').forEach(link => {
                link.setAttribute('aria-expanded', 'false');
            });
            button.setAttribute('aria-expanded', 'true');
            showPageError();
        } catch (error) {
            showPageError(`Could not load step log: ${error.message}`);
        } finally {
            button.disabled = false;
        }
    }

    function renderChain(run, pipelineDefinition) {
        const chain = $('step-chain');
        const pre = $('step-log');
        if (expandedLog && !pre.hidden) {
            expandedLog.scrollTop = pre.scrollTop;
            expandedLog.content = pre.textContent;
        }
        chain.replaceChildren();
        const steps = run ? run.steps : pipelineDefinition.steps.map((step, index) => ({
            ...step, index, status: 'pending',
        }));
        steps.forEach(step => {
            const li = document.createElement('li');
            li.className = `step-node step-${statusClass(step.status)}`;

            const name = textElement('div', 'step-name', step.name);
            name.append(' ', textElement('span', 'muted', step.type));
            li.appendChild(name);

            const statusLine = document.createElement('div');
            statusLine.appendChild(textElement(
                'span', `status-badge status-${statusClass(step.status)}`, step.status
            ));
            if (Number(step.attempt) > 1) {
                statusLine.append(' ', textElement('span', 'muted', `attempt ${step.attempt}`));
            }
            li.appendChild(statusLine);
            li.appendChild(textElement('div', 'muted', duration(step.started_at, step.ended_at)));

            const linkLine = document.createElement('div');
            if (step.type === 'train' && step.run_id) {
                const link = document.createElement('a');
                link.href = `/projects/${encodeURIComponent(step.project)}/history/${step.run_id}/log`;
                link.textContent = step.run_ref;
                linkLine.appendChild(link);
            } else if (step.type === 'train') {
                linkLine.appendChild(textElement('span', 'muted', step.project));
            } else if (step.has_log) {
                const isExpanded = expandedLog && run && expandedLog.runId === run.run_id &&
                    expandedLog.stepName === step.name;
                const button = textElement('button', 'link-button', 'log');
                button.type = 'button';
                button.dataset.stepLog = step.name;
                button.setAttribute('aria-controls', 'step-log');
                button.setAttribute('aria-expanded', isExpanded ? 'true' : 'false');
                button.addEventListener('click', () => showStepLog(run, step.name, button));
                linkLine.appendChild(button);
            }
            li.appendChild(linkLine);
            if (step.wait_reason) {
                li.appendChild(textElement('div', 'queue-wait', `waiting: ${step.wait_reason}`));
            }
            if (step.error) li.appendChild(textElement('div', 'pipeline-error', step.error));
            chain.appendChild(li);
        });
        const logStillSelected = expandedLog && run && expandedLog.runId === run.run_id &&
            steps.some(step => step.name === expandedLog.stepName && step.has_log);
        if (logStillSelected) {
            pre.hidden = false;
            pre.textContent = expandedLog.content;
            pre.scrollTop = expandedLog.scrollTop;
        } else {
            pre.hidden = true;
        }
    }

    function renderControls(run) {
        const latest = runs[0];
        $('btn-pipeline-start').hidden = Boolean(latest && latest.status === 'running');
        $('btn-pipeline-cancel').hidden = !(run && run.status === 'running');
        $('btn-pipeline-resume').hidden = !(
            run && latest && run.run_id === latest.run_id && ['failed', 'cancelled'].includes(run.status)
        );
        const badge = $('pipeline-run-status');
        badge.textContent = run ? run.status : 'never run';
        badge.className = `status-badge status-${statusClass(run ? run.status : 'pending')}`;
        $('pipeline-run-error').textContent = run && run.error ? run.error : '';
    }

    function selectRun(runId) {
        selected = runId;
        refresh();
    }

    function renderHistory() {
        const body = $('pipeline-runs').querySelector('tbody');
        body.replaceChildren();
        runs.forEach(run => {
            const stepName = (run.steps[run.current_step] || {}).name || '';
            let progress = '';
            if (run.status === 'running' && stepName) progress = `Running: ${stepName}`;
            if (['failed', 'cancelled'].includes(run.status) && stepName) progress = `Stopped at ${stepName}`;

            const row = document.createElement('tr');
            row.dataset.run = run.run_id;
            const runCell = document.createElement('td');
            const runButton = textElement('button', 'run-history-link', `#${run.run_id}`);
            runButton.type = 'button';
            runButton.setAttribute('aria-label', `Show pipeline run #${run.run_id}`);
            runCell.appendChild(runButton);
            const statusCell = document.createElement('td');
            statusCell.appendChild(textElement(
                'span', `status-badge status-${statusClass(run.status)}`, run.status
            ));
            [
                runCell,
                statusCell,
                textElement('td', '', new Date(run.started_at).toLocaleString()),
                textElement('td', '', duration(run.started_at, run.ended_at)),
                textElement('td', '', progress),
            ].forEach(cell => row.appendChild(cell));
            row.addEventListener('click', () => selectRun(Number(row.dataset.run)));
            body.appendChild(row);
        });

        const select = $('pipeline-run-select');
        select.replaceChildren();
        runs.forEach(run => {
            const option = document.createElement('option');
            option.value = run.run_id;
            option.textContent = `#${run.run_id} — ${run.status}`;
            select.appendChild(option);
        });
        select.disabled = runs.length === 0;
        if (selected) select.value = String(selected);
    }

    async function refreshOnce() {
        const [def, list] = await Promise.all([api(API), api(`${API}/runs?limit=50`)]);
        if (!def.success) throw new Error(errorMessage(def, 'Could not load pipeline definition'));
        if (!list.success) throw new Error(errorMessage(list, 'Could not load pipeline runs'));
        definition = def.data.pipeline;
        $('pipeline-description').textContent = definition.description || '';
        if (!yamlLoaded) {
            $('pipeline-yaml').value = def.data.yaml;
            yamlLoaded = true;
        }
        runs = list.data.runs;
        if (selected && !runs.some(run => run.run_id === selected)) selected = null;
        const run = selected ? runs.find(item => item.run_id === selected) : runs[0];
        renderChain(run, definition);
        renderControls(run);
        renderHistory();
        clearPageError('refresh');
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

    function showErrors(errors) {
        const list = $('yaml-errors');
        list.replaceChildren();
        errors.forEach(error => {
            const item = document.createElement('li');
            item.appendChild(textElement('code', '', error.path || '(root)'));
            item.append(` ${error.message}`);
            list.appendChild(item);
        });
    }

    $('btn-yaml-validate').addEventListener('click', async event => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
            const data = await api('/api/v1/pipelines/validate', {
                method: 'POST', headers: { 'Content-Type': 'application/yaml' }, body: $('pipeline-yaml').value,
            });
            if (!data.success) throw new Error(errorMessage(data, 'Validation failed'));
            showErrors(data.data.errors);
            $('yaml-status').textContent = data.data.valid ? 'Valid' : '';
            showPageError();
        } catch (error) {
            showPageError(`Could not validate pipeline: ${error.message}`);
        } finally {
            button.disabled = false;
        }
    });

    $('btn-yaml-save').addEventListener('click', async event => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
            const data = await api(API, {
                method: 'PUT', headers: { 'Content-Type': 'application/yaml' }, body: $('pipeline-yaml').value,
            });
            if (!data.success) {
                showErrors(data.error.details?.errors || [{ path: '', message: errorMessage(data, 'Save failed') }]);
                $('yaml-status').textContent = '';
                showPageError();
                return;
            }
            showErrors([]);
            $('yaml-status').textContent = 'Saved';
            showPageError();
            await refresh();
        } catch (error) {
            showPageError(`Could not save pipeline: ${error.message}`);
        } finally {
            button.disabled = false;
        }
    });

    async function action(url, label, button) {
        button.disabled = true;
        try {
            const data = await api(url, { method: 'POST' });
            if (!data.success) throw new Error(errorMessage(data, `Could not ${label}`));
            selected = null;
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
        const run = selected ? runs.find(item => item.run_id === selected) : runs[0];
        if (run && confirm(`Cancel ${run.run_ref}?`)) {
            action(`${API}/runs/${run.run_id}/cancel`, 'cancel pipeline', event.currentTarget);
        }
    });
    $('btn-pipeline-resume').addEventListener('click', event => {
        const run = runs[0];
        if (run) action(`${API}/runs/${run.run_id}/resume`, 'resume pipeline', event.currentTarget);
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
    $('pipeline-run-select').addEventListener('change', event => {
        selectRun(Number(event.target.value));
    });

    function clearPoll() {
        if (pollTimer !== null) window.clearTimeout(pollTimer);
        pollTimer = null;
    }

    function schedulePoll() {
        clearPoll();
        if (!polling || document.hidden) return;
        pollTimer = window.setTimeout(async () => {
            await refresh();
            schedulePoll();
        }, 5000);
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
