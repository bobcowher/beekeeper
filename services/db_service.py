import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional
from contextlib import contextmanager

from models.user import User

# Runs a pipeline may still hand off: steps of running pipeline runs, and of each
# pipeline's latest run when it is failed/cancelled (resumable).
_PIPELINE_PINNED_RUNS_SQL = '''
    SELECT s.run_pk FROM pipeline_step_runs s
    JOIN pipeline_runs r ON r.id = s.pipeline_run_pk
    WHERE s.run_pk IS NOT NULL AND (
        r.status = 'running'
        OR (r.status IN ('failed', 'cancelled')
            AND r.run_number = (SELECT MAX(r2.run_number) FROM pipeline_runs r2
                                WHERE r2.pipeline_name = r.pipeline_name)))
'''


class DatabaseService:
    """Service for managing database operations."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_database()

    def _init_database(self):
        """Initialize database with schema."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        # Read schema
        schema_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'schema.sql')
        with open(schema_path, 'r') as f:
            schema = f.read()

        # Execute schema
        with self._get_connection() as conn:
            conn.executescript(schema)
            conn.commit()

        # Run migrations for existing databases
        self._run_migrations()

    @contextmanager
    def _get_connection(self):
        """Get database connection with row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _run_migrations(self):
        """Run database migrations for existing databases."""
        with self._get_connection() as conn:
            # Check if lockout columns exist
            cursor = conn.execute("PRAGMA table_info(users)")
            columns = [row['name'] for row in cursor.fetchall()]

            # Add lockout columns if missing
            if 'failed_login_attempts' not in columns:
                conn.execute('ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER DEFAULT 0')
            if 'locked_until' not in columns:
                conn.execute('ALTER TABLE users ADD COLUMN locked_until TIMESTAMP')

            # Check if metric_analyses table exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='metric_analyses'"
            )
            if not cursor.fetchone():
                # Create metric_analyses table
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS metric_analyses (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_pk INTEGER NOT NULL,
                        metric_name TEXT NOT NULL,
                        trend TEXT,
                        recent_trend TEXT,
                        initial_value REAL,
                        final_value REAL,
                        best_value REAL,
                        best_step INTEGER,
                        peak_value REAL,
                        peak_step INTEGER,
                        peak_reversal_pct REAL DEFAULT 0,
                        improvement_percent REAL,
                        converged BOOLEAN DEFAULT 0,
                        convergence_step INTEGER,
                        anomaly_count INTEGER DEFAULT 0,
                        anomaly_details TEXT,
                        summary TEXT,
                        sampled_points TEXT,
                        total_points INTEGER,
                        parsed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (run_pk) REFERENCES training_runs(id) ON DELETE CASCADE,
                        UNIQUE(run_pk, metric_name)
                    )
                ''')
                conn.execute(
                    'CREATE INDEX IF NOT EXISTS idx_metric_analyses_run_pk ON metric_analyses(run_pk)'
                )

            # Migrate existing databases: add new columns if missing
            for col, typedef in [
                ('recent_trend', 'TEXT'),
                ('peak_value', 'REAL'),
                ('peak_step', 'INTEGER'),
                ('peak_reversal_pct', 'REAL DEFAULT 0'),
                ('smoothed_points', 'TEXT'),
                ('smoothed_final_value', 'REAL'),
                ('ema_alpha', 'REAL'),
            ]:
                try:
                    conn.execute(f'ALTER TABLE metric_analyses ADD COLUMN {col} {typedef}')
                except Exception:
                    pass  # column already exists

            ma_columns = {row['name'] for row in conn.execute("PRAGMA table_info(metric_analyses)")}
            if 'run_id' in ma_columns and 'run_pk' not in ma_columns:
                conn.execute('ALTER TABLE metric_analyses RENAME COLUMN run_id TO run_pk')
            conn.execute('DROP INDEX IF EXISTS idx_metric_analyses_run')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_metric_analyses_run_pk ON metric_analyses(run_pk)')

            # Add annotation columns to training_runs if missing
            cursor = conn.execute("PRAGMA table_info(training_runs)")
            tr_columns = {row['name'] for row in cursor.fetchall()}
            for col, typedef in [
                ('notes',   "TEXT NOT NULL DEFAULT ''"),
                ('notable', 'INTEGER NOT NULL DEFAULT 0'),
                ('tags',    "TEXT NOT NULL DEFAULT ''"),
                ('persistent_dir', 'TEXT'),
                ('run_number', 'INTEGER'),
            ]:
                if col not in tr_columns:
                    try:
                        conn.execute(f'ALTER TABLE training_runs ADD COLUMN {col} {typedef}')
                    except Exception:
                        pass

            conn.execute('''CREATE TABLE IF NOT EXISTS run_counters (
                project_name TEXT PRIMARY KEY, last_number INTEGER NOT NULL)''')

            conn.execute('''CREATE TABLE IF NOT EXISTS run_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name TEXT NOT NULL,
                branch TEXT,
                env_vars TEXT,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                wait_reason TEXT,
                error TEXT,
                run_pk INTEGER,
                created_at TIMESTAMP NOT NULL,
                started_at TIMESTAMP)''')
            conn.execute('''CREATE INDEX IF NOT EXISTS idx_run_queue_project_status
                            ON run_queue(project_name, status)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS pipeline_run_counters (
                pipeline_name TEXT PRIMARY KEY, last_number INTEGER NOT NULL)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS pipeline_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pipeline_name TEXT NOT NULL,
                run_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                definition TEXT NOT NULL,
                current_step INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                started_at TIMESTAMP NOT NULL,
                ended_at TIMESTAMP,
                UNIQUE(pipeline_name, run_number))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS pipeline_step_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pipeline_run_pk INTEGER NOT NULL,
                step_index INTEGER NOT NULL,
                step_name TEXT NOT NULL,
                step_type TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                queue_id INTEGER,
                run_pk INTEGER,
                exit_code INTEGER,
                pid INTEGER,
                log_path TEXT,
                run_dir TEXT,
                error TEXT,
                started_at TIMESTAMP,
                ended_at TIMESTAMP,
                UNIQUE(pipeline_run_pk, step_index, attempt))''')

            conn.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

            # Backfill per-project run numbers for rows created before run_number existed.
            # Numbers continue after any already-assigned numbers in the same project.
            conn.execute('''
                UPDATE training_runs SET run_number = (
                    SELECT numbered.n FROM (
                        SELECT id,
                               COALESCE((SELECT MAX(run_number) FROM training_runs t2
                                         WHERE t2.project_name = t.project_name), 0)
                               + ROW_NUMBER() OVER (PARTITION BY project_name ORDER BY started_at, id) AS n
                        FROM training_runs t WHERE run_number IS NULL
                    ) AS numbered WHERE numbered.id = training_runs.id
                ) WHERE run_number IS NULL
            ''')
            conn.execute('''
                INSERT INTO run_counters (project_name, last_number)
                SELECT project_name, MAX(run_number) FROM training_runs
                WHERE run_number IS NOT NULL GROUP BY project_name
                ON CONFLICT(project_name) DO UPDATE
                    SET last_number = MAX(last_number, excluded.last_number)
            ''')
            conn.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS idx_training_runs_project_number '
                'ON training_runs(project_name, run_number)'
            )

            conn.commit()

    def migration_applied(self, name: str) -> bool:
        with self._get_connection() as conn:
            return conn.execute(
                'SELECT 1 FROM schema_migrations WHERE name = ?', (name,)
            ).fetchone() is not None

    def mark_migration_applied(self, name: str):
        with self._get_connection() as conn:
            conn.execute('INSERT OR IGNORE INTO schema_migrations (name) VALUES (?)', (name,))
            conn.commit()

    # User operations
    def get_user_by_email(self, email: str) -> Optional[User]:
        """Get user by email address."""
        with self._get_connection() as conn:
            row = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
            if not row:
                return None
            return User(
                id=row['id'],
                email=row['email'],
                name=row['name'],
                is_admin=bool(row['is_admin']),
                created_at=datetime.fromisoformat(row['created_at']),
                last_login_at=datetime.fromisoformat(row['last_login_at']) if row['last_login_at'] else None,
                failed_login_attempts=row.get('failed_login_attempts', 0),
                locked_until=datetime.fromisoformat(row['locked_until']) if row.get('locked_until') else None
            )

    def get_user_by_id(self, user_id: int) -> Optional[User]:
        """Get user by ID."""
        with self._get_connection() as conn:
            row = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
            if not row:
                return None
            return User(
                id=row['id'],
                email=row['email'],
                name=row['name'],
                is_admin=bool(row['is_admin']),
                created_at=datetime.fromisoformat(row['created_at']),
                last_login_at=datetime.fromisoformat(row['last_login_at']) if row['last_login_at'] else None,
                failed_login_attempts=row.get('failed_login_attempts', 0),
                locked_until=datetime.fromisoformat(row['locked_until']) if row.get('locked_until') else None
            )

    def create_user(self, email: str, name: str, password_hash: str, is_admin: bool = False) -> User:
        """Create a new user."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                'INSERT INTO users (email, name, password_hash, is_admin) VALUES (?, ?, ?, ?)',
                (email, name, password_hash, is_admin)
            )
            conn.commit()
            return self.get_user_by_id(cursor.lastrowid)

    def update_user(self, user_id: int, **fields):
        """Update user fields."""
        allowed_fields = ['name', 'email', 'password_hash', 'is_admin', 'last_login_at',
                         'failed_login_attempts', 'locked_until']
        updates = []
        values = []

        for field, value in fields.items():
            if field in allowed_fields:
                updates.append(f'{field} = ?')
                if isinstance(value, datetime):
                    values.append(value.isoformat())
                else:
                    values.append(value)

        if not updates:
            return

        values.append(user_id)
        query = f"UPDATE users SET {', '.join(updates)} WHERE id = ?"

        with self._get_connection() as conn:
            conn.execute(query, values)
            conn.commit()

    def delete_user(self, user_id: int):
        """Delete a user."""
        with self._get_connection() as conn:
            conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
            conn.commit()

    def get_user_count(self) -> int:
        """Get total number of users."""
        with self._get_connection() as conn:
            row = conn.execute('SELECT COUNT(*) as count FROM users').fetchone()
            return row['count']

    def count_admins(self) -> int:
        """Count number of admin users."""
        with self._get_connection() as conn:
            row = conn.execute('SELECT COUNT(*) as count FROM users WHERE is_admin = 1').fetchone()
            return row['count']

    def list_all_users(self) -> list[User]:
        """List all users."""
        with self._get_connection() as conn:
            rows = conn.execute('SELECT * FROM users ORDER BY created_at').fetchall()
            return [
                User(
                    id=row['id'],
                    email=row['email'],
                    name=row['name'],
                    is_admin=bool(row['is_admin']),
                    created_at=datetime.fromisoformat(row['created_at']),
                    last_login_at=datetime.fromisoformat(row['last_login_at']) if row['last_login_at'] else None,
                    failed_login_attempts=row.get('failed_login_attempts', 0),
                    locked_until=datetime.fromisoformat(row['locked_until']) if row.get('locked_until') else None
                )
                for row in rows
            ]

    def get_password_hash(self, user_id: int) -> Optional[str]:
        """Get user's password hash."""
        with self._get_connection() as conn:
            row = conn.execute('SELECT password_hash FROM users WHERE id = ?', (user_id,)).fetchone()
            return row['password_hash'] if row else None

    def record_failed_login(self, user_id: int, max_attempts: int = 5, lockout_minutes: int = 15):
        """Record a failed login attempt and lock account if threshold reached."""
        from datetime import timedelta

        with self._get_connection() as conn:
            # Increment failed attempts
            conn.execute(
                'UPDATE users SET failed_login_attempts = failed_login_attempts + 1 WHERE id = ?',
                (user_id,)
            )

            # Check if we should lock the account
            row = conn.execute('SELECT failed_login_attempts FROM users WHERE id = ?', (user_id,)).fetchone()
            if row and row['failed_login_attempts'] >= max_attempts:
                locked_until = datetime.now() + timedelta(minutes=lockout_minutes)
                conn.execute(
                    'UPDATE users SET locked_until = ? WHERE id = ?',
                    (locked_until.isoformat(), user_id)
                )

            conn.commit()

    def reset_failed_logins(self, user_id: int):
        """Reset failed login attempts and unlock account."""
        with self._get_connection() as conn:
            conn.execute(
                'UPDATE users SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?',
                (user_id,)
            )
            conn.commit()

    # Session operations
    def create_session(self, user_id: int, expires_at: datetime) -> str:
        """Create a new session."""
        import secrets
        session_id = secrets.token_urlsafe(32)

        with self._get_connection() as conn:
            conn.execute(
                'INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)',
                (session_id, user_id, expires_at.isoformat())
            )
            conn.commit()

        return session_id

    def get_session(self, session_id: str) -> Optional[tuple[int, datetime]]:
        """Get session info (user_id, expires_at)."""
        with self._get_connection() as conn:
            row = conn.execute(
                'SELECT user_id, expires_at FROM sessions WHERE id = ?',
                (session_id,)
            ).fetchone()

            if not row:
                return None

            expires_at = datetime.fromisoformat(row['expires_at'])
            if expires_at < datetime.now():
                # Session expired
                self.delete_session(session_id)
                return None

            return (row['user_id'], expires_at)

    def delete_session(self, session_id: str):
        """Delete a session."""
        with self._get_connection() as conn:
            conn.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
            conn.commit()

    def cleanup_expired_sessions(self):
        """Delete all expired sessions."""
        with self._get_connection() as conn:
            conn.execute('DELETE FROM sessions WHERE expires_at < ?', (datetime.now().isoformat(),))
            conn.commit()

    # API key operations
    def create_api_key(self, user_id: int, name: str) -> str:
        """Create a new API key and return the plaintext key."""
        import secrets
        import bcrypt

        # Generate key with format: bk_<random24chars>
        plaintext_key = f"bk_{secrets.token_urlsafe(24)}"
        key_hash = bcrypt.hashpw(plaintext_key.encode(), bcrypt.gensalt()).decode()

        with self._get_connection() as conn:
            conn.execute(
                'INSERT INTO api_keys (key_hash, user_id, name) VALUES (?, ?, ?)',
                (key_hash, user_id, name)
            )
            conn.commit()

        return plaintext_key

    def validate_api_key(self, key: str) -> Optional[User]:
        """Validate API key and return user if valid."""
        import bcrypt

        with self._get_connection() as conn:
            # Get all key hashes for comparison
            rows = conn.execute('SELECT key_hash, user_id FROM api_keys').fetchall()

            for row in rows:
                try:
                    if bcrypt.checkpw(key.encode(), row['key_hash'].encode()):
                        # Update last_used_at
                        conn.execute(
                            'UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?',
                            (datetime.now().isoformat(), row['key_hash'])
                        )
                        conn.commit()

                        # Return user
                        return self.get_user_by_id(row['user_id'])
                except Exception:
                    continue

            return None

    def delete_api_key(self, key_hash: str):
        """Delete an API key."""
        with self._get_connection() as conn:
            conn.execute('DELETE FROM api_keys WHERE key_hash = ?', (key_hash,))
            conn.commit()

    def list_user_api_keys(self, user_id: int) -> list[dict]:
        """List all API keys for a user."""
        with self._get_connection() as conn:
            rows = conn.execute(
                'SELECT key_hash, name, created_at, last_used_at FROM api_keys WHERE user_id = ? ORDER BY created_at DESC',
                (user_id,)
            ).fetchall()

            return [
                {
                    'key_hash': row['key_hash'],
                    'key_prefix': row['key_hash'][:16] + '...',  # Show prefix for identification
                    'name': row['name'],
                    'created_at': row['created_at'],
                    'last_used_at': row['last_used_at'],
                }
                for row in rows
            ]

    # Training run operations
    def create_training_run(self, project_name: str, metadata: dict) -> tuple[int, int]:
        """Create a run record with the project's next run number. Returns (run_pk, run_id)."""
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30)
        try:
            conn.execute('BEGIN IMMEDIATE')
            run_id = conn.execute(
                '''INSERT INTO run_counters (project_name, last_number) VALUES (?, 1)
                   ON CONFLICT(project_name) DO UPDATE SET last_number = last_number + 1
                   RETURNING last_number''',
                (project_name,)
            ).fetchone()[0]
            cursor = conn.execute(
                '''INSERT INTO training_runs
                   (project_name, run_number, started_at, status, commit_sha, commit_message,
                    branch, python_version, gpu_info, hostname)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    project_name,
                    run_id,
                    metadata['started_at'].isoformat(),
                    metadata['status'],
                    metadata.get('commit_sha'),
                    metadata.get('commit_message'),
                    metadata.get('branch'),
                    metadata.get('python_version'),
                    metadata.get('gpu_info'),
                    metadata.get('hostname'),
                )
            )
            conn.execute('COMMIT')
            return cursor.lastrowid, run_id
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def get_run(self, project_name: str, run_id: int) -> dict | None:
        """Resolve a project's run number to its full row, or None."""
        with self._get_connection() as conn:
            row = conn.execute(
                'SELECT * FROM training_runs WHERE project_name = ? AND run_number = ?',
                (project_name, run_id)
            ).fetchone()
            return dict(row) if row else None

    def delete_project_runs(self, project_name: str) -> int:
        """Delete a project's runs, their metric analyses, and its run counter."""
        with self._get_connection() as conn:
            # foreign_keys is off on these connections, so cascade does not fire
            conn.execute(
                '''DELETE FROM metric_analyses WHERE run_pk IN
                   (SELECT id FROM training_runs WHERE project_name = ?)''',
                (project_name,)
            )
            cursor = conn.execute('DELETE FROM training_runs WHERE project_name = ?', (project_name,))
            conn.execute('DELETE FROM run_counters WHERE project_name = ?', (project_name,))
            conn.commit()
            return cursor.rowcount

    def update_training_run(self, run_pk: int, **fields):
        """Update run fields (status, timing, metadata, etc.)."""
        allowed_fields = [
            'ended_at', 'duration_seconds', 'status', 'exit_code',
            'log_file_path', 'tensorboard_dir', 'persistent_dir',
            'commit_sha', 'commit_message', 'python_version', 'gpu_info', 'hostname',
        ]
        updates = []
        values = []

        for field, value in fields.items():
            if field in allowed_fields:
                updates.append(f'{field} = ?')
                if isinstance(value, datetime):
                    values.append(value.isoformat())
                else:
                    values.append(value)

        if not updates:
            return

        values.append(run_pk)
        query = f"UPDATE training_runs SET {', '.join(updates)} WHERE id = ?"

        with self._get_connection() as conn:
            conn.execute(query, values)
            conn.commit()

    def get_training_runs(self, project_name: str, limit: int = 20) -> list[dict]:
        """Fetch recent runs for a project, ordered by started_at DESC."""
        with self._get_connection() as conn:
            if limit is None:
                rows = conn.execute(
                    'SELECT * FROM training_runs WHERE project_name = ? ORDER BY started_at DESC',
                    (project_name,)
                ).fetchall()
            else:
                rows = conn.execute(
                    'SELECT * FROM training_runs WHERE project_name = ? ORDER BY started_at DESC LIMIT ?',
                    (project_name, limit)
                ).fetchall()

            return [dict(row) for row in rows]

    def get_training_run(self, run_pk: int) -> dict:
        """Fetch single run details."""
        with self._get_connection() as conn:
            row = conn.execute('SELECT * FROM training_runs WHERE id = ?', (run_pk,)).fetchone()
            return dict(row) if row else None

    def delete_training_run(self, run_pk: int):
        """Delete run record."""
        with self._get_connection() as conn:
            conn.execute('DELETE FROM training_runs WHERE id = ?', (run_pk,))
            conn.commit()

    def count_training_runs(self, project_name: str) -> int:
        """Count total runs for project."""
        with self._get_connection() as conn:
            row = conn.execute(
                'SELECT COUNT(*) as count FROM training_runs WHERE project_name = ?',
                (project_name,)
            ).fetchone()
            return row['count']

    def get_project_total_runtime(self, project_name: str) -> int:
        """Sum of duration_seconds across all runs for a project. Returns 0 if none."""
        with self._get_connection() as conn:
            row = conn.execute(
                'SELECT COALESCE(SUM(duration_seconds), 0) as total '
                'FROM training_runs WHERE project_name = ?',
                (project_name,)
            ).fetchone()
            return int(row['total']) if row else 0

    def update_run_annotations(self, run_pk: int, **fields):
        """Update notes, notable, and/or tags on a run record."""
        allowed = {'notes': str, 'notable': int, 'tags': str}
        updates, values = [], []
        for field, value in fields.items():
            if field in allowed:
                updates.append(f'{field} = ?')
                values.append(allowed[field](value))
        if not updates:
            return
        values.append(run_pk)
        with self._get_connection() as conn:
            conn.execute(f"UPDATE training_runs SET {', '.join(updates)} WHERE id = ?", values)
            conn.commit()

    def prune_old_runs(self, project_name: str, keep_last: int = 20) -> list[dict]:
        """Delete non-notable runs beyond retention limit. Notable runs are never pruned,
        nor are runs a pipeline may still hand off. Returns list of deleted runs."""
        with self._get_connection() as conn:
            # Only count/delete non-notable runs; notable runs are exempt
            rows = conn.execute(
                f'''SELECT * FROM training_runs
                   WHERE project_name = ? AND notable = 0
                     AND id NOT IN ({_PIPELINE_PINNED_RUNS_SQL})
                   ORDER BY started_at DESC
                   LIMIT -1 OFFSET ?''',
                (project_name, keep_last)
            ).fetchall()

            deleted_runs = [dict(row) for row in rows]

            if deleted_runs:
                ids_to_delete = [row['id'] for row in deleted_runs]
                placeholders = ','.join('?' * len(ids_to_delete))
                conn.execute(
                    f'DELETE FROM training_runs WHERE id IN ({placeholders})',
                    ids_to_delete
                )
                conn.commit()

            return deleted_runs

    def delete_all_runs(self, project_name: str) -> int:
        """Delete all run history for a project. Returns count deleted."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                'DELETE FROM training_runs WHERE project_name = ?',
                (project_name,)
            )
            conn.commit()
            return cursor.rowcount

    def rename_project_runs(self, old_name: str, new_name: str) -> int:
        """Update project_name on all run records. Returns count updated."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                'UPDATE training_runs SET project_name = ? WHERE project_name = ?',
                (new_name, old_name)
            )
            conn.execute(
                'UPDATE run_counters SET project_name = ? WHERE project_name = ?',
                (new_name, old_name)
            )
            conn.commit()
            return cursor.rowcount

    # Metric analysis operations
    def save_metric_analysis(self, run_pk: int, metric_name: str, analysis_data: dict):
        """Insert or replace metric analysis."""
        import json

        with self._get_connection() as conn:
            conn.execute(
                '''INSERT OR REPLACE INTO metric_analyses
                   (run_pk, metric_name, trend, recent_trend, initial_value, final_value,
                    best_value, best_step, peak_value, peak_step, peak_reversal_pct,
                    improvement_percent, converged, convergence_step,
                    anomaly_count, anomaly_details, summary, sampled_points, total_points,
                    smoothed_points, smoothed_final_value, ema_alpha)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    run_pk,
                    metric_name,
                    analysis_data.get('trend'),
                    analysis_data.get('recent_trend'),
                    analysis_data.get('initial_value'),
                    analysis_data.get('final_value'),
                    analysis_data.get('best_value'),
                    analysis_data.get('best_step'),
                    analysis_data.get('peak_value'),
                    analysis_data.get('peak_step'),
                    analysis_data.get('peak_reversal_pct', 0.0),
                    analysis_data.get('improvement_percent'),
                    analysis_data.get('converged', False),
                    analysis_data.get('convergence_step'),
                    analysis_data.get('anomaly_count', 0),
                    json.dumps(analysis_data.get('anomalies', [])),
                    analysis_data.get('summary'),
                    json.dumps(analysis_data.get('sampled_points', [])),
                    analysis_data.get('total_points'),
                    json.dumps(analysis_data.get('smoothed_points', [])),
                    analysis_data.get('smoothed_final_value'),
                    analysis_data.get('ema_alpha'),
                )
            )
            conn.commit()

    def get_metric_analyses(self, run_pk: int, metric_names: list | None = None) -> dict:
        """Get cached analyses for a run."""
        import json

        with self._get_connection() as conn:
            if metric_names:
                placeholders = ','.join('?' * len(metric_names))
                query = f'''SELECT * FROM metric_analyses
                           WHERE run_pk = ? AND metric_name IN ({placeholders})'''
                params = [run_pk] + metric_names
                rows = conn.execute(query, params).fetchall()
            else:
                rows = conn.execute(
                    'SELECT * FROM metric_analyses WHERE run_pk = ?',
                    (run_pk,)
                ).fetchall()

            result = {}
            for row in rows:
                result[row['metric_name']] = {
                    'trend': row['trend'],
                    'recent_trend': row['recent_trend'],
                    'initial_value': row['initial_value'],
                    'final_value': row['final_value'],
                    'best_value': row['best_value'],
                    'best_step': row['best_step'],
                    'peak_value': row['peak_value'],
                    'peak_step': row['peak_step'],
                    'peak_reversal_pct': row['peak_reversal_pct'] if row['peak_reversal_pct'] is not None else 0.0,
                    'improvement_percent': row['improvement_percent'],
                    'converged': bool(row['converged']),
                    'convergence_step': row['convergence_step'],
                    'anomaly_count': row['anomaly_count'],
                    'anomalies': json.loads(row['anomaly_details']) if row['anomaly_details'] else [],
                    'summary': row['summary'],
                    'sampled_points': json.loads(row['sampled_points']) if row['sampled_points'] else [],
                    'total_points': row['total_points'],
                    'smoothed_points': json.loads(row['smoothed_points']) if 'smoothed_points' in row.keys() and row['smoothed_points'] else [],
                    'smoothed_final_value': row['smoothed_final_value'] if 'smoothed_final_value' in row.keys() and row['smoothed_final_value'] is not None else None,
                    'ema_alpha': row['ema_alpha'] if 'ema_alpha' in row.keys() and row['ema_alpha'] is not None else None,
                }

            return result

    def delete_metric_analyses(self, run_pk: int):
        """Delete all analyses for a run."""
        with self._get_connection() as conn:
            conn.execute('DELETE FROM metric_analyses WHERE run_pk = ?', (run_pk,))
            conn.commit()

    # --- Run queue -------------------------------------------------------------

    @staticmethod
    def _queue_row(row) -> dict:
        entry = dict(row)
        entry["env_vars"] = json.loads(entry["env_vars"]) if entry.get("env_vars") else {}
        return entry

    def enqueue_run(self, project_name: str, branch: str | None = None,
                    env_vars: dict | None = None, source: str = "manual") -> int:
        with self._get_connection() as conn:
            cursor = conn.execute(
                '''INSERT INTO run_queue (project_name, branch, env_vars, source, status, created_at)
                   VALUES (?, ?, ?, ?, 'queued', ?)''',
                (project_name, branch, json.dumps(env_vars) if env_vars else None, source,
                 datetime.now().isoformat()))
            conn.commit()
            return cursor.lastrowid

    def get_queue_entry(self, queue_id: int) -> dict | None:
        with self._get_connection() as conn:
            row = conn.execute('SELECT * FROM run_queue WHERE id = ?', (queue_id,)).fetchone()
            return self._queue_row(row) if row else None

    def list_queue(self, project_name: str) -> list[dict]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM run_queue WHERE project_name = ? AND status = 'queued' ORDER BY id",
                (project_name,)).fetchall()
            return [self._queue_row(r) for r in rows]

    def projects_with_queued_runs(self) -> list[str]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT project_name FROM run_queue WHERE status = 'queued'").fetchall()
            return [r["project_name"] for r in rows]

    def _update_queue(self, queue_id: int, where_queued: bool = False, **fields) -> bool:
        # Column names come only from the call sites in this file, never from user input.
        cols = ", ".join(f"{k} = ?" for k in fields)
        sql = f"UPDATE run_queue SET {cols} WHERE id = ?" + (" AND status = 'queued'" if where_queued else "")  # NOSONAR
        with self._get_connection() as conn:
            cursor = conn.execute(sql, (*fields.values(), queue_id))
            conn.commit()
            return cursor.rowcount == 1

    def mark_queue_started(self, queue_id: int, run_pk: int) -> bool:
        """Claim a queued entry as started. False if it left 'queued' (e.g. cancelled) meanwhile."""
        return self._update_queue(queue_id, where_queued=True, status="started", run_pk=run_pk,
                                  wait_reason=None, started_at=datetime.now().isoformat())

    def set_queue_wait_reason(self, queue_id: int, reason: str | None):
        self._update_queue(queue_id, where_queued=True, wait_reason=reason)

    def mark_queue_failed(self, queue_id: int, error: str):
        self._update_queue(queue_id, where_queued=True, status="failed", error=error)

    def cancel_queue_entry(self, queue_id: int, error: str | None = None) -> bool:
        return self._update_queue(queue_id, where_queued=True, status="cancelled", error=error)

    def rename_queue_project(self, old: str, new: str):
        with self._get_connection() as conn:
            conn.execute('UPDATE run_queue SET project_name = ? WHERE project_name = ?', (new, old))
            conn.commit()

    def cancel_project_queue(self, project_name: str, error: str):
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE run_queue SET status = 'cancelled', error = ? "
                "WHERE project_name = ? AND status = 'queued'", (error, project_name))
            conn.commit()

    def purge_old_queue_entries(self, days: int = 30) -> int:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM run_queue WHERE status != 'queued' AND created_at < ?", (cutoff,))
            conn.commit()
            return cursor.rowcount

    # --- Pipelines ----------------------------------------------------------------

    _PIPELINE_RUN_FIELDS = {"status", "current_step", "error", "ended_at"}
    _STEP_RUN_FIELDS = {"status", "queue_id", "run_pk", "exit_code", "pid", "log_path",
                        "run_dir", "error", "started_at", "ended_at"}

    @staticmethod
    def _pipeline_row(row) -> dict | None:
        if row is None:
            return None
        run = dict(row)
        run["definition"] = json.loads(run["definition"])
        return run

    def create_pipeline_run(self, pipeline_name: str, definition: dict) -> tuple[int, int] | None:
        """Start a pipeline run with step 0 pending. None if one is already running."""
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30)
        try:
            conn.execute('BEGIN IMMEDIATE')
            active = conn.execute(
                "SELECT 1 FROM pipeline_runs WHERE pipeline_name = ? AND status = 'running'",
                (pipeline_name,)).fetchone()
            if active:
                conn.execute('ROLLBACK')
                return None
            number = conn.execute(
                '''INSERT INTO pipeline_run_counters (pipeline_name, last_number) VALUES (?, 1)
                   ON CONFLICT(pipeline_name) DO UPDATE SET last_number = last_number + 1
                   RETURNING last_number''', (pipeline_name,)).fetchone()[0]
            cursor = conn.execute(
                '''INSERT INTO pipeline_runs (pipeline_name, run_number, status, definition, current_step, started_at)
                   VALUES (?, ?, 'running', ?, 0, ?)''',
                (pipeline_name, number, json.dumps(definition), datetime.now().isoformat()))
            pk = cursor.lastrowid
            first = definition["steps"][0]
            conn.execute(
                '''INSERT INTO pipeline_step_runs (pipeline_run_pk, step_index, step_name, step_type, attempt, status)
                   VALUES (?, 0, ?, ?, 1, 'pending')''', (pk, first["name"], first["type"]))
            conn.execute('COMMIT')
            return pk, number
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def get_pipeline_run(self, pipeline_name: str, run_number: int) -> dict | None:
        with self._get_connection() as conn:
            return self._pipeline_row(conn.execute(
                'SELECT * FROM pipeline_runs WHERE pipeline_name = ? AND run_number = ?',
                (pipeline_name, run_number)).fetchone())

    def get_pipeline_run_by_pk(self, pk: int) -> dict | None:
        with self._get_connection() as conn:
            return self._pipeline_row(conn.execute(
                'SELECT * FROM pipeline_runs WHERE id = ?', (pk,)).fetchone())

    def latest_pipeline_run(self, pipeline_name: str) -> dict | None:
        with self._get_connection() as conn:
            return self._pipeline_row(conn.execute(
                'SELECT * FROM pipeline_runs WHERE pipeline_name = ? ORDER BY run_number DESC LIMIT 1',
                (pipeline_name,)).fetchone())

    def list_pipeline_runs(self, pipeline_name: str, limit: int = 20) -> list[dict]:
        with self._get_connection() as conn:
            rows = conn.execute(
                'SELECT * FROM pipeline_runs WHERE pipeline_name = ? ORDER BY run_number DESC LIMIT ?',
                (pipeline_name, limit)).fetchall()
            return [self._pipeline_row(r) for r in rows]

    def running_pipeline_runs(self) -> list[dict]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM pipeline_runs WHERE status = 'running' ORDER BY id").fetchall()
            return [self._pipeline_row(r) for r in rows]

    def update_pipeline_run(self, pk: int, **fields):
        bad = set(fields) - self._PIPELINE_RUN_FIELDS
        if bad:
            raise ValueError(f"not updatable: {sorted(bad)}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._get_connection() as conn:
            conn.execute(f'UPDATE pipeline_runs SET {cols} WHERE id = ?', (*fields.values(), pk))  # NOSONAR
            conn.commit()

    def create_step_run(self, pipeline_run_pk: int, step_index: int, step_name: str,
                        step_type: str, status: str = "pending") -> int:
        with self._get_connection() as conn:
            attempt = conn.execute(
                'SELECT COALESCE(MAX(attempt), 0) + 1 FROM pipeline_step_runs '
                'WHERE pipeline_run_pk = ? AND step_index = ?',
                (pipeline_run_pk, step_index)).fetchone()[0]
            cursor = conn.execute(
                '''INSERT INTO pipeline_step_runs (pipeline_run_pk, step_index, step_name, step_type, attempt, status)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (pipeline_run_pk, step_index, step_name, step_type, attempt, status))
            conn.commit()
            return cursor.lastrowid

    def enqueue_step_run(self, step_pk: int, project_name: str, branch: str | None,
                         env_vars: dict | None, source: str) -> int | None:
        """Enqueue a pipeline train step's run and mark the step queued in one transaction.

        Returns the queue id, or None (nothing written) if the step is no longer pending.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                '''INSERT INTO run_queue (project_name, branch, env_vars, source, status, created_at)
                   VALUES (?, ?, ?, ?, 'queued', ?)''',
                (project_name, branch, json.dumps(env_vars) if env_vars else None, source,
                 datetime.now().isoformat()))
            queue_id = cursor.lastrowid
            updated = conn.execute(
                "UPDATE pipeline_step_runs SET status = 'queued', queue_id = ? "
                "WHERE id = ? AND status = 'pending'", (queue_id, step_pk)).rowcount
            if updated != 1:
                conn.rollback()
                return None
            conn.commit()
            return queue_id

    def current_step_run(self, pipeline_run_pk: int, step_index: int) -> dict | None:
        with self._get_connection() as conn:
            row = conn.execute(
                'SELECT * FROM pipeline_step_runs WHERE pipeline_run_pk = ? AND step_index = ? '
                'ORDER BY attempt DESC LIMIT 1', (pipeline_run_pk, step_index)).fetchone()
            return dict(row) if row else None

    def list_step_runs(self, pipeline_run_pk: int) -> list[dict]:
        with self._get_connection() as conn:
            rows = conn.execute(
                '''SELECT s.* FROM pipeline_step_runs s
                   WHERE s.pipeline_run_pk = ? AND s.attempt = (
                       SELECT MAX(t.attempt) FROM pipeline_step_runs t
                       WHERE t.pipeline_run_pk = s.pipeline_run_pk AND t.step_index = s.step_index)
                   ORDER BY s.step_index''', (pipeline_run_pk,)).fetchall()
            return [dict(r) for r in rows]

    def update_step_run(self, step_pk: int, **fields):
        bad = set(fields) - self._STEP_RUN_FIELDS
        if bad:
            raise ValueError(f"not updatable: {sorted(bad)}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._get_connection() as conn:
            conn.execute(f'UPDATE pipeline_step_runs SET {cols} WHERE id = ?', (*fields.values(), step_pk))  # NOSONAR
            conn.commit()

    def delete_skipped_steps(self, pipeline_run_pk: int):
        with self._get_connection() as conn:
            conn.execute("DELETE FROM pipeline_step_runs WHERE pipeline_run_pk = ? AND status = 'skipped'",
                         (pipeline_run_pk,))
            conn.commit()

    def delete_pipeline_records(self, pipeline_name: str):
        with self._get_connection() as conn:
            conn.execute('''DELETE FROM pipeline_step_runs WHERE pipeline_run_pk IN
                            (SELECT id FROM pipeline_runs WHERE pipeline_name = ?)''', (pipeline_name,))
            conn.execute('DELETE FROM pipeline_runs WHERE pipeline_name = ?', (pipeline_name,))
            conn.execute('DELETE FROM pipeline_run_counters WHERE pipeline_name = ?', (pipeline_name,))
            conn.commit()


# Global database instance (initialized in app.py)
_db: DatabaseService | None = None


def init_database(db_path: str):
    """Initialize the global database service."""
    global _db
    _db = DatabaseService(db_path)


def get_db() -> DatabaseService:
    """Get the global database service."""
    if _db is None:
        raise RuntimeError("Database service not initialized")
    return _db
