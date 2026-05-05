# SonarCloud Fixes Plan

Generated: 2026-05-05  
Branch: sonar-fixes  
Project: bobcowher_beekeeper  

## Current State

**Quality Gate: FAILING** on three conditions:

| Condition | Actual | Threshold | Status |
|-----------|--------|-----------|--------|
| `new_security_rating` | E (5) | A (1) | FAIL |
| `new_duplicated_lines_density` | 3.7% | 3% | FAIL |
| `new_security_hotspots_reviewed` | 0% | 100% | FAIL |

**Total open issues: 282**

| Severity | Count |
|----------|-------|
| BLOCKER | 31 |
| CRITICAL | 53 |
| MAJOR | 129 |
| MINOR | 77 |

---

## Priority 1 — Fix Quality Gate (required to pass)

### 1a. Review security hotspots (11 hotspots)

**Effort: low** — doesn't require code changes, just triage on SonarCloud.

All 11 security hotspots must be marked as either "Safe" (if the risk is accepted/mitigated) or "Fixed" after code change. Currently 0% reviewed, threshold is 100%.

Action: Go to SonarCloud → Security Hotspots tab for bobcowher_beekeeper. Review each hotspot and mark status. Hotspots that are genuine risks need code fixes first (see Priority 2).

### 1b. Reduce duplication on new code (3.7% → < 3%)

**Effort: medium**

The `new_duplicated_lines_density` gate applies only to code changed since the quality gate baseline. The recent persistent-storage additions introduced duplication.

**Key files with duplication (`css:S4666`):**

- `static/css/style.css:1337` — duplicate `.form-group` selector (first at line 422)
- `static/css/style.css:1364` — duplicate `.form-actions` selector (first at line 433)

Fix: merge the duplicate CSS rules into the originals at lines 422 and 433. Remove the duplicate declarations at 1337 and 1364.

---

## Priority 2 — BLOCKER Security Issues (31)

### 2a. Path traversal — `pythonsecurity:S2083` (27 occurrences) — **CRITICAL RISK**

Sonar is flagging path construction from user-controlled data. However, Beekeeper already uses `_safe_path()` in `routes/files.py` with `os.path.realpath` + path separator boundary checks. The issue is that path construction in the **services** layer bypasses this guard.

**Affected files:**
- `services/process_manager.py` — multiple locations (path construction for workspace, log, run dirs)
- `services/run_storage_service.py:15,49` — persistent storage path construction
- Other services

**Action:** For each flagged location:
1. Verify whether a project name validation guard exists upstream (project names are validated on creation as `[a-zA-Z0-9_-]+` pattern — check that this is enforced).
2. Where user input reaches path construction without a prior validation boundary, add explicit validation or use `os.path.realpath` with a boundary assertion (same pattern as `_safe_path()`).
3. Where the input is already internal/trusted (e.g., project name from the database, not from the current request), add a `# noqa` or SonarCloud suppression comment with justification.

**Note:** Many of these may be false positives — Sonar cannot trace the validation chain through the request lifecycle. Each occurrence needs human review.

### 2b. OS command injection — `pythonsecurity:S2076` (1 occurrence)

**File:** `services/process_manager.py:1027`

Sonar flagged shell command construction from user-controlled data. Review the specific line — if using `subprocess` with a list (not shell=True), this is a false positive. If using a string command with `shell=True`, refactor to use a list of arguments.

### 2c. XSS via innerHTML — `jssecurity:S5696` (2 occurrences)

**Files:**
- `static/js/history.js:105` — `innerHTML` assignment
- `static/js/app.js:130` — `innerHTML` assignment

**Action:** Inspect each assignment:
- If injecting server-generated HTML that contains no user input, it's likely a false positive — add a SonarCloud suppression with justification.
- If any user-controlled strings (run tags, notes, project names) flow into these `innerHTML` assignments, switch to `textContent` for the user-supplied parts or use a proper sanitization function.

### 2d. Open redirect — `pythonsecurity:S5146` (1 occurrence)

**File:** `routes/auth.py:76`

A redirect target is being constructed from user-supplied data (likely the `next` query parameter for post-login redirect). 

**Fix:** Validate the redirect URL before using it. Accept only relative paths within the application (starts with `/`, no `//`, no scheme):

```python
def is_safe_redirect(url: str) -> bool:
    return url.startswith('/') and not url.startswith('//')
```

Or use Flask's `url_for` to build redirect targets rather than passing raw `next` values through.

---

## Priority 3 — CRITICAL Code Quality (53 issues)

### 3a. Magic string literals — `python:S1192` (17 occurrences)

**Effort: low** — mechanical refactor.

Repeated string literals that should be constants:

| String | Occurrences | Location |
|--------|-------------|----------|
| `"project.json"` | 7 | `routes/api_v1.py` |
| `"Run not found"` | 4 | `routes/training.py` |
| `"mcp_server.py"` | 3 | `routes/api_v1.py` |
| Others | varies | multiple |

**Fix:** Define constants at the module level or in a `constants.py` module:

```python
PROJECT_FILE = "project.json"
RUN_NOT_FOUND_MSG = "Run not found"
```

### 3b. High cognitive complexity — `python:S3776` (26 occurrences)

**Effort: high** — requires real refactoring.

Functions with complexity > 15:

| Function | File | Complexity |
|----------|------|------------|
| unnamed | `services/process_manager.py:60` | 28 |
| unnamed | `routes/project.py:139` | 26 |
| unnamed | `services/project_service.py:51` | 18 |
| + 23 more | various | >15 |

**Action:** Tackle the highest-complexity functions first. Standard approaches:
- Extract helper functions for each logical sub-phase
- Replace nested if-chains with early returns
- Move validation logic into separate validators
- `process_manager.py` pre-launch sequence is a good first target — it already has named phases (git pull, pip install, launch) that can each become their own function

### 3c. Bare exception catches — `python:S5754` (2 occurrences)

**File:** `services/tensorboard_service.py:764,772`

**Fix:** Replace bare `except:` with `except Exception:` (or a more specific exception class).

### 3d. JS: deeply nested functions — `javascript:S2004` (5 occurrences)

**File:** `static/js/history.js:172,228,243` + 2 more

Nesting deeper than 4 levels. Extract inner callbacks/handlers into named functions at a higher scope.

### 3e. JS: cognitive complexity — `javascript:S3776` (2 occurrences)

**File:** `static/js/training.js:262,304`

Both functions exceed the 15-level threshold. Extract event handlers and status-update logic into smaller named functions.

### 3f. numpy: use `np.nonzero` — `python:S6729` (1 occurrence)

**File:** `services/tensorboard_service.py:435`

Replace `np.where(condition)` (single-argument form) with `np.nonzero(condition)`.

---

## Priority 4 — MAJOR Issues (selected high-value fixes)

### 4a. Flask routes missing HTTP method declarations — `python:S6965` (40 occurrences)

**Effort: low** — purely additive, no logic change.

All `@bp.route(...)` decorators in `routes/api_v1.py` and other route files need explicit `methods=[...]` arguments.

**Example fix:**
```python
# Before
@bp.route('/projects/<name>/runs')

# After
@bp.route('/projects/<name>/runs', methods=['GET'])
```

40 occurrences across `routes/api_v1.py` (most), plus other route files. Do this file by file.

### 4b. Path traversal (medium) — `pythonsecurity:S6549` (49 occurrences)

Same class of issue as 2a but at MAJOR severity. Triage each occurrence — many are likely false positives given the existing validation chain.

### 4c. Hardcoded password — `python:S2068` (1 occurrence)

**File:** `services/config_service.py:20`

The string `"password"` appears in a context Sonar interprets as a credential. Review the line:
- If it's a default config key name or example string (not a real password), add a suppression comment.
- If it's an actual hardcoded credential, move it to an environment variable.

### 4d. f-strings without placeholders — `python:S3457` (3 occurrences)

**Files:** `services/tensorboard_service.py:62,69`, `admin.py:209`

Remove the `f` prefix from strings that have no `{}` interpolation.

### 4e. Unused function parameter — `python:S1172` (1 occurrence)

**File:** `services/tensorboard_service.py:687`

Parameter `improvement_percent` is never read. Either use it or remove it from the signature.

### 4f. Return type mismatch — `python:S5886` (1 occurrence)

**File:** `services/process_manager.py:467`

Function `_archive_*` is declared to return `str` but can return `None`. Fix: either add a return value in all paths, or update the type hint to `str | None`.

### 4g. Floating point equality in tests — `python:S1244` (2 occurrences)

**File:** `tests/test_api.py:88,89`

**Fix:** Use `pytest.approx` or `math.isclose`:
```python
# Before
assert result == 1.5

# After
assert result == pytest.approx(1.5)
```

### 4h. CSS duplicate selectors — `css:S4666` (2 occurrences)

**File:** `static/css/style.css:1337,1364`

Merge `.form-group` rules at line 1337 into the original at line 422, and `.form-actions` at 1364 into 422+. Delete the duplicates. (Also fixes the quality gate duplication issue from Priority 1b.)

### 4i. setup.sh shell script quality — `shelldre:*` (9 occurrences)

- `S1066` — merge nested if into enclosing if (line 47)
- `S7677` — redirect error messages to stderr (`>&2`) at lines 49, 31
- `S7688` — use `[[` instead of `[` for conditionals (lines 56, 139, 149, + 3 more)

### 4j. HTML form label accessibility — `Web:S6853` (1 occurrence)

**File:** `templates/create_project.html:68`

A form label is not properly associated with its control. Add a matching `for` attribute to the `<label>` pointing to the control's `id`.

---

## Priority 5 — Minor / Low Risk

- JS: use optional chaining (`?.`) — `javascript:S6582` (6 occurrences in training.js)
- JS: prefer `element.remove()` over `parent.removeChild()` — `javascript:S7762` (2 occurrences)
- JS: top-level await vs async IIFE — `javascript:S7785` (3 occurrences in training.js)
- JS: nested ternary — `javascript:S3358` (1 occurrence)
- JS: truthy check always true — `javascript:S2589` (1 occurrence)
- CSS: contrast ratio — `css:S7924` (3 occurrences) — low contrast text
- `pyproject.toml`: missing lock file — `text:S8565`

---

## Recommended Order of Attack

1. **Review hotspots** on SonarCloud UI (no code changes, unblocks quality gate metric)
2. **CSS duplicate selectors** (`static/css/style.css:1337,1364`) — 15 minutes, fixes duplication gate
3. **Open redirect in auth.py** — 30 minutes, high security value, simple fix
4. **Flask route methods** (`python:S6965`) — 1-2 hours, purely additive, 40 occurrences
5. **Magic string constants** (`python:S1192`) — 1 hour, low risk
6. **XSS in JS** — 30 minutes, inspect and either fix or suppress
7. **Bare exception catches** — 15 minutes
8. **f-strings without placeholders** — 15 minutes
9. **Path traversal triage** — 2-3 hours, most will be suppressions with justification
10. **Cognitive complexity** — ongoing, tackle highest-complexity functions first

---

## Notes for the Implementing Agent

- Run `./sonar-status.sh` after each batch of fixes to track progress.
- Sonar re-analyzes on every push to `main` (via the GitHub Actions workflow in `.github/workflows/sonarcloud.yml`). Push to a PR branch to see differential results without affecting the main gate.
- Many `pythonsecurity:S2083`/`S6549` path traversal flags are likely false positives. The project name is validated as `[a-zA-Z0-9_-]+` at creation time. Where this validation is traceable, add a suppression comment: `# noqa` won't work for Sonar — use `# NOSONAR` inline or configure a `sonar.issue.ignore.multicriteria` rule in `sonar-project.properties`.
- The `new_security_rating` gate will pass once the security vulnerabilities introduced in new code (the persistent storage commits) are either fixed or suppressed with justification.
- Do not suppress blockers without understanding the actual risk. The path traversal issues in particular need individual review.
