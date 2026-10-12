// static/js/pipeline_edit.js — Edit Pipeline page: graphical step editor and YAML editor, as tabs.
//
// The server converts between the two (POST /api/v1/pipelines/validate), so this file never
// parses YAML. Each view keeps its own copy and converts only when the other side changed,
// which keeps YAML comments intact until someone actually edits in the graphical view.
(function () {
    const header = document.getElementById('pipeline-edit-header');
    if (!header) return;
    const NAME = header.dataset.name;
    const API = `/api/v1/pipelines/${encodeURIComponent(NAME)}`;
    const $ = id => document.getElementById(id);
    const editor = $('pipeline-yaml');
    const list = $('step-editor-list');

    const TOP_KEYS = ['name', 'folder', 'description', 'steps'];
    const STEP_KEYS = {
        train: ['name', 'type', 'project', 'branch', 'env_vars'],
        task: ['name', 'type', 'command', 'timeout_s'],
    };

    let mode = 'graphical';
    let model = null;          // {description, folder, steps: [step]}; see blankStep()
    let modelChanged = false;  // graphical edits not yet written into the YAML text
    let yamlChanged = false;   // YAML edits not yet loaded into the model
    let unsaved = false;
    let comments = countComments(editor.value);
    let commentNoticeShown = false;
    let projects = [];
    let nextKey = 0;
    const branchCache = {};

    // ── Helpers ─────────────────────────────────────────────────────────────

    function el(tag, attrs = {}, ...children) {
        const node = document.createElement(tag);
        Object.entries(attrs).forEach(([key, value]) => {
            if (value === undefined || value === null || value === false) return;
            if (key === 'className') node.className = value;
            else if (key === 'text') node.textContent = value;
            else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
            else node.setAttribute(key, value === true ? '' : value);
        });
        children.flat().forEach(child => child && node.append(child));
        return node;
    }

    function errorMessage(data, fallback) {
        return data && data.error && data.error.message ? data.error.message : fallback;
    }

    function showPageError(message) {
        const node = $('pipeline-page-error');
        node.textContent = message || '';
        node.hidden = !message;
    }

    function notice(message) {
        const node = $('editor-notice');
        node.textContent = message || '';
        node.hidden = !message;
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

    function countComments(text) {
        return text.split('\n').filter(line => /^\s*#/.test(line)).length;
    }

    function markUnsaved() {
        unsaved = true;
        $('yaml-dirty').hidden = false;
        $('yaml-status').textContent = '';
    }

    function markModelChanged() {
        modelChanged = true;
        markUnsaved();
        if (comments && !commentNoticeShown) {
            commentNoticeShown = true;
            notice(`Editing here rewrites the YAML and removes its ${comments} comment` +
                `${comments === 1 ? '' : 's'}. Make comment-sensitive changes on the YAML tab.`);
        }
    }

    // ── Model ↔ definition ──────────────────────────────────────────────────

    function blankStep(type = 'task') {
        return { key: nextKey++, name: '', type, project: '', branch: '', env: [], command: '', timeout: '' };
    }

    function uniqueStepName(base) {
        const taken = new Set(model.steps.map(step => step.name));
        for (let i = 1; ; i++) {
            if (!taken.has(`${base}-${i}`)) return `${base}-${i}`;
        }
    }

    const text = value => (value === undefined || value === null ? '' : String(value));
    const isMapping = value => value !== null && typeof value === 'object' && !Array.isArray(value);

    // A parsed definition the graphical view can show without dropping anything, or an Error saying why not.
    function modelFromParsed(parsed) {
        if (!isMapping(parsed)) return new Error('the YAML is not a mapping');
        const extraTop = Object.keys(parsed).filter(key => !TOP_KEYS.includes(key));
        if (extraTop.length) return new Error(`it has keys the editor doesn't know: ${extraTop.join(', ')}`);
        const steps = parsed.steps === undefined || parsed.steps === null ? [] : parsed.steps;
        if (!Array.isArray(steps)) return new Error('steps is not a list');
        const out = [];
        for (const [i, step] of steps.entries()) {
            if (!isMapping(step)) return new Error(`step ${i + 1} is not a mapping`);
            if (!STEP_KEYS[step.type]) return new Error(`step ${i + 1} has no type of train or task`);
            const extra = Object.keys(step).filter(key => !STEP_KEYS[step.type].includes(key));
            if (extra.length) return new Error(`step ${i + 1} has keys the editor doesn't know: ${extra.join(', ')}`);
            if (step.env_vars !== undefined && step.env_vars !== null && !isMapping(step.env_vars)) {
                return new Error(`step ${i + 1}'s env_vars is not a mapping`);
            }
            out.push({
                ...blankStep(step.type),
                name: text(step.name),
                project: text(step.project),
                branch: text(step.branch),
                env: Object.entries(step.env_vars || {}).map(([k, v]) => [k, text(v)]),
                command: text(step.command),
                timeout: text(step.timeout_s),
            });
        }
        return { description: text(parsed.description), folder: text(parsed.folder), steps: out };
    }

    function definitionFromModel() {
        const defn = { name: NAME };
        if (model.description.trim()) defn.description = model.description.trim();
        if (model.folder.trim()) defn.folder = model.folder.trim();
        defn.steps = model.steps.map(step => {
            if (step.type === 'train') {
                const out = { name: step.name, type: 'train' };
                if (step.project) out.project = step.project;   // absent reads as "required", not "invalid"
                if (step.branch.trim()) out.branch = step.branch.trim();
                const env = step.env.filter(([k, v]) => k.trim() || v);
                if (env.length) out.env_vars = Object.fromEntries(env.map(([k, v]) => [k.trim(), v]));
                return out;
            }
            const out = { name: step.name, type: 'task', command: step.command };
            const timeout = step.timeout.trim();
            if (timeout) out.timeout_s = /^\d+$/.test(timeout) ? Number(timeout) : timeout;
            return out;
        });
        return defn;
    }

    // ── Projects and branches ───────────────────────────────────────────────

    async function loadProjects() {
        try {
            const data = await api('/api/v1/projects');
            if (data.success) projects = data.data.projects;
        } catch (error) {
            projects = [];   // the dropdown still offers the step's current project
        }
    }

    function loadBranches(project) {
        if (!project) return Promise.resolve([]);
        if (!branchCache[project]) {
            branchCache[project] = api(`/api/v1/projects/${encodeURIComponent(project)}/branches`)
                .then(data => (data.success ? data.data.branches : []))
                .catch(() => []);
        }
        return branchCache[project];
    }

    function fillBranches(step, datalist) {
        loadBranches(step.project).then(branches => {
            datalist.replaceChildren(...branches.map(branch => el('option', { value: branch })));
        });
    }

    // ── Graphical view ──────────────────────────────────────────────────────

    function field(labelText, input, help) {
        return el('div', { className: 'form-group' },
            el('label', { for: input.id }, labelText, help ? ' ' : null,
                help ? el('span', { className: 'tooltip-icon', title: help, text: '?' }) : null),
            input);
    }

    function bindText(input, step, key) {
        input.value = step[key];
        input.addEventListener('input', () => {
            step[key] = input.value;
            clearFieldError(input);
            markModelChanged();
        });
        return input;
    }

    function fitRows(textarea) {
        textarea.rows = Math.min(20, Math.max(3, textarea.value.split('\n').length + 1));
    }

    function projectSelect(step, index) {
        const select = el('select', { id: `step-${step.key}-project`, 'data-path': `steps[${index}].project` });
        select.append(el('option', { value: '', text: 'Choose a project…' }));
        projects.forEach(project => {
            const ready = project.setup_status === 'ready';
            select.append(el('option', {
                value: project.name, text: ready ? project.name : `${project.name} (setup ${project.setup_status})`,
            }));
        });
        if (step.project && !projects.some(project => project.name === step.project)) {
            select.append(el('option', { value: step.project, text: `${step.project} (not found)` }));
        }
        select.value = step.project;
        return select;
    }

    function envRows(step, index) {
        const rows = el('div', { className: 'env-vars-list' });
        const addRow = (pair, focus) => {
            const key = el('input', { type: 'text', placeholder: 'KEY', 'aria-label': 'Variable name' });
            const value = el('input', { type: 'text', placeholder: 'value', 'aria-label': 'Variable value' });
            key.value = pair[0];
            value.value = pair[1];
            key.addEventListener('input', () => { pair[0] = key.value; markModelChanged(); });
            value.addEventListener('input', () => { pair[1] = value.value; markModelChanged(); });
            const remove = el('button', {
                type: 'button', className: 'btn btn-danger btn-sm btn-remove-env', 'aria-label': 'Remove variable',
                onclick: () => {
                    step.env.splice(step.env.indexOf(pair), 1);
                    row.remove();
                    markModelChanged();
                },
            }, '×');
            const row = el('div', { className: 'env-var-row form-row' },
                el('div', { className: 'form-group' }, key),
                el('div', { className: 'form-group' }, value),
                remove);
            rows.append(row);
            if (focus) key.focus();
        };
        step.env.forEach(pair => addRow(pair, false));
        const add = el('button', {
            type: 'button', className: 'btn btn-secondary btn-sm',
            onclick: () => {
                const pair = ['', ''];
                step.env.push(pair);
                addRow(pair, true);
                markModelChanged();
            },
        }, '+ Add Variable');
        return el('div', { className: 'form-group step-env', 'data-path': `steps[${index}].env_vars` },
            el('label', {}, 'Environment variables ',
                el('span', {
                    className: 'tooltip-icon', text: '?',
                    title: "Added to this training run's environment, on top of the project's own variables. Values are passed as-is, not shell-expanded.",
                })),
            rows, add);
    }

    function trainBody(step, index) {
        const select = projectSelect(step, index);
        const branchList = el('datalist', { id: `step-${step.key}-branches` });
        const branch = bindText(el('input', {
            type: 'text', id: `step-${step.key}-branch`, list: branchList.id,
            placeholder: 'project default', 'data-path': `steps[${index}].branch`,
        }), step, 'branch');
        select.addEventListener('change', () => {
            step.project = select.value;
            clearFieldError(select);
            markModelChanged();
            fillBranches(step, branchList);
        });
        fillBranches(step, branchList);
        return [
            el('div', { className: 'form-row' },
                field('Project', select, 'Queues a training run on this project and waits for it to finish. The run uses the project’s normal queue and settings.'),
                el('div', {}, field('Branch', branch, 'Optional. Leave empty to use the project’s own branch.'), branchList)),
            envRows(step, index),
        ];
    }

    function taskBody(step, index) {
        const command = bindText(el('textarea', {
            id: `step-${step.key}-command`, className: 'code-editor', spellcheck: 'false',
            'data-path': `steps[${index}].command`,
            placeholder: 'cp "$BEEKEEPER_PREV_STEP_DIR/model.pt" /models/',
        }), step, 'command');
        fitRows(command);
        command.addEventListener('input', () => fitRows(command));
        const timeout = bindText(el('input', {
            type: 'text', inputmode: 'numeric', id: `step-${step.key}-timeout`,
            placeholder: '3600', 'data-path': `steps[${index}].timeout_s`,
        }), step, 'timeout');
        return [
            field('Command', command, 'Run with bash -c in this run’s shared folder, $BEEKEEPER_PIPELINE_RUN_DIR. Earlier steps’ outputs are in $BEEKEEPER_PREV_STEP_DIR and $BEEKEEPER_STEP_<NAME>_DIR.'),
            el('div', { className: 'step-timeout' },
                field('Timeout (seconds)', timeout, 'Optional. The task is killed after this long. Default 3600.')),
        ];
    }

    function typeToggle(step) {
        const group = el('div', { className: 'segmented', role: 'group', 'aria-label': 'Step type' });
        ['train', 'task'].forEach(type => {
            group.append(el('button', {
                type: 'button', className: 'segmented-btn', 'aria-pressed': String(step.type === type),
                onclick: () => {
                    if (step.type === type) return;
                    step.type = type;
                    markModelChanged();
                    renderSteps();
                },
            }, type === 'train' ? 'Train' : 'Task'));
        });
        return group;
    }

    function stepCard(step, index) {
        const count = model.steps.length;
        const name = bindText(el('input', {
            type: 'text', className: 'step-editor-name', id: `step-${step.key}-name`,
            'aria-label': `Step ${index + 1} name`, placeholder: 'step-name',
            'data-path': `steps[${index}].name`,
        }), step, 'name');
        const move = (delta) => () => {
            model.steps.splice(index, 1);
            model.steps.splice(index + delta, 0, step);
            markModelChanged();
            renderSteps();
            list.querySelector(`[data-key="${step.key}"] .step-move-${delta < 0 ? 'up' : 'down'}`)?.focus();
        };
        const tools = el('div', { className: 'step-editor-tools' },
            el('button', {
                type: 'button', className: 'btn btn-secondary btn-sm step-move-up', disabled: index === 0,
                'aria-label': `Move step ${index + 1} up`, title: 'Move up', onclick: move(-1),
            }, '↑'),
            el('button', {
                type: 'button', className: 'btn btn-secondary btn-sm step-move-down', disabled: index === count - 1,
                'aria-label': `Move step ${index + 1} down`, title: 'Move down', onclick: move(1),
            }, '↓'),
            el('button', {
                type: 'button', className: 'btn btn-danger btn-sm step-delete',
                'aria-label': `Delete step ${index + 1}`, title: 'Delete step',
                onclick: () => {
                    model.steps.splice(index, 1);
                    markModelChanged();
                    renderSteps();
                },
            }, '×'));
        return el('li', { className: `step-editor step-editor-${step.type}`, 'data-key': step.key, 'data-path': `steps[${index}]` },
            el('div', { className: 'step-editor-head' },
                el('span', { className: 'step-editor-num', text: String(index + 1) }),
                name, typeToggle(step), tools),
            el('div', { className: 'step-editor-body' },
                step.type === 'train' ? trainBody(step, index) : taskBody(step, index)));
    }

    function insertButton(at) {
        return el('li', { className: 'step-insert' }, el('button', {
            type: 'button', className: 'step-insert-btn',
            'aria-label': at === model.steps.length ? 'Add step at the end' : `Add step before step ${at + 1}`,
            onclick: () => {
                const step = blankStep('task');
                step.name = uniqueStepName('step');
                model.steps.splice(at, 0, step);
                markModelChanged();
                renderSteps();
                $(`step-${step.key}-name`).select();
            },
        }, '+ Add step'));
    }

    function renderSteps() {
        list.replaceChildren();
        model.steps.forEach((step, index) => {
            if (index > 0) list.append(insertButton(index));
            list.append(stepCard(step, index));
        });
        list.append(insertButton(model.steps.length));
    }

    function renderGraphical() {
        $('g-description').value = model.description;
        $('g-folder').value = model.folder;
        renderSteps();
    }

    ['description', 'folder'].forEach(key => {
        const input = $(`g-${key}`);
        input.addEventListener('input', () => {
            model[key] = input.value;
            clearFieldError(input);
            markModelChanged();
        });
    });

    // ── Errors ──────────────────────────────────────────────────────────────

    function clearFieldError(input) {
        const holder = input.closest('[data-path]') || input;
        holder.classList.remove('invalid');
        if (holder.nextElementSibling?.classList.contains('field-error')) holder.nextElementSibling.remove();
    }

    // The closest field for an error path: steps[1].env_vars.FOO → that step's variables,
    // steps[1] → the step card, steps → nothing (it goes in the list).
    function fieldFor(path) {
        while (path) {
            const target = document.querySelector(`#panel-graphical [data-path="${CSS.escape(path)}"]`);
            if (target) return target;
            const shorter = path.replace(/(\.[^.[\]]+|\[\d+\])$/, '');
            if (shorter === path) return null;
            path = shorter;
        }
        return null;
    }

    function clearErrors() {
        $('yaml-errors').replaceChildren();
        document.querySelectorAll('.field-error').forEach(node => node.remove());
        document.querySelectorAll('.invalid').forEach(node => node.classList.remove('invalid'));
    }

    // In the graphical view an error sits under the field it names (steps[2].project → that
    // dropdown); anything without a field, like a YAML syntax error, goes in the list.
    function showErrors(errors) {
        clearErrors();
        const unplaced = [];
        errors.forEach(error => {
            const target = mode === 'graphical' ? fieldFor(error.path || '') : null;
            if (!target) {
                unplaced.push(error);
                return;
            }
            target.classList.add('invalid');
            const message = el('div', { className: 'field-error', role: 'alert', text: error.message });
            if (target.tagName === 'LI') target.querySelector('.step-editor-head').after(message);
            else target.after(message);
        });
        unplaced.forEach(error => {
            $('yaml-errors').append(el('li', {}, el('code', { text: error.path || '(root)' }), ` ${error.message}`));
        });
        const first = document.querySelector('.invalid, #yaml-errors li');
        if (first) first.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }

    // ── Tabs ────────────────────────────────────────────────────────────────

    function setMode(next) {
        mode = next;
        const graphical = next === 'graphical';
        $('panel-graphical').hidden = !graphical;
        $('panel-yaml').hidden = graphical;
        $('field-reference').hidden = graphical;
        [['tab-graphical', graphical], ['tab-yaml', !graphical]].forEach(([id, active]) => {
            $(id).classList.toggle('active', active);
            $(id).setAttribute('aria-selected', String(active));
        });
    }

    function validate(body) {
        const json = typeof body !== 'string';
        return api('/api/v1/pipelines/validate', {
            method: 'POST',
            headers: { 'Content-Type': json ? 'application/json' : 'application/yaml' },
            body: json ? JSON.stringify(body) : body,
        }).then(data => {
            if (!data.success) throw new Error(errorMessage(data, 'Validation failed'));
            return data.data;
        });
    }

    async function showYaml() {
        if (mode === 'yaml') return;
        if (modelChanged) {
            const result = await validate(definitionFromModel());
            editor.value = result.yaml;
            comments = 0;
            modelChanged = false;
        }
        clearErrors();
        notice('');
        setMode('yaml');
        fitEditor();
    }

    // Returns false, with the reason shown, when the YAML can't be shown graphically yet.
    async function showGraphical() {
        if (mode === 'graphical' && model) return true;
        if (yamlChanged || !model) {
            const result = await validate(editor.value);
            const loaded = result.parsed ? modelFromParsed(result.parsed) : new Error('it doesn’t parse');
            if (loaded instanceof Error) {
                setMode('yaml');
                showErrors(result.parsed ? [] : result.errors);
                notice(`The graphical editor can’t show this pipeline because ${loaded.message}. Fix it here, or keep editing YAML.`);
                return false;
            }
            model = loaded;
            yamlChanged = false;
            comments = countComments(editor.value);
            commentNoticeShown = false;
        }
        setMode('graphical');
        clearErrors();
        notice('');
        renderGraphical();
        return true;
    }

    async function switchTo(target, button) {
        button.disabled = true;
        try {
            showPageError('');
            if (target === 'yaml') await showYaml();
            else await showGraphical();
        } catch (error) {
            showPageError(`Could not switch views: ${error.message}`);
        } finally {
            button.disabled = false;
        }
    }

    $('tab-graphical').addEventListener('click', event => switchTo('graphical', event.currentTarget));
    $('tab-yaml').addEventListener('click', event => switchTo('yaml', event.currentTarget));

    // ── YAML view ───────────────────────────────────────────────────────────

    function fitEditor() {
        editor.rows = Math.min(40, Math.max(12, editor.value.split('\n').length + 1));
    }

    editor.addEventListener('input', () => {
        fitEditor();
        yamlChanged = true;
        markUnsaved();
    });

    // ── Validate / Save ─────────────────────────────────────────────────────

    window.addEventListener('beforeunload', event => {
        if (unsaved) {
            event.preventDefault();
            event.returnValue = '';
        }
    });

    $('btn-yaml-validate').addEventListener('click', async event => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
            const result = await validate(mode === 'graphical' ? definitionFromModel() : editor.value);
            showErrors(result.errors);
            $('yaml-status').textContent = result.valid ? 'Valid' : '';
            showPageError('');
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
            const graphical = mode === 'graphical';
            const data = await api(API, {
                method: 'PUT',
                headers: { 'Content-Type': graphical ? 'application/json' : 'application/yaml' },
                body: graphical ? JSON.stringify(definitionFromModel()) : editor.value,
            });
            if (!data.success) {
                showErrors(data.error.details?.errors || [{ path: '', message: errorMessage(data, 'Save failed') }]);
                $('yaml-status').textContent = '';
                showPageError('');
                return;
            }
            unsaved = false;
            $('yaml-status').textContent = 'Saved';
            showPageError('');
            location.href = `/pipelines/${encodeURIComponent(NAME)}`;
        } catch (error) {
            showPageError(`Could not save pipeline: ${error.message}`);
        } finally {
            button.disabled = false;
        }
    });

    // ── Start ───────────────────────────────────────────────────────────────

    fitEditor();
    loadProjects()
        .then(() => showGraphical())
        .catch(error => {
            setMode('yaml');
            showPageError(`Could not load the graphical editor: ${error.message}`);
        });
})();
