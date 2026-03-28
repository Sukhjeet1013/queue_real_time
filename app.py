import os
from datetime import datetime
from functools import wraps
from urllib.parse import urlsplit

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from sqlalchemy import case, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from config import Config
from models import Clinic, Patient, QueueEntry, User, db

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


# -----------------------------
# AUTH
# -----------------------------
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# -----------------------------
# HELPERS
# -----------------------------
def is_safe_redirect_target(target):
    if not target:
        return False
    parsed = urlsplit(target)
    return not parsed.netloc and parsed.path.startswith("/")


def redirect_after_login():
    next_url = request.args.get("next")
    if is_safe_redirect_target(next_url):
        return redirect(next_url)

    if current_user.role in {User.ROLE_SUPERADMIN, User.ROLE_CLINIC_ADMIN}:
        return redirect(url_for("admin_dashboard"))

    return redirect(url_for("home"))


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role not in {User.ROLE_SUPERADMIN, User.ROLE_CLINIC_ADMIN}:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def superadmin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != User.ROLE_SUPERADMIN:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def clinic_admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != User.ROLE_CLINIC_ADMIN:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


# -----------------------------
# DB SETUP
# -----------------------------
def ensure_queue_schema():
    inspector = inspect(db.engine)
    if "queue_entries" not in inspector.get_table_names():
        return


def seed_default_clinics():
    if Clinic.query.first():
        return

    clinics = [
        Clinic(clinic_name="City Clinic", doctor_name="Dr. Sharma"),
        Clinic(clinic_name="HealthCare Plus", doctor_name="Dr. Mehta"),
    ]
    db.session.add_all(clinics)
    db.session.commit()


# -----------------------------
# INIT ROUTE (FIXED)
# -----------------------------
@app.route("/init-db")
def init_db_route():
    db.create_all()
    ensure_queue_schema()

    username = (os.getenv("DEFAULT_SUPERADMIN_USERNAME") or "").strip()
    password = os.getenv("DEFAULT_SUPERADMIN_PASSWORD") or ""

    if username and password:
        existing = User.query.filter(
            func.lower(User.username) == username.lower()
        ).first()

        if not existing:
            admin = User(
                username=username,
                role=User.ROLE_SUPERADMIN
            )
            admin.set_password(password)
            db.session.add(admin)
            db.session.commit()

    seed_default_clinics()

    return "DB initialized + admin created"


# -----------------------------
# DEBUG (TEMP)
# -----------------------------
@app.route("/check-admin")
def check_admin():
    users = User.query.all()
    return str([(u.username, u.role) for u in users])


# -----------------------------
# ROUTES
# -----------------------------
@app.route("/")
def home():
    return render_template("landing.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        user = User.query.filter(
            func.lower(User.username) == username.lower()
        ).first()

        if not user or not user.check_password(password):
            return render_template("login.html", error="Invalid credentials")

        login_user(user)
        return redirect_after_login()

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("home"))


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    return render_template("admin_dashboard.html")


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    app.run(debug=True)