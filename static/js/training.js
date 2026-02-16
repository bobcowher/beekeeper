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

    // --- Collapsible sections ---

    document.querySelectorAll(".collapsible-header").forEach(header => {
        header.addEventListener("click", () => {
            const targetId = header.dataset.target;
            const body = document.getElementById(targetId);
            const icon = header.querySelector(".collapse-icon");
            if (!body) return;

            const isHidden = body.style.display === "none";
            body.style.display = isHidden ? "block" : "none";
            icon.textContent = isHidden ? "\u25BC" : "\u25B6";

            if (isHidden) {
                if (targetId === "logs-body") {
                    loadLogs();
                }
                if (targetId === "tb-body") {
                    const iframe = document.getElementById("tensorboard-frame");
                    if (iframe && iframe.dataset.src && !iframe.src) {
                        iframe.src = iframe.dataset.src;
                    }
                }
            } else {
                if (targetId === "logs-body" && eventSource) {
                    eventSource.close();
                    eventSource = null;
                }
            }
        });
    });

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

    // --- Log loading ---

    function loadLogs() {
        if (!logTerminal) return;

        if (config.status === "running") {
            // Live stream via SSE
            startLogStream();
        } else {
            // Load the saved log file contents
            loadLogFile();
        }
    }

    async function loadLogFile() {
        if (!logTerminal) return;
        try {
            const resp = await fetch(`/projects/${name}/logs/stream`);
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                const text = decoder.decode(value, { stream: true });
                // Parse SSE data lines
                for (const line of text.split("\n")) {
                    if (line.startsWith("data: ")) {
                        logTerminal.textContent += line.slice(6) + "\n";
                    }
                }
                logTerminal.scrollTop = logTerminal.scrollHeight;
            }
        } catch (e) {
            // ignore — file may not exist
        }
    }

    // --- Log streaming via SSE ---

    function startLogStream() {
        if (eventSource) return;
        if (!logTerminal) return;

        eventSource = new EventSource(`/projects/${name}/logs/stream`);

        eventSource.onmessage = (e) => {
            logTerminal.textContent += e.data + "\n";
            logTerminal.scrollTop = logTerminal.scrollHeight;
        };

        eventSource.addEventListener("done", () => {
            eventSource.close();
            eventSource = null;
        });

        eventSource.onerror = () => {
            eventSource.close();
            eventSource = null;
            setTimeout(() => {
                const logsBody = document.getElementById("logs-body");
                if (logsBody && logsBody.style.display !== "none" && config.status === "running") {
                    startLogStream();
                }
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

    setInterval(async () => {
        try {
            const resp = await fetch(`/projects/${name}/status`);
            if (!resp.ok) return;
            const data = await resp.json();

            if (config.status === "running" && data.status !== "running") {
                location.reload();
            }
            if (config.status !== "running" && data.status === "running") {
                location.reload();
            }
        } catch (e) {
            // ignore
        }
    }, 3000);

    // --- Init ---

    if (config.status === "running" && elapsedEl) {
        updateElapsed();
        setInterval(updateElapsed, 1000);
    }

    // Auto-load logs if the section is already open (crashed/stopped state)
    const logsBody = document.getElementById("logs-body");
    if (logsBody && logsBody.style.display !== "none") {
        loadLogs();
    }
})();
