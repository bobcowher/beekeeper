// static/js/pipeline_edit.js — Edit Pipeline page: YAML editor with validate and save.
(function () {
    const header = document.getElementById('pipeline-edit-header');
    if (!header) return;
    const NAME = header.dataset.name;
    const API = `/api/v1/pipelines/${encodeURIComponent(NAME)}`;
    const $ = id => document.getElementById(id);
    const editor = $('pipeline-yaml');
    let savedYaml = editor.value;

    function errorMessage(data, fallback) {
        return data && data.error && data.error.message ? data.error.message : fallback;
    }

    function showPageError(message) {
        const el = $('pipeline-page-error');
        el.textContent = message || '';
        el.hidden = !message;
    }

    async function api(url, opts) {
        let response;
        try {
            response = await fetch(url, opts);
        } catch (error) {
            throw new Error(`Network request failed: ${error.message}`);
        }
        try {
            return await response.json();
        } catch (error) {
            throw new Error(`Server returned an invalid response (HTTP ${response.status})`);
        }
    }

    function fitEditor() {
        editor.rows = Math.min(40, Math.max(12, editor.value.split('\n').length + 1));
    }

    function markDirty() {
        $('yaml-dirty').hidden = editor.value === savedYaml;
    }

    function showErrors(errors) {
        const list = $('yaml-errors');
        list.replaceChildren();
        errors.forEach(error => {
            const item = document.createElement('li');
            const path = document.createElement('code');
            path.textContent = error.path || '(root)';
            item.appendChild(path);
            item.append(` ${error.message}`);
            list.appendChild(item);
        });
    }

    editor.addEventListener('input', () => {
        fitEditor();
        markDirty();
        $('yaml-status').textContent = '';
    });

    window.addEventListener('beforeunload', event => {
        if (editor.value !== savedYaml) {
            event.preventDefault();
            event.returnValue = '';
        }
    });

    $('btn-yaml-validate').addEventListener('click', async event => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
            const data = await api('/api/v1/pipelines/validate', {
                method: 'POST', headers: { 'Content-Type': 'application/yaml' }, body: editor.value,
            });
            if (!data.success) throw new Error(errorMessage(data, 'Validation failed'));
            showErrors(data.data.errors);
            $('yaml-status').textContent = data.data.valid ? 'Valid' : '';
            showPageError();
        } catch (error) {
            showPageError(`Could not validate pipeline: ${error.message}`);
        } finally {
            button.disabled = false;
        }
    });

    $('btn-yaml-save').addEventListener('click', async event => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
            const text = editor.value;
            const data = await api(API, {
                method: 'PUT', headers: { 'Content-Type': 'application/yaml' }, body: text,
            });
            if (!data.success) {
                showErrors(data.error.details?.errors || [{ path: '', message: errorMessage(data, 'Save failed') }]);
                $('yaml-status').textContent = '';
                showPageError();
                return;
            }
            savedYaml = text;
            markDirty();
            showErrors([]);
            $('yaml-status').textContent = 'Saved';
            showPageError();
            location.href = `/pipelines/${encodeURIComponent(NAME)}`;
        } catch (error) {
            showPageError(`Could not save pipeline: ${error.message}`);
        } finally {
            button.disabled = false;
        }
    });

    fitEditor();
})();
