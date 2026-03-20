from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, make_response

from services.auth_service import hash_password, verify_password, create_session_for_user, get_current_user
from services.config_service import get_config_int
from services.db_service import get_db


auth_bp = Blueprint('auth', __name__, url_prefix='/auth')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Login page and handler."""
    if request.method == 'GET':
        return render_template('login.html')

    email = request.form.get('email', '').strip()
    password = request.form.get('password', '')

    if not email or not password:
        flash('Email and password are required', 'error')
        return redirect(url_for('auth.login'))

    db = get_db()
    user = db.get_user_by_email(email)

    if not user:
        flash('Invalid email or password', 'error')
        return redirect(url_for('auth.login'))

    password_hash = db.get_password_hash(user.id)
    if not verify_password(password, password_hash):
        flash('Invalid email or password', 'error')
        return redirect(url_for('auth.login'))

    # Update last login
    db.update_user(user.id, last_login_at=datetime.now())

    # Create session
    session_id = create_session_for_user(user.id)
    response = make_response(redirect(request.args.get('next') or url_for('dashboard.index')))
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

    response = make_response(redirect(url_for('auth.login')))
    response.delete_cookie('session_id')

    flash('You have been logged out', 'success')
    return response
