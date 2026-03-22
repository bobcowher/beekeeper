// Run history display and management
(function() {
    const projectName = window.TRAINING_CONFIG?.name;
    if (!projectName) return;

    const listEl = document.getElementById('run-history-list');
    const clearBtn = document.getElementById('clear-history-btn');
    if (!listEl) return;

    // Load run history
    async function loadHistory() {
        try {
            const resp = await fetch(`/projects/${projectName}/history`);
            if (!resp.ok) {
                listEl.innerHTML = '<p class="muted">Failed to load run history.</p>';
                return;
            }

            const data = await resp.json();
            renderHistory(data.runs);
        } catch (e) {
            listEl.innerHTML = '<p class="muted">Error loading run history.</p>';
        }
    }

    function renderHistory(runs) {
        if (!runs || runs.length === 0) {
            listEl.innerHTML = '<p class="muted">No training runs yet.</p>';
            if (clearBtn) clearBtn.disabled = true;
            return;
        }

        if (clearBtn) clearBtn.disabled = false;

        let html = '<table class="run-history-table"><thead><tr>';
        html += '<th>Started</th>';
        html += '<th>Duration</th>';
        html += '<th>Status</th>';
        html += '<th>Commit</th>';
        html += '<th>Actions</th>';
        html += '</tr></thead><tbody>';

        runs.forEach(run => {
            const started = new Date(run.started_at);
            const startedStr = started.toLocaleString();
            const durationStr = formatDuration(run.duration_seconds);
            const statusClass = getStatusClass(run.status);
            const commitShort = run.commit_sha ? run.commit_sha.substring(0, 8) : 'unknown';
            const commitMsg = run.commit_message || '';

            html += '<tr>';
            html += `<td>${startedStr}</td>`;
            html += `<td>${durationStr}</td>`;
            html += `<td><span class="status-badge status-${statusClass}">${run.status}</span></td>`;
            html += `<td><code title="${commitMsg}">${commitShort}</code></td>`;
            html += '<td class="run-actions">';
            if (run.log_file_path) {
                html += `<a href="/projects/${projectName}/history/${run.id}/log" class="btn btn-secondary btn-sm">Download</a>`;
            } else {
                html += '<span class="muted">No log</span>';
            }
            html += '</td>';
            html += '</tr>';
        });

        html += '</tbody></table>';
        listEl.innerHTML = html;
    }

    function formatDuration(seconds) {
        if (!seconds) return '-';
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = seconds % 60;
        return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    }

    function getStatusClass(status) {
        const map = {
            'running': 'running',
            'completed': 'ready',
            'crashed': 'error',
            'canceled': 'stopped'
        };
        return map[status] || 'idle';
    }

    // Clear history button handler
    if (clearBtn) {
        clearBtn.addEventListener('click', async () => {
            if (!confirm('Delete all run history for this project? This cannot be undone.')) {
                return;
            }

            try {
                const resp = await fetch(`/projects/${projectName}/history/clear`, {
                    method: 'POST'
                });

                if (resp.ok) {
                    loadHistory();  // Refresh list
                } else {
                    alert('Failed to clear history');
                }
            } catch (e) {
                alert('Error clearing history');
            }
        });
    }

    // Load on page load
    loadHistory();

    // Reload when history section is expanded
    const historySection = document.getElementById('history-section');
    const historyHeader = historySection?.querySelector('.collapsible-header');
    if (historyHeader) {
        historyHeader.addEventListener('click', () => {
            setTimeout(loadHistory, 100);
        });
    }
})();
