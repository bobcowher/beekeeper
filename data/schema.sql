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
    tensorboard_dir TEXT,  -- project-relative path for new runs, legacy workspace-relative path for old runs
    persistent_dir TEXT,  -- project-relative path: persistent/runs/run_{id}

    -- Annotations
    notes      TEXT    NOT NULL DEFAULT '',
    notable    INTEGER NOT NULL DEFAULT 0,
    tags       TEXT    NOT NULL DEFAULT '',

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_runs_project ON training_runs(project_name);
CREATE INDEX IF NOT EXISTS idx_runs_started ON training_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_project_started ON training_runs(project_name, started_at DESC);

-- Metric analyses table
CREATE TABLE IF NOT EXISTS metric_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    metric_name TEXT NOT NULL,

    -- Analysis summary
    trend TEXT,  -- 'improving', 'stable', 'unstable', 'insufficient_data'
    initial_value REAL,
    final_value REAL,
    best_value REAL,
    best_step INTEGER,
    improvement_percent REAL,

    -- Convergence detection
    converged BOOLEAN DEFAULT 0,
    convergence_step INTEGER,

    -- Anomaly detection
    anomaly_count INTEGER DEFAULT 0,
    anomaly_details TEXT,  -- JSON array

    -- Summary
    summary TEXT,

    -- Sampled data
    sampled_points TEXT,  -- JSON: 100 key points
    total_points INTEGER,

    parsed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (run_id) REFERENCES training_runs(id) ON DELETE CASCADE,
    UNIQUE(run_id, metric_name)
);

CREATE INDEX IF NOT EXISTS idx_metric_analyses_run ON metric_analyses(run_id);
