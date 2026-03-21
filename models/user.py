from dataclasses import dataclass
from datetime import datetime


@dataclass
class User:
    id: int
    email: str
    name: str
    is_admin: bool
    created_at: datetime
    last_login_at: datetime | None = None
    failed_login_attempts: int = 0
    locked_until: datetime | None = None

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "is_admin": self.is_admin,
            "created_at": self.created_at.isoformat(),
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
            "failed_login_attempts": self.failed_login_attempts,
            "locked_until": self.locked_until.isoformat() if self.locked_until else None,
        }

    def is_locked(self) -> bool:
        """Check if account is currently locked."""
        if self.locked_until is None:
            return False
        return datetime.now() < self.locked_until
