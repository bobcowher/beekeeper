#!/usr/bin/env python3
"""
Beekeeper CLI Admin Tool

Command-line utility for managing Beekeeper users, authentication, and system settings.
Run this script on the server where Beekeeper is installed.
"""

import argparse
import getpass
import os
import sys
from datetime import datetime

# Add project root to path
BEEKEEPER_HOME = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BEEKEEPER_HOME)

from services.db_service import DatabaseService
from services.config_service import ConfigService
from services.auth_service import hash_password


def init_services():
    """Initialize database and config services."""
    db_path = os.path.join(BEEKEEPER_HOME, 'data', 'beekeeper.db')
    config_path = os.path.join(BEEKEEPER_HOME, 'config.properties')

    db = DatabaseService(db_path)
    config = ConfigService(config_path)

    return db, config


def list_users(args):
    """List all users."""
    db, _ = init_services()
    users = db.list_all_users()

    if not users:
        print("No users found.")
        return

    print(f"\n{'ID':<5} {'Email':<30} {'Name':<20} {'Admin':<8} {'Locked':<8} {'Last Login':<20}")
    print("-" * 100)

    for user in users:
        last_login = user.last_login_at.strftime('%Y-%m-%d %H:%M') if user.last_login_at else 'Never'
        admin_status = 'Yes' if user.is_admin else 'No'
        locked_status = 'Yes' if user.is_locked() else 'No'
        print(f"{user.id:<5} {user.email:<30} {user.name:<20} {admin_status:<8} {locked_status:<8} {last_login:<20}")

    print(f"\nTotal: {len(users)} user(s)")


def create_user(args):
    """Create a new user."""
    db, config = init_services()

    # Get user details
    email = args.email or input("Email: ").strip()
    name = args.name or input("Name: ").strip()

    if not email or not name:
        print("Error: Email and name are required", file=sys.stderr)
        sys.exit(1)

    # Check if user exists
    if db.get_user_by_email(email):
        print(f"Error: User with email '{email}' already exists", file=sys.stderr)
        sys.exit(1)

    # Get password
    if args.password:
        password = args.password
    else:
        password = getpass.getpass("Password: ")
        password_confirm = getpass.getpass("Confirm password: ")
        if password != password_confirm:
            print("Error: Passwords do not match", file=sys.stderr)
            sys.exit(1)

    # Validate password length
    min_length = config.get_int('password.min_length', 8)
    if len(password) < min_length:
        print(f"Error: Password must be at least {min_length} characters", file=sys.stderr)
        sys.exit(1)

    # Create user
    password_hash = hash_password(password)
    user = db.create_user(email, name, password_hash, is_admin=args.admin)

    role = "administrator" if args.admin else "user"
    print(f"✓ User created successfully (ID: {user.id}, Role: {role})")


def reset_password(args):
    """Reset a user's password."""
    db, config = init_services()

    # Find user
    user = db.get_user_by_email(args.email)
    if not user:
        print(f"Error: User with email '{args.email}' not found", file=sys.stderr)
        sys.exit(1)

    # Get new password
    if args.password:
        password = args.password
    else:
        password = getpass.getpass("New password: ")
        password_confirm = getpass.getpass("Confirm password: ")
        if password != password_confirm:
            print("Error: Passwords do not match", file=sys.stderr)
            sys.exit(1)

    # Validate password length
    min_length = config.get_int('password.min_length', 8)
    if len(password) < min_length:
        print(f"Error: Password must be at least {min_length} characters", file=sys.stderr)
        sys.exit(1)

    # Update password
    password_hash = hash_password(password)
    db.update_user(user.id, password_hash=password_hash)

    print(f"✓ Password reset successfully for {user.name} ({user.email})")


def promote_user(args):
    """Promote user to admin."""
    db, _ = init_services()

    user = db.get_user_by_email(args.email)
    if not user:
        print(f"Error: User with email '{args.email}' not found", file=sys.stderr)
        sys.exit(1)

    if user.is_admin:
        print(f"User {user.name} is already an admin")
        return

    db.update_user(user.id, is_admin=True)
    print(f"✓ {user.name} promoted to administrator")


def demote_user(args):
    """Demote admin to regular user."""
    db, _ = init_services()

    user = db.get_user_by_email(args.email)
    if not user:
        print(f"Error: User with email '{args.email}' not found", file=sys.stderr)
        sys.exit(1)

    if not user.is_admin:
        print(f"User {user.name} is not an admin")
        return

    # Check if this is the last admin
    if db.count_admins() <= 1:
        print("Error: Cannot demote the last administrator", file=sys.stderr)
        sys.exit(1)

    db.update_user(user.id, is_admin=False)
    print(f"✓ {user.name} demoted to regular user")


def delete_user(args):
    """Delete a user."""
    db, _ = init_services()

    user = db.get_user_by_email(args.email)
    if not user:
        print(f"Error: User with email '{args.email}' not found", file=sys.stderr)
        sys.exit(1)

    # Check if this is the last admin
    if user.is_admin and db.count_admins() <= 1:
        print("Error: Cannot delete the last administrator", file=sys.stderr)
        sys.exit(1)

    # Confirm deletion
    if not args.yes:
        confirm = input(f"Delete user {user.name} ({user.email})? [y/N] ")
        if confirm.lower() != 'y':
            print("Cancelled")
            return

    db.delete_user(user.id)
    print(f"✓ User {user.name} deleted")


def unlock_account(args):
    """Unlock a locked user account."""
    db, _ = init_services()

    user = db.get_user_by_email(args.email)
    if not user:
        print(f"Error: User with email '{args.email}' not found", file=sys.stderr)
        sys.exit(1)

    if not user.is_locked() and user.failed_login_attempts == 0:
        print(f"Account for {user.name} is not locked")
        return

    db.reset_failed_logins(user.id)
    print(f"✓ Account unlocked for {user.name} ({user.email})")
    print(f"  Failed login attempts reset to 0")


def list_api_keys(args):
    """List API keys for a user."""
    db, _ = init_services()

    user = db.get_user_by_email(args.email)
    if not user:
        print(f"Error: User with email '{args.email}' not found", file=sys.stderr)
        sys.exit(1)

    keys = db.list_user_api_keys(user.id)

    if not keys:
        print(f"No API keys found for {user.name}")
        return

    print(f"\nAPI keys for {user.name} ({user.email}):\n")
    print(f"{'Name':<25} {'Created':<20} {'Last Used':<20}")
    print("-" * 70)

    for key in keys:
        created = datetime.fromisoformat(key['created_at']).strftime('%Y-%m-%d %H:%M')
        last_used = datetime.fromisoformat(key['last_used_at']).strftime('%Y-%m-%d %H:%M') if key['last_used_at'] else 'Never'
        print(f"{key['name']:<25} {created:<20} {last_used:<20}")

    print(f"\nTotal: {len(keys)} key(s)")


def revoke_api_key(args):
    """Revoke an API key."""
    db, _ = init_services()

    user = db.get_user_by_email(args.email)
    if not user:
        print(f"Error: User with email '{args.email}' not found", file=sys.stderr)
        sys.exit(1)

    keys = db.list_user_api_keys(user.id)

    if not keys:
        print(f"No API keys found for {user.name}")
        return

    # Find key by name
    key_to_revoke = None
    for key in keys:
        if key['name'] == args.name:
            key_to_revoke = key
            break

    if not key_to_revoke:
        print(f"Error: API key '{args.name}' not found for user {user.name}", file=sys.stderr)
        sys.exit(1)

    # Confirm revocation
    if not args.yes:
        confirm = input(f"Revoke API key '{args.name}'? [y/N] ")
        if confirm.lower() != 'y':
            print("Cancelled")
            return

    db.delete_api_key(key_to_revoke['key_hash'])
    print(f"✓ API key '{args.name}' revoked")


def enable_auth(args):
    """Enable authentication."""
    _, config = init_services()

    if config.get_bool('auth.enabled', False):
        print("Authentication is already enabled")
        return

    config.set('auth.enabled', 'true')
    config.save()
    print("✓ Authentication enabled")


def disable_auth(args):
    """Disable authentication."""
    _, config = init_services()

    if not config.get_bool('auth.enabled', False):
        print("Authentication is already disabled")
        return

    # Confirm
    if not args.yes:
        print("Warning: This will allow unrestricted access to Beekeeper")
        confirm = input("Disable authentication? [y/N] ")
        if confirm.lower() != 'y':
            print("Cancelled")
            return

    config.set('auth.enabled', 'false')
    config.save()
    print("✓ Authentication disabled")


def clean_sessions(args):
    """Clean up expired sessions."""
    db, _ = init_services()

    db.cleanup_expired_sessions()
    print("✓ Expired sessions cleaned up")


def show_config(args):
    """Show current configuration."""
    _, config = init_services()

    print("\nCurrent Configuration:")
    print("-" * 50)
    print(f"Authentication enabled: {config.get_bool('auth.enabled', False)}")
    print(f"Session lifetime (days): {config.get_int('session.lifetime_days', 7)}")
    print(f"Min password length: {config.get_int('password.min_length', 8)}")
    print(f"API rate limit (req/min): {config.get_int('api.rate_limit_per_minute', 100)}")
    print()


def set_config(args):
    """Set a configuration value."""
    _, config = init_services()

    key = args.key
    value = args.value

    # Validate known config keys
    if key == 'api.rate_limit_per_minute':
        try:
            int_value = int(value)
            if int_value <= 0:
                print("Error: Rate limit must be a positive integer", file=sys.stderr)
                sys.exit(1)
        except ValueError:
            print("Error: Rate limit must be a valid integer", file=sys.stderr)
            sys.exit(1)

    config.set(key, value)
    config.save()
    print(f"✓ Configuration updated: {key} = {value}")


def main():
    parser = argparse.ArgumentParser(
        description='Beekeeper CLI Admin Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s list-users
  %(prog)s create-user --email admin@example.com --name "Admin User" --admin
  %(prog)s reset-password admin@example.com
  %(prog)s unlock-account admin@example.com
  %(prog)s promote admin@example.com
  %(prog)s list-api-keys admin@example.com
  %(prog)s enable-auth
  %(prog)s config
  %(prog)s set-config api.rate_limit_per_minute 200
        """
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    subparsers.required = True

    # list-users
    parser_list = subparsers.add_parser('list-users', help='List all users')
    parser_list.set_defaults(func=list_users)

    # create-user
    parser_create = subparsers.add_parser('create-user', help='Create a new user')
    parser_create.add_argument('--email', help='User email address')
    parser_create.add_argument('--name', help='User full name')
    parser_create.add_argument('--password', help='User password (will prompt if not provided)')
    parser_create.add_argument('--admin', action='store_true', help='Grant admin privileges')
    parser_create.set_defaults(func=create_user)

    # reset-password
    parser_reset = subparsers.add_parser('reset-password', help='Reset user password')
    parser_reset.add_argument('email', help='User email address')
    parser_reset.add_argument('--password', help='New password (will prompt if not provided)')
    parser_reset.set_defaults(func=reset_password)

    # promote
    parser_promote = subparsers.add_parser('promote', help='Promote user to admin')
    parser_promote.add_argument('email', help='User email address')
    parser_promote.set_defaults(func=promote_user)

    # demote
    parser_demote = subparsers.add_parser('demote', help='Demote admin to regular user')
    parser_demote.add_argument('email', help='User email address')
    parser_demote.set_defaults(func=demote_user)

    # delete-user
    parser_delete = subparsers.add_parser('delete-user', help='Delete a user')
    parser_delete.add_argument('email', help='User email address')
    parser_delete.add_argument('-y', '--yes', action='store_true', help='Skip confirmation')
    parser_delete.set_defaults(func=delete_user)

    # unlock-account
    parser_unlock = subparsers.add_parser('unlock-account', help='Unlock a locked user account')
    parser_unlock.add_argument('email', help='User email address')
    parser_unlock.set_defaults(func=unlock_account)

    # list-api-keys
    parser_list_keys = subparsers.add_parser('list-api-keys', help='List API keys for a user')
    parser_list_keys.add_argument('email', help='User email address')
    parser_list_keys.set_defaults(func=list_api_keys)

    # revoke-api-key
    parser_revoke = subparsers.add_parser('revoke-api-key', help='Revoke an API key')
    parser_revoke.add_argument('email', help='User email address')
    parser_revoke.add_argument('name', help='API key name')
    parser_revoke.add_argument('-y', '--yes', action='store_true', help='Skip confirmation')
    parser_revoke.set_defaults(func=revoke_api_key)

    # enable-auth
    parser_enable = subparsers.add_parser('enable-auth', help='Enable authentication')
    parser_enable.set_defaults(func=enable_auth)

    # disable-auth
    parser_disable = subparsers.add_parser('disable-auth', help='Disable authentication')
    parser_disable.add_argument('-y', '--yes', action='store_true', help='Skip confirmation')
    parser_disable.set_defaults(func=disable_auth)

    # clean-sessions
    parser_clean = subparsers.add_parser('clean-sessions', help='Clean up expired sessions')
    parser_clean.set_defaults(func=clean_sessions)

    # config
    parser_config = subparsers.add_parser('config', help='Show current configuration')
    parser_config.set_defaults(func=show_config)

    # set-config
    parser_set_config = subparsers.add_parser('set-config', help='Set a configuration value')
    parser_set_config.add_argument('key', help='Configuration key (e.g., api.rate_limit_per_minute)')
    parser_set_config.add_argument('value', help='Configuration value')
    parser_set_config.set_defaults(func=set_config)

    # Parse and execute
    args = parser.parse_args()

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nCancelled", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
