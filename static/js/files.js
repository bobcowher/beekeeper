// Beekeeper — File browser

(function () {
    const container = document.getElementById("files-body");
    if (!container) return;

    const name = container.dataset.project;
    const baseUrl = `/projects/${name}/files`;
    const listing = document.getElementById("files-listing");
    const breadcrumbs = document.getElementById("files-breadcrumbs");
    const curlBox = document.getElementById("files-curl");

    let currentPath = "";
    let loaded = false;

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
            renderBreadcrumbs(data.path);
            renderListing(data.entries, data.path);
            renderCurl(data.curl_examples);
        } catch (e) {
            listing.innerHTML = '<p class="muted">Error loading files.</p>';
        }
    }

    function renderBreadcrumbs(path) {
        let html = `<a href="#" class="fb-crumb" data-path="">src</a>`;
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

    function renderListing(entries, dirPath) {
        if (!entries.length) {
            listing.innerHTML = '<p class="muted">Empty directory.</p>';
            return;
        }

        let html = '<table class="fb-table"><thead><tr>';
        html += '<th class="fb-col-name">Name</th>';
        html += '<th class="fb-col-size">Size</th>';
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
                html += `<td class="fb-col-actions">
                    <a href="${baseUrl}/${entry.path}?zip=1" class="btn btn-secondary btn-sm" title="Download as zip">zip</a>
                </td>`;
            } else {
                html += `<td class="fb-col-name">
                    <span class="fb-file">${icon} ${entry.name}</span>
                </td>`;
                html += `<td class="fb-col-size muted">${entry.size_h}</td>`;
                html += `<td class="fb-col-actions">
                    <a href="${baseUrl}/${entry.path}" class="btn btn-secondary btn-sm">download</a>
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

        listing.querySelectorAll(".fb-dir").forEach(el => {
            el.addEventListener("click", (e) => {
                e.preventDefault();
                navigate(el.dataset.path);
            });
        });
    }

    function renderCurl(examples) {
        if (!curlBox) return;
        const host = location.host;
        const pathPart = currentPath ? `/${currentPath}` : "";
        const lines = [
            `# List files`,
            `curl http://${host}${baseUrl}${pathPart ? pathPart : "/"}`,
            ``,
            `# Download a file`,
            `curl -O http://${host}${baseUrl}/<filepath>`,
            ``,
            `# Download directory as zip`,
            `curl -o output.zip 'http://${host}${baseUrl}${pathPart || "/"}?zip=1'`,
        ];
        curlBox.textContent = lines.join("\n");
    }
})();
