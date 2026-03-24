# Beekeeper REST API Implementation

## Summary

Successfully implemented a REST API at `/api/v1/` for programmatic access to Beekeeper. All endpoints return consistent JSON responses and reuse existing service layer code.

## Endpoints Implemented

### Projects
- `GET /api/v1/projects` - List all projects with setup and training status
- `GET /api/v1/projects/<name>` - Get detailed project info including training status

### Training Control
- `POST /api/v1/projects/<name>/training/start` - Start training
- `POST /api/v1/projects/<name>/training/stop` - Stop training
- `GET /api/v1/projects/<name>/training/status` - Get current training status

### Logs
- `GET /api/v1/projects/<name>/logs` - Get log content (`?tail=N` for last N lines)
- `GET /api/v1/projects/<name>/logs/stream` - Server-Sent Events (SSE) log streaming

### Files
- `GET /api/v1/projects/<name>/files` - List workspace root files
- `GET /api/v1/projects/<name>/files/<path>` - List subdirectory or download file
  - Returns directory listing for directories
  - Returns file download for files
  - Supports `?zip=1` to download directories as zip

### TensorBoard Metrics

- `GET /api/v1/projects/<name>/tensorboard/latest` - Get metrics analysis for latest completed run
  - Query params: `?detail=low|medium|high`, `?metrics=loss,accuracy`
  - Returns: Trend analysis, statistics, convergence detection, anomalies
- `GET /api/v1/projects/<name>/runs/<run_id>/metrics` - Get metrics for specific run
  - Same query params as above

### Agent Integration
Project pages include an "Agent Instructions" section with comprehensive API documentation tailored to each project. Users can copy/paste these instructions into their AI agent or save to a markdown file. Instructions include project-specific details (URL, name, git repo), all endpoints with response formats, and common workflows.

### System
- `GET /api/v1/stats` - System stats (CPU, RAM, GPU)

## Response Format

All endpoints return JSON with a consistent envelope:

```json
// Success
{
  "success": true,
  "data": { ... }
}

// Error
{
  "success": false,
  "error": {
    "code": "ERROR_CODE",
    "message": "Human-readable error message"
  }
}
```

## Error Codes

| Code | HTTP Status | Description |
|------|-------------|-------------|
| `NOT_FOUND` | 404 | Project or file not found |
| `FORBIDDEN` | 403 | Path traversal attempt or permission denied |
| `ALREADY_RUNNING` | 409 | Training already running |
| `NOT_RUNNING` | 409 | Cannot stop (not running) |
| `SETUP_INCOMPLETE` | 400 | Project setup not complete |
| `START_FAILED` | 400 | Failed to start training |
| `STOP_FAILED` | 400 | Failed to stop training |
| `READ_ERROR` | 500 | Failed to read log file |
| `LOAD_ERROR` | 500 | Failed to load project |
| `NO_TENSORBOARD_DATA` | 404 | No TensorBoard metrics found |
| `METRIC_NOT_FOUND` | 404 | Requested metric doesn't exist |
| `NO_COMPLETED_RUNS` | 404 | No completed runs available |
| `INVALID_PARAMETER` | 400 | Invalid query parameter value |
| `PARSE_ERROR` | 500 | Failed to parse TFEvents |

## Example Usage

```bash
# List all projects
curl http://localhost:5000/api/v1/projects

# Get project details
curl http://localhost:5000/api/v1/projects/demo-project

# Start training
curl -X POST http://localhost:5000/api/v1/projects/demo-project/training/start

# Check training status
curl http://localhost:5000/api/v1/projects/demo-project/training/status

# Get last 50 log lines
curl "http://localhost:5000/api/v1/projects/demo-project/logs?tail=50"

# Stream logs (SSE)
curl -N http://localhost:5000/api/v1/projects/demo-project/logs/stream

# List workspace files
curl http://localhost:5000/api/v1/projects/demo-project/files

# Download a file
curl -O http://localhost:5000/api/v1/projects/demo-project/files/train.py

# Download directory as zip
curl -o workspace.zip "http://localhost:5000/api/v1/projects/demo-project/files?zip=1"

# Get system stats
curl http://localhost:5000/api/v1/stats

# Get latest run metrics (summary only)
curl http://localhost:5000/api/v1/projects/demo-project/tensorboard/latest

# Get metrics with sampled points
curl "http://localhost:5000/api/v1/projects/demo-project/tensorboard/latest?detail=medium"

# Filter specific metrics
curl "http://localhost:5000/api/v1/projects/demo-project/tensorboard/latest?metrics=train/loss,val/loss"

# Get metrics for specific run
curl http://localhost:5000/api/v1/projects/demo-project/runs/42/metrics
```

### Agent Instructions

Each project page includes an "Agent Instructions" section in the API tab. This provides:
- Project-specific API documentation (URLs, endpoints, examples pre-filled with project name)
- All endpoints with request/response formats
- Common workflows for agents to follow
- Copy/paste ready for use with AI agents or saving to beekeeper.md

Users can simply copy the instructions and provide them to their AI agent for automated control.

```

### TensorBoard Metrics Response Format

**Low Detail (default):**
```json
{
  "success": true,
  "data": {
    "run_id": 42,
    "run_info": {
      "started_at": "2026-03-22 14:30:00",
      "ended_at": "2026-03-22 16:15:00",
      "status": "completed",
      "duration_seconds": 6300
    },
    "metrics": {
      "train/loss": {
        "trend": "improving",
        "initial_value": 2.345,
        "final_value": 0.234,
        "best_value": 0.189,
        "best_step": 8500,
        "improvement_percent": -90.02,
        "converged": true,
        "convergence_step": 7800,
        "anomaly_count": 2,
        "anomalies": [
          {"step": 2300, "value": 5.67, "reason": "spike_high"},
          {"step": 4100, "value": 4.23, "reason": "spike_high"}
        ],
        "summary": "train/loss improved by 90.0% from 2.35 to 0.23. Converged at step 7800. 2 anomalies detected.",
        "total_points": 10234
      }
    }
  }
}
```

**Medium Detail:**
Same as low, but includes `sampled_points` array (~100 key points):
```json
{
  "sampled_points": [
    {"step": 0, "value": 2.345, "wall_time": 1711114200.123},
    {"step": 100, "value": 2.189, "wall_time": 1711114215.456},
    ...
  ]
}
```

**High Detail:**
Same as medium (sampled points included). Full raw data is intentionally not included for performance reasons.
```

## Additional Work Completed

### 1. Setup Threading Bug Fix
Fixed a critical bug where project setup threads weren't executing. Added comprehensive error logging and proper exception handling to the `_setup_project` function in `services/project_service.py`.

**Changes:**
- Wrapped entire setup function in try/except
- Added logging at thread start and completion
- Ensured all exceptions are caught and saved to project status

**Verified:** Setup now works correctly - threads execute, git clone works, venv creation succeeds, and dependencies install.

### 2. Passwordless sudo for Service Management
Added optional passwordless sudo configuration to `setup.sh` for convenient service restarts during development.

**Changes:**
- Added interactive prompt in setup.sh asking if user wants passwordless sudo
- Creates `/etc/sudoers.d/beekeeper` with permissions for start/stop/restart/status
- Validates sudoers syntax before installing
- Allows quick `sudo systemctl restart beekeeper` without password

**Workflow:** Now when making code changes:
1. Edit code
2. Commit and push (on develop branch)
3. Run `sudo systemctl restart beekeeper` (no password needed)
4. Test changes immediately

### 3. Default Test Project
Created a working demo project using the correct repository:
- Repository: `https://github.com/bobcowher/beekeeper-test-project.git`
- Status: Ready (fully set up)
- Ready to test training and API functionality

## Files Modified

- `routes/api_v1.py` - New API blueprint (all endpoints)
- `app.py` - Registered api_v1_bp blueprint
- `services/project_service.py` - Fixed threading and added error logging
- `setup.sh` - Added optional passwordless sudo configuration

## Testing Completed

✅ All API endpoints tested and working
✅ Error handling verified (404, 403, 409, 400 responses)
✅ Consistent JSON envelope format confirmed
✅ Project setup threading fixed and verified
✅ Training start/stop via API tested
✅ Log retrieval tested (including `?tail=N` parameter)
✅ File browsing API tested
✅ System stats endpoint tested
✅ Passwordless sudo tested and working

## Future Enhancements (Phase 2)

Authentication will be added in a future phase, covering both API and web UI:
- API: Bearer token or API key in header
- Web UI: Login page with session cookies
- Shared authentication backend for both interfaces
