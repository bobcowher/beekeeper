-- Users table
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP,
    failed_login_attempts INTEGER DEFAULT 0,
    locked_until TIMESTAMP
);

-- Sessions table
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,           -- random UUID
    user_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- API keys table
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash TEXT PRIMARY KEY,     -- hash of the key
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,            -- user-provided name for the key
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);

-- Training runs table
CREATE TABLE IF NOT EXISTS training_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,

    -- Timing
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    duration_seconds INTEGER,

    -- Status
    status TEXT NOT NULL,  -- 'running', 'completed', 'crashed', 'canceled'
    exit_code INTEGER,

    -- Environment metadata
    commit_sha TEXT,
    commit_message TEXT,
    branch TEXT,
    python_version TEXT,
    gpu_info TEXT,
    hostname TEXT,

    -- Files
    log_file_path TEXT,  -- relative path: run_logs/run-{timestamp}-{id}.log
    tensorboard_dir TEXT,  -- relative path: workspace/runs/{timestamp}

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_runs_project ON training_runs(project_name);
CREATE INDEX IF NOT EXISTS idx_runs_started ON training_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_project_started ON training_runs(project_name, started_at DESC);
