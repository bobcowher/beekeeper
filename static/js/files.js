// Beekeeper — File browser

(function () {
    const container = document.getElementById("files-body");
    if (!container) return;

    const name = container.dataset.project;
    const baseUrl = `/projects/${name}/files`;
    const viewUrl = `/projects/${name}/files/view`;
    const listing = document.getElementById("files-listing");
    const breadcrumbs = document.getElementById("files-breadcrumbs");

    let currentPath = "";
    let loaded = false;
    let currentEntries = [];
    let currentSort = { column: 'name', direction: 'asc' };

    // Expose load function for collapsible trigger
    window.loadFiles = function () {
        if (!loaded) {
            loaded = true;
            navigate("");
        }
    };

    async function navigate(path) {
        currentPath = path;
        listing.innerHTML = '<p class="muted">Loading...</p>';

        const url = path ? `${baseUrl}/${path}` : `${baseUrl}/`;
        try {
            const resp = await fetch(url);
            if (!resp.ok) {
                listing.innerHTML = '<p class="muted">Failed to load directory.</p>';
                return;
            }
            const data = await resp.json();
            currentEntries = data.entries;
            renderBreadcrumbs(data.path);
            renderListing(data.path);
        } catch (e) {
            listing.innerHTML = '<p class="muted">Error loading files.</p>';
        }
    }

    function renderBreadcrumbs(path) {
        let html = `<a href="#" class="fb-crumb" data-path="">workspace</a>`;
        if (path) {
            const parts = path.split("/");
            let cumulative = "";
            for (const part of parts) {
                cumulative = cumulative ? cumulative + "/" + part : part;
                html += ` <span class="fb-crumb-sep">/</span> `;
                html += `<a href="#" class="fb-crumb" data-path="${cumulative}">${part}</a>`;
            }
        }
        breadcrumbs.innerHTML = html;

        breadcrumbs.querySelectorAll(".fb-crumb").forEach(el => {
            el.addEventListener("click", (e) => {
                e.preventDefault();
                navigate(el.dataset.path);
            });
        });
    }

    const IMAGE_EXTS = new Set(["png","jpg","jpeg","gif","webp","svg","bmp","ico"]);
    const TEXT_EXTS  = new Set(["py","txt","log","json","yaml","yml","md","sh","csv",
                                 "tsv","ini","cfg","toml","js","css","html","xml",
                                 "rb","rs","go","java","c","cpp","h","ts"]);

    function fileExt(filename) {
        const dot = filename.lastIndexOf(".");
        return dot >= 0 ? filename.slice(dot + 1).toLowerCase() : "";
    }

    function isViewable(filename) {
        const ext = fileExt(filename);
        return IMAGE_EXTS.has(ext) || TEXT_EXTS.has(ext);
    }

    function formatTime(timestamp) {
        const date = new Date(timestamp * 1000);
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, '0');
        const day = String(date.getDate()).padStart(2, '0');
        const hours = String(date.getHours()).padStart(2, '0');
        const minutes = String(date.getMinutes()).padStart(2, '0');
        return `${year}-${month}-${day} ${hours}:${minutes}`;
    }

    function sortEntries() {
        const sorted = [...currentEntries];
        sorted.sort((a, b) => {
            // Directories always come first
            if (a.type !== b.type) {
                return a.type === "dir" ? -1 : 1;
            }

            let valA, valB;
            if (currentSort.column === 'name') {
                valA = a.name.toLowerCase();
                valB = b.name.toLowerCase();
            } else if (currentSort.column === 'size') {
                valA = a.size || 0;
                valB = b.size || 0;
            } else if (currentSort.column === 'modified') {
                valA = a.mtime;
                valB = b.mtime;
            }

            if (valA < valB) return currentSort.direction === 'asc' ? -1 : 1;
            if (valA > valB) return currentSort.direction === 'asc' ? 1 : -1;
            return 0;
        });
        return sorted;
    }

    function toggleSort(column) {
        if (currentSort.column === column) {
            currentSort.direction = currentSort.direction === 'asc' ? 'desc' : 'asc';
        } else {
            currentSort.column = column;
            currentSort.direction = 'asc';
        }
        renderListing(currentPath);
    }

    function copyCurlCommand(filePath) {
        const host = window.location.host;
        const url = `http://${host}${baseUrl}/${filePath}`;
        const command = `curl -O ${url}`;
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(command);
        } else {
            const ta = document.createElement('textarea');
            ta.value = command;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        }
    }

    function renderListing(dirPath) {
        if (!currentEntries.length) {
            listing.innerHTML = '<p class="muted">Empty directory.</p>';
            return;
        }

        const entries = sortEntries();
        const sortIcon = (col) => {
            if (currentSort.column !== col) return '';
            return currentSort.direction === 'asc' ? ' ▲' : ' ▼';
        };

        let html = '<table class="fb-table"><thead><tr>';
        html += `<th class="fb-col-name fb-sortable" data-column="name">Name${sortIcon('name')}</th>`;
        html += `<th class="fb-col-size fb-sortable" data-column="size">Size${sortIcon('size')}</th>`;
        html += `<th class="fb-col-modified fb-sortable" data-column="modified">Modified${sortIcon('modified')}</th>`;
        html += '<th class="fb-col-actions"></th>';
        html += '</tr></thead><tbody>';

        for (const entry of entries) {
            const icon = entry.type === "dir" ? "\uD83D\uDCC1" : "\uD83D\uDCC4";
            html += '<tr class="fb-row">';

            if (entry.type === "dir") {
                html += `<td class="fb-col-name">
                    <a href="#" class="fb-link fb-dir" data-path="${entry.path}">${icon} ${entry.name}/</a>
                </td>`;
                html += `<td class="fb-col-size muted">&mdash;</td>`;
                html += `<td class="fb-col-modified muted">${formatTime(entry.mtime)}</td>`;
                html += `<td class="fb-col-actions">
                    <div class="fb-menu">
                        <button class="fb-menu-trigger" title="Actions">⋮</button>
                        <div class="fb-menu-dropdown">
                            <a href="${baseUrl}/${entry.path}?zip=1" class="fb-menu-item">Download as zip</a>
                        </div>
                    </div>
                </td>`;
            } else {
                const viewable = isViewable(entry.name);
                html += `<td class="fb-col-name">
                    ${viewable
                        ? `<a href="#" class="fb-link fb-view" data-path="${entry.path}" data-name="${entry.name}">${icon} ${entry.name}</a>`
                        : `<span class="fb-file">${icon} ${entry.name}</span>`
                    }
                </td>`;
                html += `<td class="fb-col-size muted">${entry.size_h}</td>`;
                html += `<td class="fb-col-modified muted">${formatTime(entry.mtime)}</td>`;
                html += `<td class="fb-col-actions">
                    <div class="fb-menu">
                        <button class="fb-menu-trigger" title="Actions">⋮</button>
                        <div class="fb-menu-dropdown">
                            ${viewable ? `<button class="fb-menu-item fb-view" data-path="${entry.path}" data-name="${entry.name}">View</button>` : ""}
                            <a href="${baseUrl}/${entry.path}" class="fb-menu-item">Download</a>
                            <button class="fb-menu-item fb-copy-curl" data-path="${entry.path}">Copy curl download</button>
                        </div>
                    </div>
                </td>`;
            }

            html += '</tr>';
        }

        html += '</tbody></table>';

        // Zip-all button for current directory
        const zipUrl = dirPath ? `${baseUrl}/${dirPath}?zip=1` : `${baseUrl}/?zip=1`;
        html += `<div class="fb-footer">
            <a href="${zipUrl}" class="btn btn-secondary btn-sm">Download this folder as zip</a>
        </div>`;

        listing.innerHTML = html;

        // Sortable column headers
        listing.querySelectorAll(".fb-sortable").forEach(th => {
            th.addEventListener("click", () => {
                toggleSort(th.dataset.column);
            });
        });

        // Directory navigation
        listing.querySelectorAll(".fb-dir").forEach(el => {
            el.addEventListener("click", (e) => {
                e.preventDefault();
                navigate(el.dataset.path);
            });
        });

        // File viewer
        listing.querySelectorAll(".fb-view").forEach(el => {
            el.addEventListener("click", (e) => {
                e.preventDefault();
                openViewer(el.dataset.path, el.dataset.name);
            });
        });

        // Copy curl command
        listing.querySelectorAll(".fb-copy-curl").forEach(el => {
            el.addEventListener("click", (e) => {
                e.preventDefault();
                copyCurlCommand(el.dataset.path);
                // Close the menu
                el.closest(".fb-menu-dropdown").classList.remove("open");
            });
        });

        // Three-dot menu toggles
        listing.querySelectorAll(".fb-menu-trigger").forEach(trigger => {
            trigger.addEventListener("click", (e) => {
                e.stopPropagation();
                const menu = trigger.nextElementSibling;
                const isOpen = menu.classList.contains("open");

                // Close all menus
                document.querySelectorAll(".fb-menu-dropdown.open").forEach(m => {
                    m.classList.remove("open");
                });

                // Toggle this menu
                if (!isOpen) {
                    menu.classList.add("open");
                }
            });
        });

        // Close menus when clicking outside
        document.addEventListener("click", () => {
            document.querySelectorAll(".fb-menu-dropdown.open").forEach(m => {
                m.classList.remove("open");
            });
        });
    }

    // ── File Viewer ──────────────────────────────────────────────────────────

    let modal, modalTitle, modalContent, refreshTimer;

    function ensureModal() {
        if (modal) return;
        const el = document.createElement("div");
        el.id = "fv-modal";
        el.className = "fv-overlay";
        el.innerHTML = `
            <div class="fv-dialog">
                <div class="fv-header">
                    <span id="fv-title"></span>
                    <button id="fv-close" class="fv-close" title="Close">&times;</button>
                </div>
                <div id="fv-content" class="fv-content"></div>
            </div>`;
        document.body.appendChild(el);

        modal = el;
        modalTitle = document.getElementById("fv-title");
        modalContent = document.getElementById("fv-content");

        document.getElementById("fv-close").addEventListener("click", closeViewer);
        modal.addEventListener("click", (e) => { if (e.target === modal) closeViewer(); });
        document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeViewer(); });
    }

    function openViewer(filePath, filename) {
        ensureModal();
        clearInterval(refreshTimer);
        modalTitle.textContent = filename;
        modalContent.innerHTML = '<p class="muted">Loading...</p>';
        modal.classList.add("open");

        const ext = fileExt(filename);
        if (IMAGE_EXTS.has(ext)) {
            showImage(filePath);
        } else {
            showText(filePath);
        }
    }

    function closeViewer() {
        if (!modal) return;
        modal.classList.remove("open");
        clearInterval(refreshTimer);
        refreshTimer = null;
    }

    function showImage(filePath) {
        const url = `${viewUrl}/${filePath}`;
        const img = document.createElement("img");
        img.className = "fv-image";
        img.src = url + "?t=" + Date.now();
        modalContent.innerHTML = "";
        modalContent.appendChild(img);

        // Auto-refresh: swap src only after the new image loads (no flicker)
        refreshTimer = setInterval(() => {
            const next = new Image();
            next.onload = () => { img.src = next.src; };
            next.src = url + "?t=" + Date.now();
        }, 2000);
    }

    async function showText(filePath) {
        const url = `${viewUrl}/${filePath}`;
        try {
            const resp = await fetch(url);
            if (!resp.ok) {
                modalContent.innerHTML = `<p class="fv-error">Could not load file (${resp.status}).</p>`;
                return;
            }
            const text = await resp.text();
            const pre = document.createElement("pre");
            pre.className = "fv-text";
            pre.textContent = text;
            modalContent.innerHTML = "";
            modalContent.appendChild(pre);
        } catch (e) {
            modalContent.innerHTML = '<p class="fv-error">Error loading file.</p>';
        }
    }
})();
