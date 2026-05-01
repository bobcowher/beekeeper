// Run history display, annotations, and comparison
(function() {
    const projectName = window.TRAINING_CONFIG?.name;
    if (!projectName) return;

    const listEl = document.getElementById('run-history-list');
    const clearBtn = document.getElementById('clear-history-btn');
    if (!listEl) return;

    let allRuns = [];
    let filterNotable = false;
    let filterTag = '';
    const checkedIds = new Set();
    const expandedNotes = new Set();

    // ---------- API ----------

    async function patchRun(runId, data) {
        await fetch(`/projects/${projectName}/history/${runId}`, {
            method: 'PATCH',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
    }

    // ---------- Load ----------

    async function loadHistory() {
        try {
            const resp = await fetch(`/projects/${projectName}/history`);
            if (!resp.ok) {
                listEl.innerHTML = '<p class="muted">Failed to load run history.</p>';
                return;
            }
            const data = await resp.json();
            allRuns = data.runs || [];
            // Keep checked state only for runs that still exist
            const existingIds = new Set(allRuns.map(r => r.id));
            for (const id of [...checkedIds]) {
                if (!existingIds.has(id)) checkedIds.delete(id);
            }
            renderAll();
        } catch (e) {
            listEl.innerHTML = '<p class="muted">Error loading run history.</p>';
        }
    }

    // ---------- Render ----------

    function getFiltered() {
        return allRuns.filter(run => {
            if (filterNotable && !run.notable) return false;
            if (filterTag) {
                const tags = (run.tags || '').split(',').map(t => t.trim()).filter(Boolean);
                if (!tags.some(t => t.toLowerCase().includes(filterTag.toLowerCase()))) return false;
            }
            return true;
        });
    }

    function renderAll() {
        if (clearBtn) clearBtn.disabled = allRuns.length === 0;

        const filtered = getFiltered();

        let html = '<div class="history-filter-bar">';
        html += `<button class="filter-notable-btn${filterNotable ? ' active' : ''}" id="filter-notable-btn">&#9733; Starred</button>`;
        html += `<input type="text" class="filter-tag-input" id="filter-tag-input" placeholder="Filter by tag…" value="${escHtml(filterTag)}">`;
        if (checkedIds.size === 2) {
            html += `<button class="btn btn-secondary btn-sm" id="compare-btn">Compare selected</button>`;
        }
        html += '</div>';

        if (allRuns.length === 0) {
            html += '<p class="muted">No training runs yet.</p>';
            listEl.innerHTML = html;
            attachFilterEvents();
            return;
        }

        if (filtered.length === 0) {
            html += '<p class="muted">No runs match the current filter.</p>';
            listEl.innerHTML = html;
            attachFilterEvents();
            return;
        }

        html += '<table class="run-history-table"><thead><tr>';
        html += '<th style="width:32px;text-align:center">&#9733;</th>';
        html += '<th style="width:20px"></th>';
        html += '<th style="width:40px">#</th>';
        html += '<th>Started</th>';
        html += '<th>Duration</th>';
        html += '<th>Status</th>';
        html += '<th>Branch / Commit</th>';
        html += '<th>Tags</th>';
        html += '<th>Actions</th>';
        html += '</tr></thead><tbody>';

        filtered.forEach(run => {
            html += buildRunRow(run);
        });

        html += '</tbody></table>';
        listEl.innerHTML = html;
        attachFilterEvents();
        attachTableEvents();
    }

    function buildRunRow(run) {
        const started = new Date(run.started_at).toLocaleString();
        const duration = formatDuration(run.duration_seconds);
        const statusClass = getStatusClass(run.status);
        const branch = escHtml(run.branch || '-');
        const commitShort = run.commit_sha ? run.commit_sha.substring(0, 7) : '-';
        const commitMsg = escHtml(run.commit_message || '');
        const notable = run.notable ? 1 : 0;
        const tags = (run.tags || '').split(',').map(t => t.trim()).filter(Boolean);
        const isChecked = checkedIds.has(run.id);
        const notesOpen = expandedNotes.has(run.id);

        const tagsHtml = tags.map(t => `<span class="tag-chip">${escHtml(t)}</span>`).join('') +
            `<button class="tag-add-btn" data-run-id="${run.id}" title="Edit tags">&#43;</button>`;

        let row = `<tr data-run-id="${run.id}">`;
        row += `<td style="text-align:center"><button class="star-btn${notable ? ' notable' : ''}" data-run-id="${run.id}" title="${notable ? 'Remove star' : 'Star this run'}">${notable ? '&#9733;' : '&#9734;'}</button></td>`;
        row += `<td><input type="checkbox" class="compare-checkbox" data-run-id="${run.id}"${isChecked ? ' checked' : ''}></td>`;
        row += `<td class="muted" style="font-size:0.8rem">#${run.id}</td>`;
        row += `<td>${started}</td>`;
        row += `<td>${duration}</td>`;
        row += `<td><span class="status-badge status-${statusClass}">${run.status}</span></td>`;
        row += `<td><code>${branch}</code> <code title="${commitMsg}">${commitShort}</code></td>`;
        row += `<td class="tags-cell" data-run-id="${run.id}">${tagsHtml}</td>`;
        row += '<td class="run-actions">';
        row += `<button class="btn btn-secondary btn-sm notes-btn" data-run-id="${run.id}">${run.notes ? '&#128221;' : '+'} Notes</button>`;
        if (run.log_file_path) {
            row += ` <a href="/projects/${projectName}/history/${run.id}/log" class="btn btn-secondary btn-sm">Log</a>`;
        }
        row += '</td>';
        row += '</tr>';

        // Notes sub-row
        const notesText = escHtml(run.notes || '');
        row += `<tr class="notes-row" data-run-id="${run.id}" style="${notesOpen ? '' : 'display:none'}">`;
        row += `<td colspan="9"><textarea class="notes-editor" data-run-id="${run.id}" placeholder="Add post-run observations…">${notesText}</textarea></td>`;
        row += '</tr>';

        return row;
    }

    // ---------- Events ----------

    function attachFilterEvents() {
        document.getElementById('filter-notable-btn')?.addEventListener('click', () => {
            filterNotable = !filterNotable;
            renderAll();
        });

        document.getElementById('filter-tag-input')?.addEventListener('input', e => {
            filterTag = e.target.value;
            renderAll();
        });

        document.getElementById('compare-btn')?.addEventListener('click', openCompare);
    }

    function attachTableEvents() {
        // Star buttons
        listEl.querySelectorAll('.star-btn').forEach(btn => {
            btn.addEventListener('click', async () => {
                const runId = parseInt(btn.dataset.runId);
                const run = allRuns.find(r => r.id === runId);
                if (!run) return;
                run.notable = run.notable ? 0 : 1;
                await patchRun(runId, {notable: !!run.notable});
                renderAll();
            });
        });

        // Compare checkboxes
        listEl.querySelectorAll('.compare-checkbox').forEach(cb => {
            cb.addEventListener('change', () => {
                const runId = parseInt(cb.dataset.runId);
                if (cb.checked) {
                    if (checkedIds.size >= 2) { cb.checked = false; return; }
                    checkedIds.add(runId);
                } else {
                    checkedIds.delete(runId);
                }
                // Show/hide compare button without full re-render
                const bar = listEl.querySelector('.history-filter-bar');
                if (!bar) return;
                const existing = document.getElementById('compare-btn');
                if (checkedIds.size === 2 && !existing) {
                    const btn = document.createElement('button');
                    btn.id = 'compare-btn';
                    btn.className = 'btn btn-secondary btn-sm';
                    btn.textContent = 'Compare selected';
                    btn.addEventListener('click', openCompare);
                    bar.appendChild(btn);
                } else if (checkedIds.size !== 2 && existing) {
                    existing.remove();
                }
            });
        });

        // Notes toggle buttons
        listEl.querySelectorAll('.notes-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const runId = parseInt(btn.dataset.runId);
                const notesRow = listEl.querySelector(`.notes-row[data-run-id="${runId}"]`);
                if (!notesRow) return;
                const open = notesRow.style.display !== 'none';
                notesRow.style.display = open ? 'none' : '';
                if (!open) {
                    expandedNotes.add(runId);
                    notesRow.querySelector('.notes-editor')?.focus();
                } else {
                    expandedNotes.delete(runId);
                }
            });
        });

        // Notes editors — save on blur
        listEl.querySelectorAll('.notes-editor').forEach(ta => {
            ta.addEventListener('blur', async () => {
                const runId = parseInt(ta.dataset.runId);
                const run = allRuns.find(r => r.id === runId);
                if (!run) return;
                if (ta.value === (run.notes || '')) return;
                run.notes = ta.value;
                await patchRun(runId, {notes: ta.value});
                // Refresh the notes button icon without full re-render
                const btn = listEl.querySelector(`.notes-btn[data-run-id="${runId}"]`);
                if (btn) btn.innerHTML = (ta.value ? '&#128221;' : '+') + ' Notes';
            });
        });

        // Tag add buttons
        listEl.querySelectorAll('.tag-add-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const runId = parseInt(btn.dataset.runId);
                const run = allRuns.find(r => r.id === runId);
                if (!run) return;
                const cell = listEl.querySelector(`.tags-cell[data-run-id="${runId}"]`);
                if (cell) openTagEditor(cell, run);
            });
        });
    }

    function openTagEditor(cell, run) {
        const currentTags = run.tags || '';
        cell.innerHTML = `<input type="text" class="tag-editor-active" value="${escHtml(currentTags)}" placeholder="tag1,tag2…">`;
        const input = cell.querySelector('.tag-editor-active');
        input.focus();
        input.select();

        const save = async () => {
            const tags = input.value.split(',').map(t => t.trim()).filter(Boolean).join(',');
            run.tags = tags;
            await patchRun(run.id, {tags});
            renderAll();
        };

        input.addEventListener('blur', save);
        input.addEventListener('keydown', e => {
            if (e.key === 'Enter') { e.preventDefault(); save(); }
            if (e.key === 'Escape') { renderAll(); }
        });
    }

    // ---------- Compare ----------

    async function openCompare() {
        const [id1, id2] = [...checkedIds];
        const run1 = allRuns.find(r => r.id === id1);
        const run2 = allRuns.find(r => r.id === id2);
        if (!run1 || !run2) return;

        showCompareModal(run1, run2, null, true);

        try {
            const resp = await fetch(`/projects/${projectName}/history/diff?from=${id1}&to=${id2}`);
            const diffData = await resp.json();
            showCompareModal(run1, run2, diffData, false);
        } catch (e) {
            showCompareModal(run1, run2, {error: 'Failed to fetch diff'}, false);
        }
    }

    function showCompareModal(run1, run2, diffData, loading) {
        document.getElementById('history-compare-modal')?.remove();

        const overlay = document.createElement('div');
        overlay.id = 'history-compare-modal';
        overlay.className = 'compare-modal-overlay';
        overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

        const modal = document.createElement('div');
        modal.className = 'compare-modal';

        const diffHtml = loading
            ? '<p class="muted">Loading diff…</p>'
            : buildDiffSection(diffData);

        modal.innerHTML = `
            <div class="compare-modal-header">
                <h3 style="margin:0;color:var(--accent)">Compare Runs</h3>
                <button class="btn btn-secondary btn-sm" id="close-compare-btn">&#10005; Close</button>
            </div>
            <div class="compare-runs-grid">
                ${buildRunCard(run1, 'Run A')}
                ${buildRunCard(run2, 'Run B')}
            </div>
            <div id="compare-diff-container">${diffHtml}</div>
        `;

        overlay.appendChild(modal);
        document.body.appendChild(overlay);
        document.getElementById('close-compare-btn')?.addEventListener('click', () => overlay.remove());
    }

    function buildRunCard(run, label) {
        const started = new Date(run.started_at).toLocaleString();
        const duration = formatDuration(run.duration_seconds);
        const commitShort = run.commit_sha ? run.commit_sha.substring(0, 7) : '-';
        const tags = (run.tags || '').split(',').map(t => t.trim()).filter(Boolean)
            .map(t => `<span class="tag-chip">${escHtml(t)}</span>`).join('');

        return `<div class="compare-run-card">
            <h4>${label} &mdash; Run #${run.id}</h4>
            <div><strong>Started:</strong> ${started}</div>
            <div><strong>Duration:</strong> ${duration}</div>
            <div><strong>Status:</strong> <span class="status-badge status-${getStatusClass(run.status)}">${run.status}</span></div>
            <div><strong>Branch:</strong> <code>${escHtml(run.branch || '-')}</code></div>
            <div><strong>Commit:</strong> <code title="${escHtml(run.commit_message || '')}">${commitShort}</code>${run.commit_message ? ` <em>${escHtml(run.commit_message)}</em>` : ''}</div>
            ${tags ? `<div style="margin-top:4px"><strong>Tags:</strong> ${tags}</div>` : ''}
            ${run.notes ? `<div style="margin-top:6px"><strong>Notes:</strong><br><em style="color:var(--text-secondary)">${escHtml(run.notes)}</em></div>` : ''}
        </div>`;
    }

    function buildDiffSection(diffData) {
        if (!diffData) return '';
        if (diffData.error) return `<p class="muted" style="margin-top:0.5rem">${escHtml(diffData.error)}</p>`;
        if (diffData.same_commit) {
            return `<p class="muted" style="margin-top:0.5rem">Both runs used the same commit (<code>${diffData.from_sha}</code>) — no code changes between them.</p>`;
        }
        if (!diffData.diff) {
            return '<p class="muted" style="margin-top:0.5rem">No diff available.</p>';
        }

        const lines = diffData.diff.split('\n');
        let inner = '';
        lines.forEach(line => {
            let cls = '';
            if (line.startsWith('+++') || line.startsWith('---')) cls = 'diff-line-meta';
            else if (line.startsWith('@@')) cls = 'diff-line-header';
            else if (line.startsWith('+')) cls = 'diff-line-add';
            else if (line.startsWith('-')) cls = 'diff-line-remove';
            const escaped = escHtml(line);
            inner += cls ? `<span class="${cls}">${escaped}</span>\n` : `${escaped}\n`;
        });

        return `<div class="diff-section-label" style="margin-top:0.75rem">
            Code diff: <code>${diffData.from_sha}</code> &rarr; <code>${diffData.to_sha}</code>
        </div>
        <div class="diff-view">${inner}</div>`;
    }

    // ---------- Clear history ----------

    if (clearBtn) {
        clearBtn.addEventListener('click', async () => {
            if (!confirm('Delete all run history for this project? This cannot be undone.')) return;
            try {
                const resp = await fetch(`/projects/${projectName}/history/clear`, {method: 'POST'});
                if (resp.ok) {
                    allRuns = [];
                    checkedIds.clear();
                    expandedNotes.clear();
                    renderAll();
                } else {
                    alert('Failed to clear history');
                }
            } catch (e) {
                alert('Error clearing history');
            }
        });
    }

    // ---------- Utilities ----------

    function formatDuration(seconds) {
        if (!seconds) return '-';
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = seconds % 60;
        return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    }

    function getStatusClass(status) {
        const map = {running: 'running', completed: 'ready', crashed: 'error', canceled: 'stopped'};
        return map[status] || 'idle';
    }

    function escHtml(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    // ---------- Init ----------

    loadHistory();

    const historySection = document.getElementById('history-section');
    const historyHeader = historySection?.querySelector('.collapsible-header');
    if (historyHeader) {
        historyHeader.addEventListener('click', () => setTimeout(loadHistory, 100));
    }
})();
