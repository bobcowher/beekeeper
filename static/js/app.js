// Beekeeper — stats polling and rendering

(function () {
    const statsEl = document.getElementById("stats-content");
    if (!statsEl) return;

    const POLL_INTERVAL = 2000;

    function renderBar(percent, color) {
        return `<div class="stat-bar">
            <div class="stat-bar-fill" style="width:${percent}%;background:${color}"></div>
        </div>`;
    }

    function fmtPower(w) {
        return w < 100 ? w.toFixed(1) + " W" : Math.round(w) + " W";
    }

    function renderGpu(gpu) {
        const memPct = gpu.mem_percent;
        return `<div class="stat-group">
            <div class="stat-label">${gpu.name}</div>
            <div class="stat-row">
                <span class="stat-key">Utilization</span>
                ${renderBar(gpu.gpu_util, "var(--accent)")}
                <span class="stat-val">${gpu.gpu_util}%</span>
            </div>
            <div class="stat-row">
                <span class="stat-key">VRAM</span>
                ${renderBar(memPct, "var(--accent)")}
                <span class="stat-val">${gpu.mem_used_h} / ${gpu.mem_total_h}</span>
            </div>
            <div class="stat-row">
                <span class="stat-key">Temp</span>
                <span class="stat-val">${gpu.temp !== null ? gpu.temp + " °C" : "N/A"}</span>
            </div>
            <div class="stat-row">
                <span class="stat-key">Power</span>
                <span class="stat-val">${fmtPower(gpu.power)} / ${fmtPower(gpu.power_limit)}</span>
            </div>
        </div>`;
    }

    function renderCpu(cpu) {
        return `<div class="stat-group">
            <div class="stat-label">CPU (${cpu.count} cores${cpu.freq ? " @ " + Math.round(cpu.freq) + " MHz" : ""})</div>
            <div class="stat-row">
                <span class="stat-key">Usage</span>
                ${renderBar(cpu.percent, "var(--success)")}
                <span class="stat-val">${cpu.percent}%</span>
            </div>
        </div>`;
    }

    function renderMemory(mem) {
        return `<div class="stat-group">
            <div class="stat-label">System RAM</div>
            <div class="stat-row">
                <span class="stat-key">Usage</span>
                ${renderBar(mem.percent, "#7aa2f7")}
                <span class="stat-val">${mem.used_gb} / ${mem.total_gb} GiB</span>
            </div>
        </div>`;
    }

    function render(data) {
        let html = "";

        if (data.gpus && data.gpus.length) {
            data.gpus.forEach(g => { html += renderGpu(g); });
        } else {
            html += `<div class="stat-group"><p class="muted">No GPU detected</p></div>`;
        }

        html += renderCpu(data.cpu);
        html += renderMemory(data.memory);

        statsEl.innerHTML = html;
    }

    async function poll() {
        try {
            const resp = await fetch("/api/stats");
            if (resp.ok) {
                render(await resp.json());
            }
        } catch (e) {
            // silently retry next cycle
        }
    }

    // Initial fetch, then poll
    poll();
    setInterval(poll, POLL_INTERVAL);
})();
