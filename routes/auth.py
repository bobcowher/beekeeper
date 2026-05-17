import logging
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, make_response

from services.auth_service import hash_password, verify_password, create_session_for_user, get_current_user
from services.config_service import get_config_int
from services.db_service import get_db

logger = logging.getLogger(__name__)


auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

_LOGIN_ROUTE = 'auth.login'


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Login page and handler."""
    if request.method == 'GET':
        return render_template('login.html')

    email = request.form.get('email', '').strip()
    password = request.form.get('password', '')

    if not email or not password:
        flash('Email and password are required', 'error')
        return redirect(url_for(_LOGIN_ROUTE))

    db = get_db()
    user = db.get_user_by_email(email)
    client_ip = request.remote_addr

    if not user:
        logger.warning(f"Login attempt from {client_ip} for non-existent email: {email}")
        flash('Invalid email or password', 'error')
        return redirect(url_for(_LOGIN_ROUTE))

    # Check if account is locked
    locked_until = user.locked_until
    if user.is_locked() and locked_until:
        minutes_left = int((locked_until - datetime.now()).total_seconds() / 60) + 1
        logger.warning(f"Login attempt from {client_ip} for locked account: {email}")
        flash(f'Account locked due to too many failed login attempts. Try again in {minutes_left} minute(s).', 'error')
        return redirect(url_for(_LOGIN_ROUTE))

    password_hash = db.get_password_hash(user.id)
    if not password_hash:
        logger.error("No password hash for user %s — treating as invalid credentials", email)
        flash('Invalid email or password', 'error')
        return redirect(url_for(_LOGIN_ROUTE))
    if not verify_password(password, password_hash):
        # Record failed login attempt
        db.record_failed_login(user.id)

        # Re-fetch user to get updated failed attempts count
        user = db.get_user_by_email(email)
        if not user:
            flash('Invalid email or password', 'error')
            return redirect(url_for(_LOGIN_ROUTE))
        attempts_left = 5 - user.failed_login_attempts

        logger.warning(
            f"Failed login attempt from {client_ip} for {email} "
            f"(attempt {user.failed_login_attempts}/5, {attempts_left} remaining)"
        )

        if attempts_left > 0:
            flash(f'Invalid email or password. {attempts_left} attempt(s) remaining before account lockout.', 'error')
        else:
            logger.warning(f"Account {email} locked due to failed login attempts from {client_ip}")
            flash('Account locked due to too many failed login attempts. Locked for 15 minutes.', 'error')

        return redirect(url_for(_LOGIN_ROUTE))

    # Successful login - reset failed attempts
    db.reset_failed_logins(user.id)

    # Update last login
    db.update_user(user.id, last_login_at=datetime.now())

    logger.info(f"Successful login from {client_ip} for {email}")

    # Create session
    session_id = create_session_for_user(user.id)
    next_url = request.args.get('next', '')
    if not (next_url and next_url.startswith('/') and not next_url.startswith('//')):  # NOSONAR
        next_url = url_for('dashboard.index')
    response = make_response(redirect(next_url))
    response.set_cookie('session_id', session_id, httponly=True, max_age=60*60*24*7)

    flash(f'Welcome back, {user.name}!', 'success')
    return response


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    """Registration page (only accessible when no users exist)."""
    db = get_db()

    # Only allow registration if no users exist (first user setup)
    if db.get_user_count() > 0:
        return "Registration is disabled", 403

    if request.method == 'GET':
        return render_template('register.html')

    email = request.form.get('email', '').strip()
    name = request.form.get('name', '').strip()
    password = request.form.get('password', '')

    # Validate
    if not email or not name or not password:
        flash('All fields are required', 'error')
        return redirect(url_for('auth.register'))

    min_length = get_config_int('password.min_length', 8)
    if len(password) < min_length:
        flash(f'Password must be at least {min_length} characters', 'error')
        return redirect(url_for('auth.register'))

    # Create first user as admin
    password_hash = hash_password(password)
    user = db.create_user(email, name, password_hash, is_admin=True)

    # Auto-login
    session_id = create_session_for_user(user.id)
    response = make_response(redirect(url_for('dashboard.index')))
    response.set_cookie('session_id', session_id, httponly=True, max_age=60*60*24*7)

    flash('Account created successfully! You are now logged in as an administrator.', 'success')
    return response


@auth_bp.route('/logout', methods=['POST'])
def logout():
    """Logout handler."""
    session_id = request.cookies.get('session_id')
    if session_id:
        db = get_db()
        db.delete_session(session_id)

    response = make_response(redirect(url_for(_LOGIN_ROUTE)))
    response.delete_cookie('session_id')

    flash('You have been logged out', 'success')
    return response
