// static/js/pipeline.js — Pipeline page: step chain, run controls, YAML editor.
(function () {
    const header = document.getElementById('pipeline-header');
    if (!header) return;
    const NAME = header.dataset.name;
    const API = `/api/v1/pipelines/${encodeURIComponent(NAME)}`;
    const $ = id => document.getElementById(id);
    const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

    let runs = [];
    let selected = null;   // run_id shown in the chain; null = latest
    let expandedLog = null;

    async function api(url, opts = {}) {
        const r = await fetch(url, opts);
        return r.json();
    }

    function duration(start, end) {
        if (!start) return '';
        const secs = ((end ? new Date(end) : new Date()) - new Date(start)) / 1000;
        const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = Math.floor(secs % 60);
        return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
    }

    function renderChain(run, definition) {
        const chain = $('step-chain');
        const pre = $('step-log');
        if (expandedLog && pre.style.display !== 'none') {
            expandedLog.scrollTop = pre.scrollTop;
            expandedLog.content = pre.textContent;
        }
        chain.innerHTML = '';
        const steps = run ? run.steps : definition.steps.map((s, i) => ({ ...s, index: i, status: 'pending' }));
        steps.forEach(s => {
            const li = document.createElement('li');
            li.className = `step-node step-${s.status}`;
            let link = '';
            if (s.type === 'train' && s.run_id) {
                link = `<a href="/projects/${encodeURIComponent(s.project)}/history/${s.run_id}/log">${esc(s.run_ref)}</a>`;
            } else if (s.type === 'train') {
                link = `<span class="muted">${esc(s.project)}</span>`;
            } else if (s.has_log) {
                const isExpanded = expandedLog && run && expandedLog.runId === run.run_id && expandedLog.stepName === s.name;
                link = `<a href="#" data-step-log="${esc(s.name)}" aria-expanded="${isExpanded ? 'true' : 'false'}">log</a>`;
            }
            li.innerHTML = `
                <div class="step-name">${esc(s.name)} <span class="muted">${esc(s.type)}</span></div>
                <div><span class="status-badge status-${esc(s.status)}">${esc(s.status)}</span>
                     ${s.attempt > 1 ? `<span class="muted">attempt ${s.attempt}</span>` : ''}</div>
                <div class="muted">${duration(s.started_at, s.ended_at)}</div>
                <div>${link}</div>
                ${s.wait_reason ? `<div class="queue-wait">waiting: ${esc(s.wait_reason)}</div>` : ''}
                ${s.error ? `<div class="pipeline-error">${esc(s.error)}</div>` : ''}`;
            chain.appendChild(li);
        });
        const logStillSelected = expandedLog && run && expandedLog.runId === run.run_id &&
            steps.some(s => s.name === expandedLog.stepName && s.has_log);
        if (logStillSelected) {
            pre.style.display = '';
            pre.textContent = expandedLog.content;
            pre.scrollTop = expandedLog.scrollTop;
        } else {
            pre.style.display = 'none';
        }
        chain.querySelectorAll('[data-step-log]').forEach(a => a.addEventListener('click', async e => {
            e.preventDefault();
            const data = await api(`${API}/runs/${run.run_id}/steps/${a.dataset.stepLog}/log?tail=500`);
            expandedLog = {
                runId: run.run_id,
                stepName: a.dataset.stepLog,
                content: data.success ? data.data.log : data.error.message,
                scrollTop: 0,
            };
            pre.style.display = '';
            pre.textContent = expandedLog.content;
            chain.querySelectorAll('[data-step-log]').forEach(link => link.setAttribute('aria-expanded', 'false'));
            a.setAttribute('aria-expanded', 'true');
        }));
    }

    function renderControls(run) {
        const latest = runs[0];
        $('btn-pipeline-start').style.display = latest && latest.status === 'running' ? 'none' : '';
        $('btn-pipeline-cancel').style.display = run && run.status === 'running' ? '' : 'none';
        $('btn-pipeline-resume').style.display =
            run && latest && run.run_id === latest.run_id && ['failed', 'cancelled'].includes(run.status) ? '' : 'none';
        const badge = $('pipeline-run-status');
        badge.textContent = run ? run.status : 'never run';
        badge.className = `status-badge status-${run ? run.status : 'pending'}`;
        $('pipeline-run-error').textContent = run && run.error ? run.error : '';
    }

    function renderHistory() {
        const body = $('pipeline-runs').querySelector('tbody');
        body.innerHTML = runs.map(r => {
            const stopped = r.status === 'succeeded' ? '' : (r.steps[r.current_step] || {}).name || '';
            return `<tr data-run="${r.run_id}"><td>#${r.run_id}</td>
                <td><span class="status-badge status-${esc(r.status)}">${esc(r.status)}</span></td>
                <td>${esc(new Date(r.started_at).toLocaleString())}</td>
                <td>${duration(r.started_at, r.ended_at)}</td><td>${esc(stopped)}</td></tr>`;
        }).join('');
        body.querySelectorAll('tr').forEach(tr => tr.addEventListener('click', () => {
            selected = Number(tr.dataset.run);
            refresh();
        }));
        const sel = $('pipeline-run-select');
        sel.innerHTML = runs.map(r => `<option value="${r.run_id}">#${r.run_id} — ${esc(r.status)}</option>`).join('');
        if (selected) sel.value = String(selected);
    }

    let definition = { steps: [] };

    async function refresh() {
        const [def, list] = await Promise.all([api(API), api(`${API}/runs?limit=50`)]);
        if (def.success) {
            definition = def.data.pipeline;
            $('pipeline-description').textContent = definition.description || '';
        }
        runs = list.success ? list.data.runs : [];
        const run = selected ? runs.find(r => r.run_id === selected) : runs[0];
        renderChain(run, definition);
        renderControls(run);
        renderHistory();
    }

    async function loadYaml() {
        const def = await api(API);
        if (def.success) $('pipeline-yaml').value = def.data.yaml;
    }

    function showErrors(errors) {
        $('yaml-errors').innerHTML = errors.map(e => `<li><code>${esc(e.path || '(root)')}</code> ${esc(e.message)}</li>`).join('');
    }

    $('btn-yaml-validate').addEventListener('click', async () => {
        const data = await api('/api/v1/pipelines/validate', {
            method: 'POST', headers: { 'Content-Type': 'application/yaml' }, body: $('pipeline-yaml').value,
        });
        showErrors(data.data.errors);
        $('yaml-status').textContent = data.data.valid ? 'Valid' : '';
    });

    $('btn-yaml-save').addEventListener('click', async () => {
        const data = await api(API, {
            method: 'PUT', headers: { 'Content-Type': 'application/yaml' }, body: $('pipeline-yaml').value,
        });
        if (data.success) { showErrors([]); $('yaml-status').textContent = 'Saved'; refresh(); }
        else { showErrors(data.error.details?.errors || [{ path: '', message: data.error.message }]); $('yaml-status').textContent = ''; }
    });

    async function action(url, label) {
        const data = await api(url, { method: 'POST' });
        if (!data.success) alert(`Could not ${label}: ${data.error.message}`);
        selected = null;
        refresh();
    }
    $('btn-pipeline-start').addEventListener('click', () => action(`${API}/runs`, 'start pipeline'));
    $('btn-pipeline-cancel').addEventListener('click', () => {
        const run = selected ? runs.find(r => r.run_id === selected) : runs[0];
        if (run && confirm(`Cancel ${run.run_ref}?`)) action(`${API}/runs/${run.run_id}/cancel`, 'cancel');
    });
    $('btn-pipeline-resume').addEventListener('click', () => {
        const run = runs[0];
        if (run) action(`${API}/runs/${run.run_id}/resume`, 'resume');
    });
    $('btn-pipeline-delete').addEventListener('click', async () => {
        if (!confirm(`Delete pipeline ${NAME} and its run history? Training runs are kept.`)) return;
        const data = await api(API, { method: 'DELETE' });
        if (data.success) location.href = '/'; else alert(data.error.message);
    });
    $('pipeline-run-select').addEventListener('change', e => { selected = Number(e.target.value); refresh(); });

    loadYaml();
    refresh();
    setInterval(refresh, 5000);
})();
