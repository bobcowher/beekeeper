This is a project called Beekeeper, focused on managing long-running(4-40 hour) AI training runs on a remote server. 

Non-functional requirements:
- Should be written in Python with Flask
- Should be light weight & performant
- Code should be simple, with clear structure, wherever possible. 
- The application will set its home directory to its install/checkout location. We will refer to this as BEEKEEPER_HOME. 

Theme:
- Should look like a more yellow VSCode spinoff. Simple, clear structure, with minimal logos. 

Core Functional Requirements:
- Users should be able to create Projects in the UI. A project will require 
    - A project name(no spaces)
    - A Git url
    - Default branch (default should be main)
    - A target Python version(dropdown)
    - The python file for training (default should be train.py)
    - The tensorboard log dir (default should be "runs")
    - Pip requirements file (default should be requirements.txt)
- Once the project is created, it should create a folder for the project under $BEEKEEPER_HOME/projects/$projectname
- Code will be checked out to $BEEKEEPER_HOME/projects/$projectname/src
- A virtual environment should be created for the project to run in. 
- Clicking on a project in the dashboard should result in going to a page for that project, with a clear back button. 
- Once a project is created, the user should have the ability to run or stop the project from the project page. 
- While the project is running, the user should have the option to see logs on the project page. 
- The user should have the option to see Tensorboard results for a project, in realtime, on the project page. 
- On the main page, the user should have the option to see the current GPU, CPU, and memory statistics for the host.

Things for later:
- We'll eventually need to figure out logins and security. For V1, we don't care.
- Auth to GitHub. For now, all GitHub projects used will be public.

---

# Agent Integration SDK - Software Specification

**Version:** 1.0
**Created:** 2026-03-22
**Status:** Ready for Implementation

## Overview

Add a downloadable Python SDK file that allows AI agents to integrate with Beekeeper projects with zero configuration. Agents download one `.py` file and can immediately start controlling training, fetching logs, and analyzing metrics.

## User Story

As an AI agent operator, I want to download a single Python file that contains everything needed to control a Beekeeper project, so I can quickly integrate training automation without manual configuration.

## Feature Requirements

### 1. API Endpoint

**Endpoint:** `GET /api/v1/projects/<name>/agent/sdk`

**Authentication:** API key required (same as other endpoints)

**Response:**
- Content-Type: `text/x-python` or `application/x-python`
- Content-Disposition: `attachment; filename="beekeeper_client_{project_name}.py"`
- Body: Generated Python SDK file

**Error Codes:**
- `PROJECT_NOT_FOUND` (404) - Project doesn't exist
- `UNAUTHORIZED` (401) - Invalid/missing API key

### 2. SDK File Structure

The generated Python file should contain:

#### A. Header Comments
```python
"""
Beekeeper Python SDK - Auto-generated for project: {project_name}

This file was generated on {timestamp} and contains everything needed
to control the Beekeeper training project via API.

Quick Start:
    from beekeeper_client_{project_name} import BeekeeperClient

    client = BeekeeperClient(api_key="your_api_key_here")
    client.start_training()
    metrics = client.get_latest_metrics()

Requirements:
    pip install requests

Documentation: {base_url}/api/v1/projects/{project_name}
"""
```

#### B. Configuration Constants
```python
# Project Configuration (auto-generated)
PROJECT_NAME = "{project_name}"
BASE_URL = "{base_url}"  # e.g., "http://192.168.1.57:5000"
API_VERSION = "v1"

# Project Details
GIT_URL = "{git_url}"
BRANCH = "{branch}"
SETUP_STATUS = "{setup_status}"
```

#### C. BeekeeperClient Class

**Constructor:**
```python
class BeekeeperClient:
    """Client for controlling Beekeeper training project: {project_name}"""

    def __init__(self, api_key: str, base_url: str = BASE_URL):
        """
        Initialize Beekeeper client.

        Args:
            api_key: Your Beekeeper API key (generate in web UI)
            base_url: API base URL (defaults to project server)
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.project_name = PROJECT_NAME
        self.session = requests.Session()
        self.session.headers.update({
            'X-API-Key': self.api_key,
            'Content-Type': 'application/json'
        })
```

**Methods to Include:**

1. **Training Control**
   - `start_training() -> dict` - Start training run
   - `stop_training() -> dict` - Stop training run
   - `get_training_status() -> dict` - Get current training status

2. **Logs**
   - `get_logs(tail: int = None) -> dict` - Get log content
   - `stream_logs() -> Iterator[str]` - Stream logs in real-time (SSE)

3. **Files**
   - `list_files(path: str = "") -> dict` - List workspace files
   - `download_file(filepath: str, destination: str = None) -> str` - Download file
   - `download_workspace(destination: str = "workspace.zip") -> str` - Download as zip

4. **TensorBoard Metrics**
   - `get_latest_metrics(detail: str = 'low', metrics: list = None) -> dict` - Latest run metrics
   - `get_run_metrics(run_id: int, detail: str = 'low', metrics: list = None) -> dict` - Specific run

5. **Run History**
   - `get_run_history(limit: int = 20) -> dict` - Get training runs
   - `get_run_details(run_id: int) -> dict` - Get specific run info
   - `download_run_log(run_id: int, destination: str = None) -> str` - Download run log
   - `clear_run_history() -> dict` - Clear all runs

6. **Project Info**
   - `get_project_info() -> dict` - Get project details
   - `get_system_stats() -> dict` - Get system stats (CPU, RAM, GPU)

7. **Helper Methods**
   - `_request(method: str, endpoint: str, **kwargs) -> dict` - Internal request handler
   - `_handle_response(response: Response) -> dict` - Response parser with error handling

#### D. Example Usage Section

At the bottom of the file, include comprehensive examples in comments:

```python
# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Initialize client
    client = BeekeeperClient(api_key="bk_your_api_key_here")

    # Example 1: Start training and monitor
    print("Starting training...")
    result = client.start_training()
    print(f"Training started: {result}")

    # Example 2: Stream logs in real-time
    print("\nStreaming logs:")
    for line in client.stream_logs():
        print(line, end='')

    # Example 3: Get latest metrics with analysis
    print("\nFetching latest metrics...")
    metrics = client.get_latest_metrics(detail='medium')
    for metric_name, data in metrics['data']['metrics'].items():
        print(f"{metric_name}:")
        print(f"  Trend: {data['trend']}")
        print(f"  Final value: {data['final_value']}")
        print(f"  Improvement: {data['improvement_percent']:.1f}%")
        print(f"  Summary: {data['summary']}")

    # Example 4: Download specific file
    print("\nDownloading training script...")
    client.download_file('train.py', destination='./train.py')

    # Example 5: Get run history
    print("\nRecent training runs:")
    history = client.get_run_history(limit=5)
    for run in history['data']['runs']:
        print(f"Run #{run['id']}: {run['status']} - {run['started_at']}")

    # Example 6: Stop training
    print("\nStopping training...")
    result = client.stop_training()
    print(f"Training stopped: {result}")
```

### 3. Implementation Details

#### A. New Endpoint in routes/api_v1.py

Add after the existing endpoints (~line 600):

```python
@api_v1_bp.route("/projects/<name>/agent/sdk")
@api_key_required
def download_agent_sdk(name):
    """Generate and download Python SDK for AI agent integration."""
    from services.agent_sdk_generator import generate_sdk
    from flask import Response

    project, error = load_project(name)
    if error:
        return error

    # Generate SDK content
    sdk_content = generate_sdk(
        project_name=name,
        base_url=request.url_root.rstrip('/'),
        project=project
    )

    # Return as downloadable file
    filename = f"beekeeper_client_{name.replace('-', '_')}.py"
    return Response(
        sdk_content,
        mimetype='text/x-python',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"'
        }
    )
```

#### B. New Service: services/agent_sdk_generator.py

Create new file with `generate_sdk()` function that:
1. Loads the SDK template
2. Substitutes project-specific values
3. Returns formatted Python code as string

Use Python's `textwrap.dedent()` and f-strings for clean code generation.

#### C. Template Approach

Two options:
1. **Inline template** - SDK template as string in `agent_sdk_generator.py`
2. **Template file** - Store in `templates/agent_sdk_template.py` and load/render

**Recommendation:** Inline template for simplicity (one less file to manage).

### 4. UI Integration

Add download button in project.html API section:

```html
<section class="card collapsible" id="api-section">
    <h2 class="collapsible-header" data-target="api-body">
        API
        <span class="collapse-icon">▶</span>
    </h2>
    <div class="collapsible-body" id="api-body" style="display:none">
        <!-- Download SDK Button -->
        <div style="margin-bottom: 1rem;">
            <a href="/api/v1/projects/{{ project.name }}/agent/sdk"
               class="btn btn-primary"
               download>
                📥 Download Python SDK
            </a>
            <span style="color: var(--text-secondary); margin-left: 1rem;">
                One-file client for AI agents
            </span>
        </div>

        <!-- Existing curl examples -->
        <pre class="api-examples">...</pre>
    </div>
</section>
```

### 5. Documentation Updates

#### A. API_IMPLEMENTATION.md

Add new section:

```markdown
### Agent Integration

**Download Python SDK**
- `GET /api/v1/projects/<name>/agent/sdk`
- Downloads a ready-to-use Python client file
- Includes all API methods, type hints, examples
- Pre-configured for the specific project
- Requires: `pip install requests`

Usage:
```bash
# Download SDK
curl -O http://localhost:5000/api/v1/projects/demo-project/agent/sdk

# Use it
python3 -c "
from beekeeper_client_demo_project import BeekeeperClient
client = BeekeeperClient(api_key='bk_xxx')
client.start_training()
"
```
```

#### B. CHANGELOG.md

Add to 1.0.4-beta or create 1.0.5:

```markdown
### Agent Integration SDK

**Python SDK Download**
- AI agents can download a single-file Python SDK pre-configured for any project
- Includes all API methods with type hints and docstrings
- No manual configuration needed - project URL and details baked in
- Comprehensive usage examples included in file
- New endpoint: `GET /api/v1/projects/<name>/agent/sdk`
```

### 6. Testing Plan

#### Manual Testing
1. Create API key in web UI
2. Navigate to project API section
3. Click "Download Python SDK" button
4. Verify file downloads with correct name
5. Open file and verify:
   - Correct project name/URL in constants
   - All methods present
   - Examples are valid Python
6. Test SDK:
   ```bash
   pip install requests
   python3 beekeeper_client_test_project.py  # runs examples
   ```

#### Integration Testing
1. Test each SDK method against live API
2. Verify error handling
3. Test streaming logs (SSE)
4. Test file downloads
5. Test metrics parsing

### 7. Security Considerations

- **No API key in SDK**: Never embed actual API keys in generated file
- **Placeholder only**: Use `"your_api_key_here"` placeholder
- **HTTPS recommendation**: Include comment about using HTTPS in production
- **Input validation**: Sanitize project name for filename (no path traversal)

### 8. Future Enhancements (Out of Scope)

- TypeScript/JavaScript SDK generation
- Go SDK generation
- Automatic SDK versioning
- SDK package publishing (PyPI)
- OpenAPI/Swagger spec download

## Implementation Checklist

- [ ] Create `services/agent_sdk_generator.py`
- [ ] Implement `generate_sdk()` function with full template
- [ ] Add endpoint in `routes/api_v1.py`
- [ ] Add download button in `templates/project.html`
- [ ] Update `API_IMPLEMENTATION.md`
- [ ] Update `CHANGELOG.md`
- [ ] Test SDK generation for multiple projects
- [ ] Test all SDK methods against live API
- [ ] Verify file download works in browser
- [ ] Commit and deploy

## Files to Modify

1. **services/agent_sdk_generator.py** (NEW) - SDK generation logic
2. **routes/api_v1.py** - New endpoint
3. **templates/project.html** - Download button
4. **API_IMPLEMENTATION.md** - Documentation
5. **CHANGELOG.md** - Release notes

## Estimated Effort

- SDK template creation: 2 hours
- Endpoint implementation: 30 minutes
- UI integration: 15 minutes
- Testing: 1 hour
- Documentation: 30 minutes
- **Total: ~4 hours**

## Dependencies

- No new Python dependencies required
- SDK requires `requests` package (standard, widely available)

## Notes

- Keep SDK simple and focused - it's a convenience wrapper, not a complex framework
- Prioritize readability - agents should be able to understand the code easily
- Include extensive docstrings and examples - make it self-documenting
- Consider adding retry logic and better error messages in SDK methods
- Project-specific configuration makes onboarding instant (no manual setup) 
