from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, jsonify

from services.auth_service import admin_required, get_current_user
from services.config_service import get_config_bool, get_config_int, set_config, save_config, is_auth_enabled
from services.db_service import get_db
from services.ssh_key_service import get_public_key, regenerate_instance_key


admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

_PWD_MIN_LEN_KEY = 'password.min_length'
_INDEX_ROUTE = 'admin.index'


@admin_bp.route('/', methods=['GET'])
@admin_required
def index():
    """Admin panel home."""
    db = get_db()
    current_user = get_current_user()

    config = {
        'auth_enabled': is_auth_enabled(),
        'session_lifetime_days': get_config_int('session.lifetime_days', 7),
        'password_min_length': get_config_int(_PWD_MIN_LEN_KEY, 8),
        'api_rate_limit': get_config_int('api.rate_limit_per_minute', 100),
    }

    users = db.list_all_users()
    api_keys = db.list_user_api_keys(current_user.id) if current_user else []
    ssh_public_key = get_public_key(current_app.config['BEEKEEPER_HOME'])

    return render_template(
        'admin.html', config=config, users=users, api_keys=api_keys,
        current_user=current_user, ssh_public_key=ssh_public_key,
    )


@admin_bp.route('/settings', methods=['POST'])
@admin_required
def update_settings():
    """Update application settings."""
    auth_enabled = request.form.get('auth_enabled') == 'true'
    set_config('auth.enabled', 'true' if auth_enabled else 'false')

    # Update other settings if provided
    if 'session_lifetime_days' in request.form:
        try:
            days = int(request.form['session_lifetime_days'])
            if days > 0:
                set_config('session.lifetime_days', str(days))
        except ValueError:
            flash('Invalid session lifetime', 'error')
            return redirect(url_for(_INDEX_ROUTE))

    if 'password_min_length' in request.form:
        try:
            min_len = int(request.form['password_min_length'])
            if min_len >= 6:
                set_config(_PWD_MIN_LEN_KEY, str(min_len))
        except ValueError:
            flash('Invalid password minimum length', 'error')
            return redirect(url_for(_INDEX_ROUTE))

    if 'api_rate_limit' in request.form:
        try:
            rate_limit = int(request.form['api_rate_limit'])
            if rate_limit > 0:
                set_config('api.rate_limit_per_minute', str(rate_limit))
            else:
                flash('API rate limit must be greater than 0', 'error')
                return redirect(url_for(_INDEX_ROUTE))
        except ValueError:
            flash('Invalid API rate limit', 'error')
            return redirect(url_for(_INDEX_ROUTE))

    save_config()
    flash('Settings updated successfully', 'success')
    return redirect(url_for(_INDEX_ROUTE))


@admin_bp.route('/ssh-key/regenerate', methods=['POST'])
@admin_required
def regenerate_ssh_key():
    """Discard the instance SSH keypair and generate a fresh one."""
    regenerate_instance_key(current_app.config['BEEKEEPER_HOME'])
    flash(
        'New SSH key generated. Add the new public key to GitHub — projects using '
        'the old key will fail to clone or fetch until it is replaced there.',
        'warning',
    )
    return redirect(url_for(_INDEX_ROUTE))


@admin_bp.route('/users/<int:user_id>/toggle-admin', methods=['POST'])
@admin_required
def toggle_admin(user_id):
    """Toggle admin status for a user."""
    db = get_db()
    user = db.get_user_by_id(user_id)

    if not user:
        return "User not found", 404

    # Prevent removing last admin
    if user.is_admin:
        admin_count = db.count_admins()
        if admin_count <= 1:
            flash('Cannot remove the last administrator', 'error')
            return redirect(url_for(_INDEX_ROUTE))

    db.update_user(user_id, is_admin=not user.is_admin)
    action = 'removed admin privileges from' if user.is_admin else 'granted admin privileges to'
    flash(f'Successfully {action} {user.email}', 'success')
    return redirect(url_for(_INDEX_ROUTE))


@admin_bp.route('/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def delete_user(user_id):
    """Delete a user."""
    db = get_db()
    user = db.get_user_by_id(user_id)
    current_user = get_current_user()

    if not user:
        return "User not found", 404

    # Prevent deleting self
    if user.id == current_user.id:
        flash('Cannot delete your own account', 'error')
        return redirect(url_for(_INDEX_ROUTE))

    # Prevent deleting last admin
    if user.is_admin:
        admin_count = db.count_admins()
        if admin_count <= 1:
            flash('Cannot delete the last administrator', 'error')
            return redirect(url_for(_INDEX_ROUTE))

    db.delete_user(user_id)
    flash(f'Successfully deleted user {user.email}', 'success')
    return redirect(url_for(_INDEX_ROUTE))


@admin_bp.route('/api-keys', methods=['POST'])
@admin_required
def create_api_key():
    """Create a new API key for the current user."""
    name = request.form.get('name', 'Untitled').strip()
    if not name:
        name = 'Untitled'

    db = get_db()
    current_user = get_current_user()

    key = db.create_api_key(current_user.id, name)

    # Show key once (can't retrieve later)
    flash(f'API Key created: {key}', 'success')
    flash('Save this key now - you won\'t be able to see it again!', 'warning')
    return redirect(url_for(_INDEX_ROUTE))


@admin_bp.route('/api-keys/<key_hash>', methods=['POST'])
@admin_required
def delete_api_key(key_hash):
    """Delete an API key."""
    db = get_db()
    db.delete_api_key(key_hash)

    flash('API key deleted successfully', 'success')
    return redirect(url_for(_INDEX_ROUTE))


@admin_bp.route('/users/create', methods=['POST'])
@admin_required
def create_user():
    """Create a new user (admin only)."""
    from services.auth_service import hash_password

    email = request.form.get('email', '').strip()
    name = request.form.get('name', '').strip()
    password = request.form.get('password', '')
    is_admin = request.form.get('is_admin') == 'true'

    if not email or not name or not password:
        flash('All fields are required', 'error')
        return redirect(url_for(_INDEX_ROUTE))

    min_length = get_config_int(_PWD_MIN_LEN_KEY, 8)
    if len(password) < min_length:
        flash(f'Password must be at least {min_length} characters', 'error')
        return redirect(url_for(_INDEX_ROUTE))

    db = get_db()

    # Check if user already exists
    if db.get_user_by_email(email):
        flash(f'User with email {email} already exists', 'error')
        return redirect(url_for(_INDEX_ROUTE))

    password_hash = hash_password(password)
    db.create_user(email, name, password_hash, is_admin)

    flash(f'Successfully created user {email}', 'success')
    return redirect(url_for(_INDEX_ROUTE))
