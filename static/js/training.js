// Beekeeper — Parallel run list for the project page

const runListEl = document.getElementById('run-list');
const PROJECT_NAME = runListEl ? runListEl.dataset.name : (window.TRAINING_CONFIG && window.TRAINING_CONFIG.name);
const PARALLEL_ENABLED = runListEl ? runListEl.dataset.parallel === 'true' : false;
const DEFAULT_BRANCH = runListEl ? (runListEl.dataset.defaultBranch || 'main') : 'main';

// Map of run_id -> { branch, startedAt, sseSource }
const activeRuns = new Map();

// --- API helper ---
function apiFetch(url, opts = {}) {
    return fetch(url, {
        headers: { 'Content-Type': 'application/json', ...opts.headers },
        ...opts,
    });
}

// ============================================================
// Branch picker (for starting new runs)
// ============================================================
const startBtn = document.getElementById('btn-start-run');
const branchPicker = document.getElementById('branch-picker');
const runBranchSelect = document.getElementById('run-branch-select');
const confirmStartBtn = document.getElementById('btn-confirm-start');
const cancelStartBtn = document.getElementById('btn-cancel-start');

if (startBtn) {
    startBtn.addEventListener('click', async () => {
        branchPicker.classList.add('open');
        startBtn.disabled = true;
        await loadRunBranches();
    });
}
if (cancelStartBtn) {
    cancelStartBtn.addEventListener('click', () => {
        branchPicker.classList.remove('open');
        if (startBtn) startBtn.disabled = false;
    });
}
if (confirmStartBtn) {
    confirmStartBtn.addEventListener('click', () => {
        if (runBranchSelect) startRun(runBranchSelect.value);
    });
}

async function loadRunBranches() {
    if (!runBranchSelect || !PROJECT_NAME) return;
    try {
        const r = await apiFetch(`/api/v1/projects/${PROJECT_NAME}/branches`);
        const data = await r.json();
        const branches = data?.data?.branches || [];
        runBranchSelect.innerHTML = '';
        branches.forEach(b => {
            const opt = document.createElement('option');
            opt.value = b;
            opt.textContent = b === DEFAULT_BRANCH ? `${b} (default)` : b;
            if (b === DEFAULT_BRANCH) opt.selected = true;
            runBranchSelect.appendChild(opt);
        });
        if (!branches.includes(DEFAULT_BRANCH)) {
            const opt = document.createElement('option');
            opt.value = DEFAULT_BRANCH;
            opt.textContent = `${DEFAULT_BRANCH} (default)`;
            opt.selected = true;
            runBranchSelect.insertBefore(opt, runBranchSelect.firstChild);
        }
    } catch (e) {
        console.warn('Could not load branches:', e);
    }
}

async function startRun(branch) {
    if (branchPicker) branchPicker.classList.remove('open');
    if (startBtn) startBtn.disabled = false;
    try {
        const r = await apiFetch(`/api/v1/projects/${PROJECT_NAME}/training/start`, {
            method: 'POST',
            body: JSON.stringify({ branch }),
        });
        const data = await r.json();
        if (!data.success) {
            alert(`Could not start run: ${data.error?.message || 'unknown error'}`);
            return;
        }
        setTimeout(refreshRuns, 2000);
    } catch (e) {
        alert(`Could not start run: ${e}`);
    }
}

// ============================================================
// Stop a run
// ============================================================
async function stopRun(runId) {
    try {
        const r = await apiFetch(`/api/v1/projects/${PROJECT_NAME}/training/stop`, {
            method: 'POST',
            body: JSON.stringify({ run_id: runId }),
        });
        const data = await r.json();
        if (!data.success) {
            alert(`Could not stop run: ${data.error?.message || 'unknown error'}`);
        }
        setTimeout(refreshRuns, 1500);
    } catch (e) {
        alert(`Could not stop run: ${e}`);
    }
}

// ============================================================
// Run row rendering
// ============================================================
function formatElapsed(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function renderRunRow(run) {
    const { run_id, branch, status, pid, elapsed, tb_port } = run;
    const div = document.createElement('div');
    div.className = `run-row ${status === 'running' || status === 'starting' ? 'run-active' : ''}`;
    div.id = `run-row-${run_id}`;
    const host = (window.TRAINING_CONFIG && window.TRAINING_CONFIG.host) || location.hostname;
    div.innerHTML = `
        <div class="run-row-header">
            <span class="status-badge status-${status === 'starting' ? 'running' : status}">${status}</span>
            <span class="run-branch">${branch}</span>
            <span class="run-id">#${run_id}</span>
            ${pid ? `<span class="muted" style="font-size:0.75rem;">PID ${pid}</span>` : ''}
            <span class="run-elapsed" id="elapsed-${run_id}">${elapsed ? formatElapsed(elapsed) : ''}</span>
            ${tb_port ? `<a href="http://${host}:${tb_port}" target="_blank" class="btn btn-secondary btn-sm">Tensorboard</a>` : ''}
            <button class="btn btn-secondary btn-sm" id="btn-logs-${run_id}">▶ Logs</button>
            ${status !== 'starting' ? `<button class="btn btn-danger btn-sm" id="btn-stop-${run_id}">■ Stop</button>` : ''}
        </div>
        <div class="run-log-panel" id="log-panel-${run_id}" style="display:none">
            <div class="run-log-controls">
                <button class="btn btn-secondary btn-sm" id="btn-clear-log-${run_id}">Clear</button>
                <a href="/projects/${PROJECT_NAME}/logs/download?run_id=${run_id}" class="btn btn-secondary btn-sm">Download</a>
            </div>
            <pre class="run-log-terminal" id="log-terminal-${run_id}"></pre>
        </div>
    `;

    const logsBtn = div.querySelector(`#btn-logs-${run_id}`);
    const logPanel = div.querySelector(`#log-panel-${run_id}`);
    const logTerminal = div.querySelector(`#log-terminal-${run_id}`);
    const stopBtn = div.querySelector(`#btn-stop-${run_id}`);
    const clearBtn = div.querySelector(`#btn-clear-log-${run_id}`);

    logsBtn.addEventListener('click', () => toggleLogs(run_id, logPanel, logTerminal, logsBtn, status));
    if (stopBtn) stopBtn.addEventListener('click', () => stopRun(run_id));
    if (clearBtn) clearBtn.addEventListener('click', () => { logTerminal.textContent = ''; });

    if (status === 'running') {
        logPanel.style.display = '';
        logsBtn.textContent = '▼ Logs';
        startLogStream(run_id, logTerminal);
    }

    return div;
}

function toggleLogs(runId, logPanel, logTerminal, logsBtn, status) {
    const isOpen = logPanel.style.display !== 'none';
    if (isOpen) {
        logPanel.style.display = 'none';
        logsBtn.textContent = '▶ Logs';
        stopLogStream(runId);
    } else {
        logPanel.style.display = '';
        logsBtn.textContent = '▼ Logs';
        if (status === 'running' || status === 'starting') {
            startLogStream(runId, logTerminal);
        } else {
            loadHistoricalLog(runId, logTerminal);
        }
    }
}

// ============================================================
// SSE log streaming
// ============================================================
function startLogStream(runId, terminalEl) {
    const state = activeRuns.get(runId);
    if (state && state.sseSource) return;
    const src = new EventSource(`/projects/${PROJECT_NAME}/logs/stream?run_id=${runId}&tail=500`);
    src.onmessage = (e) => {
        if (e.data) {
            terminalEl.textContent += e.data + '\n';
            terminalEl.scrollTop = terminalEl.scrollHeight;
        }
    };
    src.addEventListener('done', () => {
        src.close();
        const s = activeRuns.get(runId);
        if (s) s.sseSource = null;
        setTimeout(refreshRuns, 1000);
    });
    src.onerror = () => src.close();
    if (activeRuns.has(runId)) activeRuns.get(runId).sseSource = src;
}

function stopLogStream(runId) {
    const state = activeRuns.get(runId);
    if (state && state.sseSource) {
        state.sseSource.close();
        state.sseSource = null;
    }
}

async function loadHistoricalLog(runId, terminalEl) {
    try {
        const r = await apiFetch(`/api/v1/projects/${PROJECT_NAME}/logs?run_id=${runId}&tail=500`);
        const data = await r.json();
        terminalEl.textContent = data?.data?.content || '(no log)';
        terminalEl.scrollTop = terminalEl.scrollHeight;
    } catch (e) {
        terminalEl.textContent = `(error loading log: ${e})`;
    }
}

// ============================================================
// Refresh runs from API
// ============================================================
async function refreshRuns() {
    if (!runListEl) return;
    try {
        const r = await apiFetch(`/api/v1/projects/${PROJECT_NAME}/training/status`);
        const data = await r.json();
        const runs = data?.data?.runs || [];
        reconcileRunList(runs);
        updateStartButton(runs);
    } catch (e) {
        console.warn('Could not refresh runs:', e);
    }
}

function reconcileRunList(runs) {
    if (!runListEl) return;
    const currentIds = new Set(runs.map(r => r.run_id));

    for (const [runId] of activeRuns) {
        if (!currentIds.has(runId)) {
            stopLogStream(runId);
            activeRuns.delete(runId);
            const row = document.getElementById(`run-row-${runId}`);
            if (row) row.remove();
        }
    }

    for (const run of runs) {
        if (!activeRuns.has(run.run_id)) {
            activeRuns.set(run.run_id, {
                branch: run.branch,
                startedAt: Date.now() - (run.elapsed || 0) * 1000,
                sseSource: null,
            });
            const row = renderRunRow(run);
            runListEl.appendChild(row);
        } else {
            const elapsedEl = document.getElementById(`elapsed-${run.run_id}`);
            if (elapsedEl && run.elapsed) elapsedEl.textContent = formatElapsed(run.elapsed);
        }
    }

    const idleMsg = document.getElementById('run-idle-msg');
    if (runs.length === 0 && activeRuns.size === 0) {
        if (!idleMsg) {
            const p = document.createElement('p');
            p.className = 'muted';
            p.id = 'run-idle-msg';
            p.textContent = 'No active runs.';
            runListEl.appendChild(p);
        }
    } else if (idleMsg && runs.length > 0) {
        idleMsg.remove();
    }
}

function updateStartButton(runs) {
    if (!startBtn) return;
    if (!PARALLEL_ENABLED && runs.length > 0) {
        startBtn.disabled = true;
        startBtn.title = 'Enable parallel runs in project settings to run multiple branches';
    } else {
        startBtn.disabled = false;
        startBtn.title = '';
    }
}

// Elapsed ticker
setInterval(() => {
    for (const [runId, state] of activeRuns) {
        const elapsedEl = document.getElementById(`elapsed-${runId}`);
        if (elapsedEl && state.startedAt) {
            elapsedEl.textContent = formatElapsed((Date.now() - state.startedAt) / 1000);
        }
    }
}, 5000);

// Clean up SSE streams on page unload
window.addEventListener('beforeunload', () => {
    for (const [runId] of activeRuns) stopLogStream(runId);
});

// ============================================================
// Project-info branch switcher (the dropdown in Project Info section)
// This populates and manages the #branch-select dropdown.
// ============================================================
const projectBranchSelect = document.getElementById('branch-select');

async function loadProjectBranches() {
    if (!projectBranchSelect || !PROJECT_NAME) return;
    try {
        const resp = await fetch(`/api/v1/projects/${PROJECT_NAME}/branches`);
        if (!resp.ok) return;
        const result = await resp.json();
        if (!result.success) return;
        const { branches, current } = result.data;
        projectBranchSelect.innerHTML = '';
        for (const branch of branches) {
            const option = document.createElement('option');
            option.value = branch;
            option.textContent = branch;
            if (branch === current) option.selected = true;
            projectBranchSelect.appendChild(option);
        }
        if (activeRuns.size === 0) projectBranchSelect.disabled = false;
    } catch (e) {
        console.error('Error loading project branches:', e);
    }
}

async function switchProjectBranch(newBranch) {
    if (!projectBranchSelect) return;
    const oldBranch = projectBranchSelect.dataset.originalValue;
    projectBranchSelect.disabled = true;
    try {
        const resp = await fetch(`/api/v1/projects/${PROJECT_NAME}/branch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ branch: newBranch }),
        });
        const result = await resp.json();
        if (!result.success) {
            alert(result.error?.message || 'Failed to switch branch');
            projectBranchSelect.value = oldBranch;
            projectBranchSelect.disabled = false;
            return;
        }
        if (result.data.status === 'switched') {
            location.reload();
        } else {
            projectBranchSelect.disabled = false;
        }
    } catch (e) {
        alert('Network error switching branch');
        projectBranchSelect.value = oldBranch;
        projectBranchSelect.disabled = false;
    }
}

if (projectBranchSelect) {
    projectBranchSelect.dataset.originalValue = projectBranchSelect.value;
    projectBranchSelect.addEventListener('change', (e) => {
        if (e.target.value !== projectBranchSelect.dataset.originalValue) {
            switchProjectBranch(e.target.value);
        }
    });
    loadProjectBranches();
}

// ============================================================
// Collapsible section actions (toggle handled by app.js)
// ============================================================
document.querySelectorAll('.collapsible-header').forEach(header => {
    header.addEventListener('click', () => {
        const targetId = header.dataset.target;
        const body = document.getElementById(targetId);
        if (!body) return;
        const wasHidden = body.style.display === 'none';
        setTimeout(() => {
            if (wasHidden) {
                if (targetId === 'tb-body') activateTb();
                if (targetId === 'files-body' && window.loadFiles) window.loadFiles();
            }
        }, 0);
    });
});

// ============================================================
// Tensorboard management
// ============================================================
const tbConfig = window.TRAINING_CONFIG || {};

function renderTensorboard(port) {
    const container = document.getElementById('tb-dynamic');
    if (!container) return;
    const tbUrl = `http://${tbConfig.host || location.hostname}:${port}`;
    container.innerHTML =
        '<div class="tb-toolbar">' +
            `<a href="${tbUrl}" target="_blank" class="btn btn-secondary btn-sm">Open in New Tab</a>` +
            '<button class="btn btn-secondary btn-sm" id="btn-tb-expand">Expand</button>' +
            '<button class="btn btn-danger btn-sm" id="btn-tb-stop">Stop Tensorboard</button>' +
        '</div>' +
        '<div class="tensorboard-container" id="tb-container">' +
            `<iframe src="${tbUrl}" id="tensorboard-frame"></iframe>` +
        '</div>';
    bindTbExpand();
    bindTbStop();
}

function renderTbLauncher() {
    const container = document.getElementById('tb-dynamic');
    if (!container) return;
    container.innerHTML =
        '<button class="btn btn-success btn-sm" id="btn-tb-launch">Launch Tensorboard</button>' +
        '<p class="tb-launching muted" id="tb-launching" style="display:none">Launching Tensorboard...</p>';
    bindTbLaunch();
}

async function launchTb() {
    const launchBtn = document.getElementById('btn-tb-launch');
    const launchMsg = document.getElementById('tb-launching');
    if (launchBtn) launchBtn.style.display = 'none';
    if (launchMsg) launchMsg.style.display = 'block';
    try {
        const resp = await fetch(`/projects/${PROJECT_NAME}/tensorboard/start`, { method: 'POST' });
        const data = await resp.json();
        if (resp.ok && data.tb_port) {
            tbConfig.tbPort = data.tb_port;
            renderTensorboard(data.tb_port);
        } else {
            alert(data.error || 'Failed to launch Tensorboard');
            if (launchBtn) launchBtn.style.display = '';
            if (launchMsg) launchMsg.style.display = 'none';
        }
    } catch (e) {
        alert('Network error');
        if (launchBtn) launchBtn.style.display = '';
        if (launchMsg) launchMsg.style.display = 'none';
    }
}

async function stopTb() {
    try {
        await fetch(`/projects/${PROJECT_NAME}/tensorboard/stop`, { method: 'POST' });
    } catch (e) { /* ignore */ }
    tbConfig.tbPort = null;
    renderTbLauncher();
}

function bindTbExpand() {
    const btn = document.getElementById('btn-tb-expand');
    const container = document.getElementById('tb-container');
    if (btn && container) {
        let expanded = false;
        btn.addEventListener('click', () => {
            expanded = !expanded;
            container.classList.toggle('tb-expanded', expanded);
            btn.textContent = expanded ? 'Collapse' : 'Expand';
        });
    }
}

function bindTbStop() {
    const btn = document.getElementById('btn-tb-stop');
    if (btn) btn.addEventListener('click', stopTb);
}

function bindTbLaunch() {
    const btn = document.getElementById('btn-tb-launch');
    if (btn) btn.addEventListener('click', launchTb);
}

function activateTb() {
    const iframe = document.getElementById('tensorboard-frame');
    if (iframe && iframe.dataset.src && !iframe.src) {
        iframe.src = iframe.dataset.src;
    }
    if (!tbConfig.tbPort) {
        const launchBtn = document.getElementById('btn-tb-launch');
        if (launchBtn) launchTb();
    }
}

// Bind TB controls present in initial server render
bindTbExpand();
bindTbStop();
bindTbLaunch();

// ============================================================
// Init
// ============================================================
if (runListEl) {
    refreshRuns();
    setInterval(refreshRuns, 10000);
}
