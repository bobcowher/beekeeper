# Session Notes - 2026-03-24

## What We Accomplished Today

### 1. Agent Integration - Downloadable Instructions (Complete ✅)
- **Implemented:** Downloadable markdown file with agent instructions
- **Endpoint:** `GET /api/v1/projects/<name>/agent/instructions`
- **Returns:** Pre-configured `BEEKEEPER_<project>.md` file
- **UI:** API section now has Human/Agent collapsible subsections
  - Human section: curl examples
  - Agent section: Download button + curl command + endpoint preview

### 2. Enhanced Agent Instructions Content (Complete ✅)
Added guidance to help agents understand:
- **How to Use:** Clarifies to use curl via Bash (no dedicated client)
- **Before Taking Action:** Always check status before start/stop
- **Terminology:** Maps "check logs", "tensorboard", "progress" to appropriate endpoints
- **Understanding Metrics:** Detailed explanation of trend values, convergence, anomalies
  - `trend`: improving, stable, worsening, unstable
  - `converged` and `convergence_step`
  - `anomaly_count` and `anomalies` array
  - Example response structure

### 3. Fixed Run History Bug (Complete ✅)
**Bug:** Runs stayed stuck as "running" in the database after being stopped
- `stop_training()` was killing the process but not updating the DB
- Finalization only happened in monitor thread for natural exits

**Fix:** `stop_training()` now properly:
- Appends run footer to log
- Archives log file
- Calls `_finalize_run_record()` to update database

**Added:** `POST /api/v1/projects/<name>/runs/cleanup-orphaned` endpoint
- Marks stuck "running" runs as "canceled"
- Useful after server restarts

### 4. Updated Documentation
- Updated teaandrobots.com docs with API and Agent Integration sections
- Updated this session notes file

## Current State
- Branch: develop
- Latest commit: 4b085c3
- All changes deployed to lab (192.168.1.57:5000)
- User testing new training run with fixes

## Files Changed Today
- `routes/api_v1.py` - Agent instructions endpoint, cleanup-orphaned endpoint
- `services/process_manager.py` - Fixed stop_training() to finalize run records
- `templates/project.html` - Human/Agent collapsible sections in API

---

# Session Notes - 2026-03-23

## What We Accomplished

### 1. Agent Integration Feature (Complete ✅)
- **Implemented:** Agent Instructions section replacing SDK download
- **Why:** User realized downloadable SDKs meant managing multiple files per project - not natural for agent workflow
- **Solution:** Copy/paste instructions directly in project page API section
- **Status:** Working and deployed to develop branch (commit eb4b035)
- **Files changed:**
  - Removed: `services/agent_sdk_generator.py`
  - Modified: `routes/api_v1.py`, `templates/project.html`, `API_IMPLEMENTATION.md`, `CHANGELOG.md`

### 2. Updated CLAUDE.md (Complete ✅)
- Added URLs section (local, remote lab, docs path)
- Clarified deployment workflow (ask once per session, test locally first)
- Documented teaandrobots.com docs location: `/home/robertcowher/webapps/teaandrobots/content/software/beekeeper`
- Updated recent work section

### 3. Discovered sudoers issue (Complete ✅)
- Must use full path `/usr/bin/systemctl` for passwordless sudo
- Cannot use flags like `--no-pager` (not in sudoers whitelist)
- Working commands:
  - `sudo /usr/bin/systemctl restart beekeeper`
  - `sudo /usr/bin/systemctl status beekeeper`
  - `sudo /usr/bin/systemctl stop beekeeper`
  - `sudo /usr/bin/systemctl start beekeeper`

## What Broke (Reverted - but later fixed on 3/24)

### API Section Reorganization (commit e429d5a - REVERTED, then re-implemented)
**User requested:** Split API section into "Human" and "Agent" subsections, both minimized by default

**Problem on 3/23:** Localhost stopped loading completely after this change
- **Root cause:** Unknown at the time

**Resolution on 3/24:** Successfully re-implemented nested collapsibles - they work fine now.
