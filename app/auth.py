import functools
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, current_app, g,
)

auth_bp = Blueprint('auth', __name__)


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect(url_for('views.dashboard'))
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password and password == current_app.config['SECRET_PASSWORD']:
            session.permanent = True
            session['logged_in'] = True
            return redirect(url_for('views.dashboard'))
        flash('Incorrect password.', 'error')
    return render_template('login.html')


@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))
