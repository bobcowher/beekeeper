// Beekeeper — Training controls, log streaming, status polling

(function () {
    const config = window.TRAINING_CONFIG;
    if (!config) return;

    const name = config.name;
    const logTerminal = document.getElementById("log-terminal");
    const btnStart = document.getElementById("btn-start");
    const btnStop = document.getElementById("btn-stop");
    const btnClear = document.getElementById("btn-clear-log");
    const elapsedEl = document.getElementById("elapsed-time");

    let eventSource = null;
    let statusPollId = null;

    // --- Start / Stop ---

    if (btnStart) {
        btnStart.addEventListener("click", async () => {
            btnStart.disabled = true;
            btnStart.textContent = "Starting...";
            try {
                const resp = await fetch(`/projects/${name}/start`, { method: "POST" });
                const data = await resp.json();
                if (resp.ok) {
                    location.reload();
                } else {
                    alert(data.error || "Failed to start training");
                    btnStart.disabled = false;
                    btnStart.textContent = "Start Training";
                }
            } catch (e) {
                alert("Network error");
                btnStart.disabled = false;
                btnStart.textContent = "Start Training";
            }
        });
    }

    if (btnStop) {
        btnStop.addEventListener("click", async () => {
            if (!confirm("Stop training?")) return;
            btnStop.disabled = true;
            btnStop.textContent = "Stopping...";
            try {
                const resp = await fetch(`/projects/${name}/stop`, { method: "POST" });
                const data = await resp.json();
                if (resp.ok) {
                    location.reload();
                } else {
                    alert(data.error || "Failed to stop training");
                    btnStop.disabled = false;
                    btnStop.textContent = "Stop Training";
                }
            } catch (e) {
                alert("Network error");
                btnStop.disabled = false;
                btnStop.textContent = "Stop Training";
            }
        });
    }

    // --- Clear log display ---

    if (btnClear) {
        btnClear.addEventListener("click", () => {
            if (logTerminal) logTerminal.textContent = "";
        });
    }

    // --- Log streaming via SSE ---

    function startLogStream() {
        if (eventSource) return;
        if (!logTerminal) return;

        eventSource = new EventSource(`/projects/${name}/logs/stream`);

        eventSource.onmessage = (e) => {
            logTerminal.textContent += e.data + "\n";
            // Auto-scroll to bottom
            logTerminal.scrollTop = logTerminal.scrollHeight;
        };

        eventSource.addEventListener("done", () => {
            eventSource.close();
            eventSource = null;
        });

        eventSource.onerror = () => {
            eventSource.close();
            eventSource = null;
            // Retry after a delay
            setTimeout(() => {
                if (config.status === "running") startLogStream();
            }, 3000);
        };
    }

    // --- Elapsed time ---

    function updateElapsed() {
        if (!elapsedEl || !config.startedAt) return;
        const secs = Math.floor(Date.now() / 1000 - config.startedAt);
        const h = Math.floor(secs / 3600);
        const m = Math.floor((secs % 3600) / 60);
        const s = secs % 60;
        const parts = [];
        if (h) parts.push(h + "h");
        parts.push(m + "m");
        parts.push(s + "s");
        elapsedEl.textContent = parts.join(" ");
    }

    // --- Status polling ---

    function startStatusPoll() {
        statusPollId = setInterval(async () => {
            try {
                const resp = await fetch(`/projects/${name}/status`);
                if (!resp.ok) return;
                const data = await resp.json();

                // If status changed from running to something else, reload
                if (config.status === "running" && data.status !== "running") {
                    location.reload();
                }
                // If status changed from idle/stopped/crashed to running, reload
                if (config.status !== "running" && data.status === "running") {
                    location.reload();
                }
            } catch (e) {
                // ignore
            }
        }, 3000);
    }

    // --- Init ---

    if (config.status === "running") {
        startLogStream();
        if (elapsedEl) {
            updateElapsed();
            setInterval(updateElapsed, 1000);
        }
    }

    startStatusPoll();
})();
