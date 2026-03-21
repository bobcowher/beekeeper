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
