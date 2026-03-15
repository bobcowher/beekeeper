// Beekeeper — Dashboard: sort and pin

(function () {
    const list = document.getElementById("project-list");
    if (!list) return;

    let currentSort = localStorage.getItem("beekeeperSort") || "recent";

    // ── Sort buttons ─────────────────────────────────────────────────────────

    document.querySelectorAll(".sort-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.sort === currentSort);
        btn.addEventListener("click", () => {
            currentSort = btn.dataset.sort;
            localStorage.setItem("beekeeperSort", currentSort);
            document.querySelectorAll(".sort-btn").forEach(b =>
                b.classList.toggle("active", b.dataset.sort === currentSort)
            );
            applySort();
        });
    });

    // ── Pin buttons ──────────────────────────────────────────────────────────

    list.addEventListener("click", async (e) => {
        const btn = e.target.closest(".pin-btn");
        if (!btn) return;
        e.preventDefault();

        const name = btn.dataset.project;
        const row = btn.closest(".project-row");

        try {
            const resp = await fetch(`/projects/${name}/pin`, { method: "POST" });
            if (!resp.ok) return;
            const data = await resp.json();
            row.dataset.pinned = data.pinned ? "true" : "false";
            btn.title = data.pinned ? "Unpin" : "Pin to top";
            btn.classList.toggle("pinned", data.pinned);
            applySort();
        } catch (err) {
            console.error("Pin toggle failed", err);
        }
    });

    // ── Sort / group logic ───────────────────────────────────────────────────

    function applySort() {
        const rows = Array.from(list.querySelectorAll(".project-row"));
        const pinned   = rows.filter(r => r.dataset.pinned === "true");
        const unpinned = rows.filter(r => r.dataset.pinned !== "true");

        unpinned.sort((a, b) => {
            if (currentSort === "alpha") {
                return a.dataset.name.localeCompare(b.dataset.name);
            }
            // recent: higher timestamp first; never-run (0) sinks to bottom
            const ta = parseFloat(a.dataset.lastRun) || 0;
            const tb = parseFloat(b.dataset.lastRun) || 0;
            if (ta === 0 && tb === 0) return a.dataset.name.localeCompare(b.dataset.name);
            return tb - ta;
        });

        // Remove existing section labels before re-inserting
        list.querySelectorAll(".project-section-label").forEach(el => el.remove());

        // Re-insert rows in order
        if (pinned.length) {
            const pinnedLabel = makeLabel("Pinned");
            list.prepend(pinnedLabel);
            pinned.forEach(r => list.appendChild(r));

            if (unpinned.length) {
                list.appendChild(makeLabel("Projects"));
            }
        }

        unpinned.forEach(r => list.appendChild(r));
    }

    function makeLabel(text) {
        const el = document.createElement("div");
        el.className = "project-section-label";
        el.textContent = text;
        return el;
    }

    applySort();
})();
