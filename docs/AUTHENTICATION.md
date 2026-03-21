# Beekeeper Authentication Guide

## Overview

Beekeeper now includes optional authentication to secure your ML training environment. The system is designed to be:

- **Optional**: Disabled by default, enable when you need it
- **Simple**: Easy first-time setup with admin account creation
- **Flexible**: Toggle on/off without losing data
- **Secure**: Bcrypt password hashing, session management, API keys

## Why Use Authentication?

Enable authentication when:
- Exposing Beekeeper beyond your local network
- Multiple users need access
- You need audit trails and access control
- Securing sensitive training data or code
- Using the API from external tools/scripts

Keep it disabled for:
- Single-user home lab setups on isolated networks
- Quick testing and development
- Maximum simplicity

## Getting Started

### Enable Authentication

1. **Access the Admin Panel**
   - Click "Admin" in the sidebar
   - When auth is disabled, everyone can access this

2. **Toggle Authentication On**
   - Check "Enable Authentication"
   - Click "Save Settings"

3. **Create First User**
   - You'll be redirected to account creation
   - Enter your name, email, and password
   - This first account becomes the administrator

4. **You're Done!**
   - You're automatically logged in
   - Authentication is now active

### Disable Authentication

Changed your mind? No problem:

1. Login as an admin
2. Go to Admin panel
3. Uncheck "Enable Authentication"
4. Save settings

All user accounts and API keys remain in the database - just re-enable to reactivate them.

## CLI Admin Tool

For server-side administration, Beekeeper includes a command-line tool that doesn't require web access:

```bash
./admin.sh --help
```

The `admin.sh` wrapper automatically activates the virtual environment and runs the admin tool.

### Common Commands

**User Management:**
```bash
# List all users
./admin.sh list-users

# Create a new admin user
./admin.sh create-user --email admin@example.com --name "Admin User" --admin

# Reset a user's password
./admin.sh reset-password user@example.com

# Promote user to admin
./admin.sh promote user@example.com

# Unlock a locked account
./admin.sh unlock-account user@example.com

# Delete a user
./admin.sh delete-user user@example.com
```

**API Key Management:**
```bash
# List user's API keys
./admin.sh list-api-keys user@example.com

# Revoke an API key
./admin.sh revoke-api-key user@example.com "Key Name"
```

**System Settings:**
```bash
# Show current configuration
./admin.sh config

# Enable/disable authentication
./admin.sh enable-auth
./admin.sh disable-auth

# Clean up expired sessions
./admin.sh clean-sessions
```

The CLI tool is particularly useful for:
- Password resets when locked out
- Initial setup and automation
- Batch user creation
- Server maintenance tasks

## Admin Panel Features

### User Management

**Create Users**
- Add new users with email/password
- Grant or withhold admin privileges
- Set during creation or promote later

**Manage Existing Users**
- Promote users to admin
- Demote admins to regular users
- Delete users (cannot delete yourself or last admin)
- View last login timestamps

### Settings

**Authentication Settings**
- Enable/disable authentication system
- Configure session lifetime (default: 7 days)
- Set minimum password length (default: 8 characters)

### API Key Management

**Generate Keys**
- Create named API keys for programmatic access
- Keys shown only once - save immediately!
- Format: `bk_<random_string>`

**Manage Keys**
- View all your API keys
- See creation date and last usage
- Revoke keys when no longer needed

## Using the API

When authentication is enabled, all API endpoints require an API key.

### Generate an API Key

1. Go to Admin panel
2. Enter a descriptive name (e.g., "Training Script", "CI/CD Pipeline")
3. Click "Generate API Key"
4. **Copy the key immediately** - you won't see it again

### Making API Requests

Include the API key in the `Authorization` header:

```bash
curl -H "Authorization: Bearer bk_your_key_here" \
     http://your-server:5000/api/v1/projects
```

### Example API Usage

**List projects:**
```bash
curl -H "Authorization: Bearer bk_abc123..." \
     http://localhost:5000/api/v1/projects
```

**Start training:**
```bash
curl -X POST \
     -H "Authorization: Bearer bk_abc123..." \
     http://localhost:5000/api/v1/projects/my-project/training/start
```

**Stream logs:**
```bash
curl -H "Authorization: Bearer bk_abc123..." \
     http://localhost:5000/api/v1/projects/my-project/logs/stream
```

**Get system stats:**
```bash
curl -H "Authorization: Bearer bk_abc123..." \
     http://localhost:5000/api/v1/stats
```

### Python Example

```python
import requests

API_KEY = "bk_your_key_here"
BASE_URL = "http://localhost:5000/api/v1"

headers = {"Authorization": f"Bearer {API_KEY}"}

# List projects
response = requests.get(f"{BASE_URL}/projects", headers=headers)
projects = response.json()

# Start training
response = requests.post(
    f"{BASE_URL}/projects/my-project/training/start",
    headers=headers
)
result = response.json()
```

## Security

### Password Security
- Passwords hashed with bcrypt (industry standard)
- Configurable minimum length
- Never stored in plain text
- Account lockout after failed login attempts (5 failures = 15 minute lockout)
- Progressive warnings show remaining attempts

### Session Security
- HTTPOnly cookies (not accessible via JavaScript)
- Random 32-byte session IDs
- Configurable expiration (default: 7 days)
- Server-side storage
- Automatic cleanup of expired sessions

### API Key Security
- Keys hashed with bcrypt in database
- High entropy (144 bits of randomness)
- Only shown once during creation
- Last-used timestamp for auditing
- Instant revocation capability

## Architecture

### Database
User data stored in `data/beekeeper.db` (SQLite):
- **users** - Accounts, passwords, admin status
- **sessions** - Active login sessions
- **api_keys** - Generated API keys

### Configuration
Settings stored in `config.properties`:
```properties
auth.enabled=false              # Toggle authentication
session.lifetime_days=7         # Session expiration
password.min_length=8          # Password requirements
```

### Backward Compatibility
- Authentication disabled by default
- Existing installations unchanged
- No migration required
- Toggle on/off without data loss

## Troubleshooting

### Locked Out?

**Forgot password:**
```bash
cd /path/to/beekeeper
./admin.sh reset-password your-email@example.com
```

**Account locked (too many failed attempts):**
```bash
./admin.sh unlock-account your-email@example.com
```

Accounts are automatically locked for 15 minutes after 5 failed login attempts. The unlock command immediately resets the failed attempt counter and removes the lock.

### API Returns 401 Unauthorized

Check that:
- Authentication is enabled
- API key is valid and not revoked
- Authorization header format: `Authorization: Bearer bk_...`
- Key matches exactly (no extra spaces)

### Can't Access Admin Panel

- Verify you're logged in (check sidebar for your name)
- Confirm your account has admin privileges
- If auth is disabled, anyone can access admin

### Session Expired

Sessions expire after the configured lifetime (default: 7 days). Simply login again.

## Best Practices

### For Home Labs
- Keep auth disabled if server is isolated on local network
- Enable when accessing from outside your home network
- Use strong passwords even in private environments

### For Production
- Always enable authentication
- Use reverse proxy (nginx, Caddy) for HTTPS
- Set reasonable session lifetimes
- Regularly audit and revoke unused API keys
- Monitor last login times for unusual activity

### For API Access
- Create separate API keys for different purposes
- Use descriptive names ("Jenkins CI", "Python Script", etc.)
- Revoke keys immediately when no longer needed
- Rotate keys periodically
- Never commit API keys to version control

## Future Enhancements

Planned for future releases:
- Google OAuth integration (for cloud deployments)
- Per-project permissions
- Audit logging
- Password reset via email
- Two-factor authentication (2FA)
- LDAP/Active Directory integration

## Support

- **Documentation**: See [AUTH_README.md](../AUTH_README.md) for detailed technical docs
- **API Docs**: See [API_IMPLEMENTATION.md](../API_IMPLEMENTATION.md) for complete API reference
- **Website**: https://www.teaandrobots.com/software/beekeeper/
- **GitHub**: https://github.com/bobcowher/beekeeper
