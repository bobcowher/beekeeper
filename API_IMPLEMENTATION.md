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
