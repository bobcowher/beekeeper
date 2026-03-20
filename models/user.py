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

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "is_admin": self.is_admin,
            "created_at": self.created_at.isoformat(),
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }
