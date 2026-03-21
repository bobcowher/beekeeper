# Beekeeper Authentication System

## Overview

Beekeeper now includes an optional authentication system with:
- Email/password authentication
- Admin panel for user and settings management
- API key system for programmatic access
- Session-based web authentication
- Backward compatibility (disabled by default)

## Quick Start

### First-Time Setup

1. **Default state**: Authentication is disabled by default. Existing installations continue to work without login.

2. **Enable authentication**:
   - Access the admin panel by clicking "Admin" in the sidebar (accessible to everyone when auth is disabled)
   - Toggle "Enable Authentication" in the settings
   - You'll be redirected to create the first user account
   - The first user automatically becomes an administrator

3. **Login**: After creating your account, you'll be logged in automatically.

### Admin Panel

Access the admin panel at `/admin` (requires admin privileges).

**Features**:
- Toggle authentication on/off
- Configure session lifetime
- Set password requirements
- Create and manage users
- Generate and manage API keys

## Authentication Settings

Edit settings via the Admin panel (stored in `config.properties`):

```properties
auth.enabled=false                 # Enable/disable authentication
session.lifetime_days=7            # How long users stay logged in
password.min_length=8              # Minimum password length
api.rate_limit_per_minute=10       # API rate limit per IP address
```

## CLI Admin Tool

Beekeeper includes a command-line administration tool for server-side management:

```bash
./admin.sh --help
```

The `admin.sh` wrapper automatically activates the virtual environment.

### Available Commands

- `list-users` - List all users with admin status
- `create-user` - Create a new user (interactive or with flags)
- `reset-password` - Reset a user's password
- `promote` - Promote user to administrator
- `demote` - Demote admin to regular user
- `unlock-account` - Unlock account locked due to failed login attempts
- `delete-user` - Delete a user account
- `list-api-keys` - List API keys for a user
- `revoke-api-key` - Revoke an API key by name
- `enable-auth` / `disable-auth` - Toggle authentication
- `clean-sessions` - Remove expired sessions
- `config` - Show current configuration

### Examples

```bash
# Reset password for locked-out admin
./admin.sh reset-password admin@example.com

# Create a new admin user
./admin.sh create-user --email new@example.com --name "New Admin" --admin

# List all users
./admin.sh list-users

# View configuration
./admin.sh config
```

The CLI tool is useful for password resets, initial setup, automation, and maintenance tasks.

## User Management

### Creating Users

Admins can create new users from the Admin panel:
1. Go to Admin → Users
2. Click "Create New User"
3. Enter name, email, password
4. Optionally grant admin privileges

### User Roles

- **Admin**: Full access including user management and settings
- **User**: Access to projects and features (when implemented)

### Deleting Users

- Admins can delete users from the Admin panel
- Cannot delete yourself
- Cannot delete the last admin

### Promoting/Demoting Admins

- Admins can promote users to admin or demote admins to users
- Cannot demote the last admin (system protection)

## API Authentication

When authentication is enabled, API endpoints require an API key.

### Generating API Keys

1. Go to Admin panel
2. Enter a name for your key (e.g., "CI/CD Pipeline")
3. Click "Generate API Key"
4. **Copy the key immediately** - it won't be shown again
5. Keys have format: `bk_<random_string>`

### Using API Keys

Include the API key in the `Authorization` header:

```bash
curl -H "Authorization: Bearer bk_your_api_key_here" \
     http://localhost:5000/api/v1/projects
```

**Example requests**:

```bash
# List all projects
curl -H "Authorization: Bearer bk_abc123..." \
     http://localhost:5000/api/v1/projects

# Start training
curl -X POST \
     -H "Authorization: Bearer bk_abc123..." \
     http://localhost:5000/api/v1/projects/my-project/training/start

# Stream logs
curl -H "Authorization: Bearer bk_abc123..." \
     http://localhost:5000/api/v1/projects/my-project/logs/stream
```

### Managing API Keys

- View all your API keys in the Admin panel
- Keys show creation date and last usage time
- Revoke keys when no longer needed
- Each user can have multiple keys with descriptive names

### API Key Security

- Keys are hashed in the database (bcrypt)
- Only the prefix is shown in the UI for identification
- Last usage timestamp helps identify unused keys
- Keys can be revoked instantly from the Admin panel
- IP-based rate limiting prevents brute force attacks (configurable)
- All authentication attempts are logged with IP addresses

## Session Management

### Web Sessions

- Sessions use secure HTTPOnly cookies
- Default lifetime: 7 days (configurable)
- Sessions are stored server-side (not client-side JWT)
- Expired sessions are cleaned up automatically

### Logging Out

Click "Logout" in the sidebar footer (when logged in).

## Security Features

### Password Security
- Passwords hashed with bcrypt
- Configurable minimum length (default: 8 characters)
- Never stored in plain text
- Account lockout after 5 failed login attempts (15 minute lockout)
- Progressive warnings show remaining attempts before lockout

### Session Security
- HTTPOnly cookies (not accessible via JavaScript)
- Random session IDs (32-byte URL-safe tokens)
- Server-side session storage
- Automatic cleanup of expired sessions

### API Key Security
- Keys hashed with bcrypt in database
- High entropy (144 bits)
- Format: `bk_<24_random_chars>`
- Last-used timestamp for auditing

## Disabling Authentication

To disable authentication (return to open access):

1. Login as admin
2. Go to Admin panel
3. Uncheck "Enable Authentication"
4. Save settings

**Effect**:
- Web UI becomes accessible without login
- API endpoints work without keys
- All sessions and keys remain in database (reactivate anytime)

## Backward Compatibility

- Authentication is **disabled by default**
- Existing installations continue to work unchanged
- Enable authentication when ready
- Can be toggled on/off without data loss

## Database

User data is stored in `data/beekeeper.db` (SQLite):
- **users**: User accounts and passwords
- **sessions**: Active web sessions
- **api_keys**: Generated API keys

**Note**: The database file is excluded from git (in `.gitignore`).

## Troubleshooting

### Locked Out?

**Forgot password:**
```bash
cd /path/to/beekeeper
./admin.sh reset-password your-email@example.com
```

**Account locked after failed login attempts:**
```bash
./admin.sh unlock-account your-email@example.com
```

Accounts are automatically locked for 15 minutes after 5 failed login attempts. Use the unlock command to immediately remove the lock and reset the failed attempt counter.

For more commands, run `./admin.sh --help`.

### API Returns 401

Check that:
- Authentication is enabled in settings
- API key is valid and not revoked
- Authorization header format is correct: `Bearer bk_...`
- Key hasn't been deleted

### API Returns 429 (Rate Limit)

Your IP exceeded the rate limit (default: 10 requests/minute). This is an anti-brute-force protection. Wait a minute or adjust `api.rate_limit_per_minute` in `config.properties`.

### Can't Access Admin Panel

- Check that you're logged in
- Verify your account has admin privileges
- If auth is disabled, anyone can access admin panel

## Future Enhancements (Not Yet Implemented)

Planned for Phase 2:
- Google OAuth integration (for cloud deployments)
- User permissions and roles (per-project access)
- Audit logging
- Password reset via email
- Two-factor authentication (2FA)

## Files

### New Files (Authentication System)
```
data/
├── beekeeper.db          # User database (SQLite)
└── schema.sql            # Database schema
config.properties         # Application settings
models/user.py            # User data model
routes/
├── auth.py              # Login/logout routes
└── admin.py             # Admin panel routes
services/
├── auth_service.py      # Password hashing, decorators
├── db_service.py        # Database operations
└── config_service.py    # Settings management
middleware/
└── auth_middleware.py   # Request authentication check
templates/
├── login.html           # Login page
├── register.html        # First user setup
└── admin.html           # Admin panel
```

### Modified Files
- `app.py` - Initialize services, register routes
- `routes/api_v1.py` - Add API key authentication
- `templates/base.html` - Add admin link, user menu
- `requirements.txt` - Add bcrypt
- `.gitignore` - Exclude config.properties, beekeeper.db

## Support

For issues or questions:
- Check the main README
- Review this AUTH_README
- Check application logs
