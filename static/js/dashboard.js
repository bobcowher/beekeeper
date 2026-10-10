// Beekeeper — Dashboard: sort and pin

(function () {
    const list = document.getElementById("project-list");
    if (!list) return;

    const COLLAPSE_KEY = "beekeeper.folders.collapsed";

    function loadCollapsed() {
        try { return new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "[]")); }
        catch (e) { return new Set(); }
    }
    function saveCollapsed(set) {
        try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...set])); } catch (e) { /* private mode */ }
    }
    let collapsed = loadCollapsed();

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
            // recent: use last_run_at, fall back to created_at for never-run projects
            const ta = parseFloat(a.dataset.lastRun) || parseFloat(a.dataset.createdAt) || 0;
            const tb = parseFloat(b.dataset.lastRun) || parseFloat(b.dataset.createdAt) || 0;
            if (ta === 0 && tb === 0) return a.dataset.name.localeCompare(b.dataset.name);
            return tb - ta;
        });

        list.querySelectorAll(".project-section-label").forEach(el => el.remove());
        rows.forEach(r => r.classList.remove("folder-collapsed"));

        if (pinned.length) {
            list.appendChild(makeLabel("Pinned"));
            pinned.forEach(r => list.appendChild(r));
        }

        const folderOf = r => r.dataset.folder || "";
        const folders = [...new Set(unpinned.map(folderOf).filter(Boolean))]
            .sort((a, b) => a.localeCompare(b));

        if (!folders.length) {
            if (pinned.length && unpinned.length) list.appendChild(makeLabel("Projects"));
            unpinned.forEach(r => list.appendChild(r));
            return;
        }

        const groups = folders.map(f => [f, unpinned.filter(r => folderOf(r) === f)]);
        const unfiled = unpinned.filter(r => !folderOf(r));
        if (unfiled.length) groups.push(["", unfiled]);

        groups.forEach(([folder, members]) => {
            const key = folder || "__unfiled__";
            const isCollapsed = collapsed.has(key);
            list.appendChild(makeFolderLabel(folder || "Unfiled", key, members.length, isCollapsed));
            members.forEach(r => {
                r.classList.toggle("folder-collapsed", isCollapsed);
                list.appendChild(r);
            });
        });
    }

    function makeFolderLabel(text, key, count, isCollapsed) {
        const el = makeLabel("");
        el.classList.add("folder-label");
        el.dataset.folderKey = key;
        el.setAttribute("role", "button");
        el.setAttribute("aria-expanded", isCollapsed ? "false" : "true");
        el.innerHTML = `<span class="collapse-icon">${isCollapsed ? "\u25B6" : "\u25BC"}</span> `;
        el.appendChild(document.createTextNode(text));
        const n = document.createElement("span");
        n.className = "muted folder-count";
        n.textContent = ` (${count})`;
        el.appendChild(n);
        el.tabIndex = 0;
        const toggle = () => {
            if (collapsed.has(key)) collapsed.delete(key); else collapsed.add(key);
            saveCollapsed(collapsed);
            applySort();
            const again = list.querySelector(`.folder-label[data-folder-key="${CSS.escape(key)}"]`);
            if (again) again.focus();
        };
        el.addEventListener("click", toggle);
        el.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
        });
        return el;
    }

    function makeLabel(text) {
        const el = document.createElement("div");
        el.className = "project-section-label";
        el.textContent = text;
        return el;
    }

    applySort();
})();
