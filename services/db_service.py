import os
import sqlite3
from datetime import datetime
from typing import Optional
from contextlib import contextmanager

from models.user import User


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
                        run_id INTEGER NOT NULL,
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
                        FOREIGN KEY (run_id) REFERENCES training_runs(id) ON DELETE CASCADE,
                        UNIQUE(run_id, metric_name)
                    )
                ''')
                conn.execute(
                    'CREATE INDEX IF NOT EXISTS idx_metric_analyses_run ON metric_analyses(run_id)'
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

            # Add annotation columns to training_runs if missing
            cursor = conn.execute("PRAGMA table_info(training_runs)")
            tr_columns = {row['name'] for row in cursor.fetchall()}
            for col, typedef in [
                ('notes',   "TEXT NOT NULL DEFAULT ''"),
                ('notable', 'INTEGER NOT NULL DEFAULT 0'),
                ('tags',    "TEXT NOT NULL DEFAULT ''"),
                ('persistent_dir', 'TEXT'),
            ]:
                if col not in tr_columns:
                    try:
                        conn.execute(f'ALTER TABLE training_runs ADD COLUMN {col} {typedef}')
                    except Exception:
                        pass

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
    def create_training_run(self, project_name: str, metadata: dict) -> int:
        """Create new run record. Returns run_id."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                '''INSERT INTO training_runs
                   (project_name, started_at, status, commit_sha, commit_message,
                    branch, python_version, gpu_info, hostname)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    project_name,
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
            conn.commit()
            return cursor.lastrowid

    def update_training_run(self, run_id: int, **fields):
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

        values.append(run_id)
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

    def get_training_run(self, run_id: int) -> dict:
        """Fetch single run details."""
        with self._get_connection() as conn:
            row = conn.execute('SELECT * FROM training_runs WHERE id = ?', (run_id,)).fetchone()
            return dict(row) if row else None

    def delete_training_run(self, run_id: int):
        """Delete run record."""
        with self._get_connection() as conn:
            conn.execute('DELETE FROM training_runs WHERE id = ?', (run_id,))
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

    def update_run_annotations(self, run_id: int, **fields):
        """Update notes, notable, and/or tags on a run record."""
        allowed = {'notes': str, 'notable': int, 'tags': str}
        updates, values = [], []
        for field, value in fields.items():
            if field in allowed:
                updates.append(f'{field} = ?')
                values.append(allowed[field](value))
        if not updates:
            return
        values.append(run_id)
        with self._get_connection() as conn:
            conn.execute(f"UPDATE training_runs SET {', '.join(updates)} WHERE id = ?", values)
            conn.commit()

    def prune_old_runs(self, project_name: str, keep_last: int = 20) -> list[dict]:
        """Delete non-notable runs beyond retention limit. Notable runs are never pruned.
        Returns list of deleted runs."""
        with self._get_connection() as conn:
            # Only count/delete non-notable runs; notable runs are exempt
            rows = conn.execute(
                '''SELECT * FROM training_runs
                   WHERE project_name = ? AND notable = 0
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
            conn.commit()
            return cursor.rowcount

    # Metric analysis operations
    def save_metric_analysis(self, run_id: int, metric_name: str, analysis_data: dict):
        """Insert or replace metric analysis."""
        import json

        with self._get_connection() as conn:
            conn.execute(
                '''INSERT OR REPLACE INTO metric_analyses
                   (run_id, metric_name, trend, recent_trend, initial_value, final_value,
                    best_value, best_step, peak_value, peak_step, peak_reversal_pct,
                    improvement_percent, converged, convergence_step,
                    anomaly_count, anomaly_details, summary, sampled_points, total_points,
                    smoothed_points, smoothed_final_value, ema_alpha)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    run_id,
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

    def get_metric_analyses(self, run_id: int, metric_names: list | None = None) -> dict:
        """Get cached analyses for a run."""
        import json

        with self._get_connection() as conn:
            if metric_names:
                placeholders = ','.join('?' * len(metric_names))
                query = f'''SELECT * FROM metric_analyses
                           WHERE run_id = ? AND metric_name IN ({placeholders})'''
                params = [run_id] + metric_names
                rows = conn.execute(query, params).fetchall()
            else:
                rows = conn.execute(
                    'SELECT * FROM metric_analyses WHERE run_id = ?',
                    (run_id,)
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

    def delete_metric_analyses(self, run_id: int):
        """Delete all analyses for a run."""
        with self._get_connection() as conn:
            conn.execute('DELETE FROM metric_analyses WHERE run_id = ?', (run_id,))
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
