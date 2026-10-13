// Beekeeper — Parallel run list for the project page

const runListEl = document.getElementById('run-list');
const PROJECT_NAME = runListEl?.dataset.name ?? window.TRAINING_CONFIG?.name;
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

function escHtml(s) {
    return String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
}

// ============================================================
// Branch picker (for starting new runs)
// ============================================================
const startBtn = document.getElementById('btn-start-run');
const runListActions = document.getElementById('run-list-actions');
const branchPicker = document.getElementById('branch-picker');
const runBranchSelect = document.getElementById('run-branch-select');
const confirmStartBtn = document.getElementById('btn-confirm-start');
const cancelStartBtn = document.getElementById('btn-cancel-start');
const confirmQueueBtn = document.getElementById('btn-confirm-queue');
const startReasonEl = document.getElementById('start-reason');
const TRANSIENT_REFUSALS = new Set(['ALREADY_RUNNING', 'AT_CAPACITY', 'INSUFFICIENT_VRAM']);
let canStartRefreshInFlight = null;
let canStartState = null;
let activeRunCount = 0;

// True when user clicked "Start Run..." while runs were active.
// Prevents refreshRuns from closing the picker mid-fill.
let pickerManuallyOpen = false;

if (startBtn) {
    startBtn.addEventListener('click', async () => {
        pickerManuallyOpen = true;
        if (runListActions) runListActions.style.display = 'none';
        if (branchPicker) branchPicker.classList.add('open');
        if (cancelStartBtn) cancelStartBtn.style.display = '';
        await Promise.all([loadRunBranches(), refreshCanStart()]);
    });
}
if (cancelStartBtn) {
    cancelStartBtn.addEventListener('click', () => {
        pickerManuallyOpen = false;
        if (branchPicker) branchPicker.classList.remove('open');
        cancelStartBtn.style.display = 'none';
        if (runListActions) runListActions.style.display = '';
    });
}
if (confirmStartBtn) {
    confirmStartBtn.addEventListener('click', () => {
        if (runBranchSelect) void startRun(runBranchSelect.value);
    });
}

const queueSection = document.getElementById('queue-section');
const queueListEl = document.getElementById('queue-list');
let queueRefreshInFlight = null;
let queueRefreshAgain = false;
let queuePollTimer = null;
let queuePolling = true;

if (confirmQueueBtn) {
    confirmQueueBtn.addEventListener('click', () => {
        if (runBranchSelect) void queueRun(runBranchSelect.value);
    });
}

async function queueRun(branch) {
    pickerManuallyOpen = false;
    if (branchPicker) branchPicker.classList.remove('open');
    if (cancelStartBtn) cancelStartBtn.style.display = 'none';
    try {
        const r = await apiFetch(`/api/v1/projects/${PROJECT_NAME}/training/start`, {
            method: 'POST',
            body: JSON.stringify({ branch, queue: true }),
        });
        const data = await r.json();
        if (!data.success) {
            alert(`Could not queue run: ${data.error?.message || 'unknown error'}`);
            return;
        }
        void refreshQueue();
    } catch (e) {
        alert(`Could not queue run: ${e}`);
    }
}

function formatAge(iso) {
    const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    return formatElapsed(secs) + ' ago';
}

async function refreshQueueOnce() {
    const r = await apiFetch(`/api/v1/projects/${PROJECT_NAME}/queue`);
    const data = await r.json();
    const entries = data?.data?.queue || [];
    queueSection.style.display = entries.length ? '' : 'none';
    queueListEl.innerHTML = '';
    entries.forEach(e => {
        const li = document.createElement('li');
        li.className = 'queue-row';
        li.innerHTML = `
            <span class="run-branch">${escHtml(e.branch || DEFAULT_BRANCH)}</span>
            <span class="muted queue-source">${escHtml(e.source)}</span>
            <span class="muted">${escHtml(formatAge(e.created_at))}</span>
            ${e.wait_reason ? `<span class="queue-wait">waiting: ${escHtml(e.wait_reason)}</span>` : ''}
            <button class="btn btn-secondary btn-sm" data-queue-id="${e.queue_id}">Cancel</button>`;
        li.querySelector('button').addEventListener('click', () => void cancelQueued(e.queue_id));
        queueListEl.appendChild(li);
    });
}

function refreshQueue() {
    if (!queueListEl || !PROJECT_NAME) return Promise.resolve();
    if (queueRefreshInFlight) {
        queueRefreshAgain = true;
        return queueRefreshInFlight;
    }
    queueRefreshInFlight = refreshQueueOnce()
        .catch(err => console.warn('Could not load queue:', err))
        .finally(() => {
            queueRefreshInFlight = null;
            if (queueRefreshAgain && queuePolling && !document.hidden) {
                queueRefreshAgain = false;
                void refreshQueue();
            }
        });
    return queueRefreshInFlight;
}

async function cancelQueued(queueId) {
    try {
        const r = await apiFetch(`/api/v1/projects/${PROJECT_NAME}/queue/${queueId}`, { method: 'DELETE' });
        const data = await r.json();
        if (!data.success) alert(`Could not cancel: ${data.error?.message || 'unknown error'}`);
        await refreshQueue();
    } catch (err) {
        alert(`Could not cancel queued run: Network request failed: ${err.message}`);
    }
}

function clearQueuePoll() {
    if (queuePollTimer !== null) window.clearTimeout(queuePollTimer);
    queuePollTimer = null;
}

function scheduleQueuePoll() {
    clearQueuePoll();
    if (!queuePolling || document.hidden || !queueListEl || !PROJECT_NAME) return;
    queuePollTimer = window.setTimeout(async () => {
        await refreshQueue();
        scheduleQueuePoll();
    }, 5000);
}

document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        clearQueuePoll();
    } else {
        void refreshQueue().finally(scheduleQueuePoll);
    }
});
window.addEventListener('pagehide', () => {
    queuePolling = false;
    clearQueuePoll();
});
window.addEventListener('pageshow', event => {
    if (event.persisted) {
        queuePolling = true;
        void refreshQueue().finally(scheduleQueuePoll);
    }
});

void refreshQueue().finally(scheduleQueuePoll); // NOSONAR - classic script, no top-level await

async function refreshCanStart() {
    if (!confirmStartBtn || !confirmQueueBtn || !PROJECT_NAME) return;
    if (canStartRefreshInFlight) return canStartRefreshInFlight;
    canStartRefreshInFlight = (async () => {
        try {
            const response = await apiFetch(`/api/v1/projects/${PROJECT_NAME}/training/can-start`);
            const result = await response.json();
            if (!response.ok || !result.success) return;
            canStartState = result.data;
            applyStartAvailability();
        } catch (error) {
            console.warn('Could not check whether training can start:', error);
        }
    })().finally(() => { canStartRefreshInFlight = null; });
    return canStartRefreshInFlight;
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
    pickerManuallyOpen = false;
    if (branchPicker) branchPicker.classList.remove('open');
    if (cancelStartBtn) cancelStartBtn.style.display = 'none';
    try {
        const r = await apiFetch(`/api/v1/projects/${PROJECT_NAME}/training/start`, {
            method: 'POST',
            body: JSON.stringify({ branch }),
        });
        const data = await r.json();
        if (!data.success) {
            const code = data.error?.details?.code;
            if (TRANSIENT_REFUSALS.has(code) &&
                confirm(`${data.error.message}\n\nQueue this run to start when a slot frees up?`)) {
                await queueRun(branch);
            } else {
                alert(`Could not start run: ${data.error?.message || 'unknown error'}`);
            }
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
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

function runRowMarkup(run) {
    const { run_id, branch, status, pid, elapsed, tb_port } = run;
    const statusStyle = status === 'starting' ? 'running' : escHtml(status);
    const pidHtml = pid ? `<span class="muted" style="font-size:0.75rem;">PID ${pid}</span>` : '';
    const elapsedText = elapsed ? formatElapsed(elapsed) : '';
    const host = window.TRAINING_CONFIG?.host || location.hostname;
    const tensorboardHtml = tb_port
        ? `<a href="http://${escHtml(host)}:${tb_port}" target="_blank" class="btn btn-secondary btn-sm">Tensorboard</a>`
        : '';
    const stopHtml = status === 'starting'
        ? ''
        : `<button class="btn btn-danger btn-sm" id="btn-stop-${run_id}">■ Stop</button>`;
    return `
        <div class="run-row-header">
            <span class="status-badge status-${statusStyle}">${escHtml(status)}</span>
            <span class="run-branch">${escHtml(branch)}</span>
            <span class="run-id">#${run_id}</span>
            ${pidHtml}
            <span class="run-elapsed" id="elapsed-${run_id}">${elapsedText}</span>
            ${tensorboardHtml}
            <button class="btn btn-secondary btn-sm" id="btn-logs-${run_id}">▶ Logs</button>
            ${stopHtml}
        </div>
        <div class="run-log-panel" id="log-panel-${run_id}" style="display:none">
            <div class="run-log-controls">
                <button class="btn btn-secondary btn-sm" id="btn-clear-log-${run_id}">Clear</button>
                <a href="/projects/${PROJECT_NAME}/logs/download?run_id=${run_id}" class="btn btn-secondary btn-sm">Download</a>
            </div>
            <pre class="run-log-terminal" id="log-terminal-${run_id}"></pre>
        </div>`;
}

function bindRunRow(div, run) {
    const { run_id, status } = run;
    const logsBtn = div.querySelector(`#btn-logs-${run_id}`);
    const logPanel = div.querySelector(`#log-panel-${run_id}`);
    const logTerminal = div.querySelector(`#log-terminal-${run_id}`);
    const stopBtn = div.querySelector(`#btn-stop-${run_id}`);
    const clearBtn = div.querySelector(`#btn-clear-log-${run_id}`);
    logsBtn.addEventListener('click', () => toggleLogs(run_id, logPanel, logTerminal, logsBtn, status));
    if (stopBtn) stopBtn.addEventListener('click', () => void stopRun(run_id));
    if (clearBtn) clearBtn.addEventListener('click', () => { logTerminal.textContent = ''; });
    if (status === 'running') {
        logPanel.style.display = '';
        logsBtn.textContent = '▼ Logs';
        startLogStream(run_id, logTerminal);
    }
}

function renderRunRow(run) {
    const { status, run_id } = run;
    const div = document.createElement('div');
    div.className = `run-row ${status === 'running' || status === 'starting' ? 'run-active' : ''}`;
    div.id = `run-row-${run_id}`;
    div.innerHTML = runRowMarkup(run);
    bindRunRow(div, run);
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
            void loadHistoricalLog(runId, logTerminal);
        }
    }
}

// ============================================================
// SSE log streaming
// ============================================================
function startLogStream(runId, terminalEl) {
    const state = activeRuns.get(runId);
    if (state?.sseSource) return;
    const src = new EventSource(`/projects/${PROJECT_NAME}/logs/stream?run_id=${runId}&tail=500`);
    src.onmessage = (e) => {
        if (e.data) {
            terminalEl.textContent += e.data + '\n';
            // Cap at 2000 lines to prevent memory bloat on long runs
            const lines = terminalEl.textContent.split('\n');
            if (lines.length > 2100) {
                terminalEl.textContent = lines.slice(lines.length - 2000).join('\n');
            }
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
    if (state?.sseSource) {
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
function syncTbPort(responseData) {
    const newPort = responseData?.tb_port ?? null;
    if (newPort && newPort !== tbConfig.tbPort) {
        tbConfig.tbPort = newPort;
        renderTensorboard(newPort);
    } else if (!newPort && tbConfig.tbPort) {
        tbConfig.tbPort = null;
        renderTbLauncher();
    }
}

async function refreshRuns() {
    if (!runListEl) return;
    try {
        const r = await apiFetch(`/api/v1/projects/${PROJECT_NAME}/training/status`);
        const data = await r.json();
        const runs = data?.data?.runs || [];
        reconcileRunList(runs);
        updateStartButton(runs);
        void refreshCanStart();
        syncTbPort(data?.data);
    } catch (e) {
        console.warn('Could not refresh runs:', e);
    }
}

function removeInactiveRunRows(currentIds) {
    for (const [runId] of activeRuns) {
        if (currentIds.has(runId)) continue;
        stopLogStream(runId);
        activeRuns.delete(runId);
        document.getElementById(`run-row-${runId}`)?.remove();
    }
}

function addRunRow(run) {
    activeRuns.set(run.run_id, {
        branch: run.branch,
        startedAt: Date.now() - (run.elapsed || 0) * 1000,
        sseSource: null,
        status: run.status,
    });
    runListEl.appendChild(renderRunRow(run));
}

function updateRunRow(run, state) {
    if (state.status !== run.status) {
        stopLogStream(run.run_id);
        state.status = run.status;
        if (run.elapsed) state.startedAt = Date.now() - run.elapsed * 1000;
        const oldRow = document.getElementById(`run-row-${run.run_id}`);
        const newRow = renderRunRow(run);
        if (oldRow) oldRow.replaceWith(newRow);
        return;
    }
    const elapsedEl = document.getElementById(`elapsed-${run.run_id}`);
    if (elapsedEl && run.elapsed) elapsedEl.textContent = formatElapsed(run.elapsed);
}

function reconcileRunList(runs) {
    if (!runListEl) return;
    const currentIds = new Set(runs.map(r => r.run_id));
    removeInactiveRunRows(currentIds);
    for (const run of runs) {
        const state = activeRuns.get(run.run_id);
        if (state) updateRunRow(run, state);
        else addRunRow(run);
    }

    const idleMsg = document.getElementById('run-idle-msg');
    if (idleMsg) idleMsg.remove();
}

function updateStartButton(runs) {
    activeRunCount = runs.length;
    applyStartAvailability();
}

function applyStartAvailability() {
    if (!canStartState) return;
    const queueInstead = !canStartState.can_start && TRANSIENT_REFUSALS.has(canStartState.code);
    confirmStartBtn.hidden = queueInstead;
    confirmStartBtn.disabled = !canStartState.can_start && !queueInstead;
    confirmQueueBtn.hidden = !queueInstead;
    if (startReasonEl) {
        startReasonEl.textContent = canStartState.can_start ? '' : canStartState.message;
    }

    if (activeRunCount === 0 || queueInstead || !canStartState.can_start) {
        if (runListActions) runListActions.style.display = 'none';
        if (cancelStartBtn) cancelStartBtn.style.display = pickerManuallyOpen ? '' : 'none';
        if (branchPicker && !branchPicker.classList.contains('open')) {
            branchPicker.classList.add('open');
        }
    } else if (PARALLEL_ENABLED) {
        if (!pickerManuallyOpen) {
            if (runListActions) runListActions.style.display = '';
            if (branchPicker) branchPicker.classList.remove('open');
            if (cancelStartBtn) cancelStartBtn.style.display = 'none';
        }
    } else {
        if (branchPicker) branchPicker.classList.remove('open');
        if (runListActions) runListActions.style.display = 'none';
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
        console.warn('Error switching project branch:', e);
        alert('Network error switching branch');
        projectBranchSelect.value = oldBranch;
        projectBranchSelect.disabled = false;
    }
}

if (projectBranchSelect) {
    projectBranchSelect.dataset.originalValue = projectBranchSelect.value;
    projectBranchSelect.addEventListener('change', (e) => {
        if (e.target.value !== projectBranchSelect.dataset.originalValue) {
            void switchProjectBranch(e.target.value);
        }
    });
    void loadProjectBranches(); // NOSONAR - classic script, no top-level await
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
        if (targetId === 'files-body' && window.loadFiles) void window.loadFiles();
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
    } catch (e) {
        // The launcher is reset locally even if the already-stopped server is unreachable.
        console.warn('Could not stop Tensorboard:', e);
    }
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
    if (btn) btn.addEventListener('click', () => void stopTb());
}

function bindTbLaunch() {
    const btn = document.getElementById('btn-tb-launch');
    if (btn) btn.addEventListener('click', () => void launchTb());
}

function activateTb() {
    const iframe = document.getElementById('tensorboard-frame');
    if (iframe?.dataset.src && !iframe.src) {
        iframe.src = iframe.dataset.src;
    }
    if (!tbConfig.tbPort) {
        const launchBtn = document.getElementById('btn-tb-launch');
        if (launchBtn) void launchTb();
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
    void loadRunBranches(); // NOSONAR - classic script, no top-level await
    void refreshCanStart(); // NOSONAR - classic script, no top-level await
    void refreshRuns(); // NOSONAR - classic script, no top-level await
    setInterval(refreshRuns, 10000);
}
