// Nodes page: local server summary and the Runpod pool (spend, pods, history). Polls /nodes/status.
(function () {
    const PHASES = {
        provision: "starting", upload: "uploading", prepare: "installing",
        train: "training", finalize: "syncing",
    };

    function el(tag, attrs, ...children) {
        const node = document.createElement(tag);
        for (const [k, v] of Object.entries(attrs || {})) {
            if (k === "class") node.className = v;
            else if (k === "text") node.textContent = v;
            else node.setAttribute(k, v);
        }
        for (const c of children) if (c != null) node.append(c);
        return node;
    }

    // Small runs cost fractions of a cent; show enough digits that they don't read as free.
    const money = (v) => "$" + (v || 0).toFixed(v && v < 1 ? 3 : 2);

    function duration(s) {
        if (s == null) return "";
        s = Math.max(0, Math.floor(s));
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
        return h ? `${h}h${String(m).padStart(2, "0")}m` : `${m}m`;
    }

    function ago(ts) {
        if (!ts) return "never";
        return duration(Date.now() / 1000 - ts) + " ago";
    }

    function meter(label, s) {
        const cap = s.hard ?? s.soft;
        const pct = cap ? Math.min(100, (s.spent / cap) * 100) : 0;
        let cls = "meter-ok";
        if (s.hard != null && s.spent >= s.hard) cls = "meter-bad";
        else if (s.soft != null && s.spent >= s.soft) cls = "meter-warn";
        const cap2 = (v) => "$" + v.toFixed(2);
        const caps = [s.soft != null ? `${cap2(s.soft)} soft` : null, s.hard != null ? `${cap2(s.hard)} hard` : null]
            .filter(Boolean).join(" / ") || "no cap";
        return el("div", {class: "spend-meter"},
            el("div", {class: "meter-label", text: label}),
            el("div", {class: "meter-bar"}, el("i", {class: cls, style: `width:${pct}%`})),
            el("div", {class: "meter-value", text: `${money(s.spent)} · ${caps}`}));
    }

    async function terminate(pod) {
        if (!confirm(`Terminate ${pod.pod_name}? The run stops, its results get a short final sync, and the pod is deleted.`)) return;
        await fetch(`/nodes/pods/${encodeURIComponent(pod.pod_name)}/terminate`, {method: "POST"});
        refresh();
    }

    function podsTable(pods) {
        if (!pods.length) return el("p", {class: "muted", text: "No pods running."});
        const table = el("table", {class: "pods-table"});
        table.append(el("tr", {},
            ...["Pod", "Run", "Image", "$/hr", "Up", "Spent", "Last sync", ""].map(h => el("th", {text: h}))));
        for (const p of pods) {
            const phase = PHASES[p.phase] || p.phase;
            const detail = p.phase_detail && p.phase_detail !== phase ? p.phase_detail : "";
            const btn = el("button", {class: "btn btn-danger btn-sm", text: "Terminate"});
            btn.addEventListener("click", () => terminate(p));
            table.append(el("tr", {},
                el("td", {}, el("div", {text: p.gpu_type || "choosing GPU"}),
                    el("span", {class: `phase-tag phase-${p.phase}`, text: phase}),
                    detail ? el("div", {class: "muted small", text: detail}) : null),
                el("td", {}, el("a", {href: `/projects/${encodeURIComponent(p.project)}`, class: "mono", text: p.run_ref})),
                el("td", {class: "mono muted small", text: (p.image || "").replace(/^runpod\//, "")}),
                el("td", {class: "mono", text: p.cost_per_hr != null ? p.cost_per_hr.toFixed(2) : ""}),
                el("td", {class: "mono", text: duration(p.uptime_s)}),
                el("td", {class: "mono", text: money(p.spent)}),
                el("td", {class: "mono", text: p.phase === "train" || p.phase === "finalize" ? ago(p.last_sync_at) : ""}),
                el("td", {}, btn)));
        }
        return table;
    }

    function dailyChart(days) {
        if (!days.length) return el("p", {class: "muted", text: "No spend yet."});
        const max = Math.max(...days.map(d => d[1]), 0.01);
        const wrap = el("div", {class: "daily-bars"});
        for (const [day, usd] of days) {
            wrap.append(el("div", {class: "daily-bar", title: `${day}: ${money(usd)}`},
                el("i", {style: `height:${Math.max(2, (usd / max) * 100)}%`}),
                el("span", {text: day.slice(5)})));
        }
        return wrap;
    }

    function recentTable(rows) {
        if (!rows.length) return el("p", {class: "muted", text: "None yet."});
        const table = el("table", {class: "pods-table"});
        table.append(el("tr", {}, ...["Run", "GPU", "Spent", "Ended because"].map(h => el("th", {text: h}))));
        for (const r of rows) {
            table.append(el("tr", {},
                el("td", {}, el("a", {href: `/projects/${encodeURIComponent(r.run_ref.replace(/-\d+$/, ""))}`,
                                      class: "mono", text: r.run_ref})),
                el("td", {text: r.gpu_type || ""}),
                el("td", {class: "mono", text: money(r.spent)}),
                el("td", {class: "muted", text: r.stop_reason || "finished"})));
        }
        return table;
    }

    function render(data) {
        const local = data.local || {};
        document.getElementById("local-summary").textContent =
            `${(local.gpus || []).length} GPU${(local.gpus || []).length === 1 ? "" : "s"}` +
            ((local.gpus || []).length ? ` (${local.gpus.join(", ")})` : "") +
            ` · ${local.runs || 0} run${local.runs === 1 ? "" : "s"}`;

        const pods = data.pods || [];
        document.getElementById("runpod-summary").textContent =
            `${pods.length} pod${pods.length === 1 ? "" : "s"} · ${money(data.burn_rate)}/hr`;

        const meters = document.getElementById("spend-meters");
        meters.replaceChildren(
            meter("Today", data.spend.daily), meter("This month", data.spend.monthly), meter("Total", data.spend.total));

        document.getElementById("pods").replaceChildren(podsTable(pods));

        const s = data.settings;
        const gpus = s.gpu_types.length ? s.gpu_types.join(", ") : "cheapest in stock";
        document.getElementById("pool-foot").textContent =
            `GPU list: ${gpus} · ${s.cloud_type.toLowerCase()} cloud · max ${s.max_concurrent_pods} pods · ` +
            `${s.max_runtime_hours}h max per run · sync every ${s.sync_interval_min} min` +
            (s.api_key_set ? "" : " · NO API KEY SET");

        document.getElementById("daily-spend").replaceChildren(dailyChart(data.daily || []));
        document.getElementById("recent-runs").replaceChildren(recentTable(data.recent || []));
    }

    async function refresh() {
        try {
            const resp = await fetch("/nodes/status");
            if (resp.ok) render(await resp.json());
        } catch (e) { /* keep the last render */ }
    }

    render(JSON.parse(document.getElementById("initial-nodes").textContent));
    setInterval(refresh, 5000);
})();
