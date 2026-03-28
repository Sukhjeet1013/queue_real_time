import os
from datetime import datetime
from functools import wraps
from urllib.parse import urlsplit

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from sqlalchemy import func, inspect, select, text
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
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "warning"


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


@login_manager.unauthorized_handler
def handle_unauthorized():
    flash("Please sign in to continue.", "warning")
    next_url = request.full_path if request.query_string else request.path
    return redirect(url_for("login", next=next_url))


def is_safe_redirect_target(target):
    if not target:
        return False

    current_parts = urlsplit(request.host_url)
    target_parts = urlsplit(target)
    return (not target_parts.netloc or target_parts.netloc == current_parts.netloc) and (
        target_parts.scheme in ("", "http", "https")
    )


def redirect_after_login(default_endpoint="home"):
    next_url = request.args.get("next") or request.form.get("next")
    if next_url and is_safe_redirect_target(next_url):
        return redirect(next_url)

    if current_user.is_authenticated and current_user.role in {
        User.ROLE_SUPERADMIN,
        User.ROLE_CLINIC_ADMIN,
    }:
        return redirect(url_for("admin_dashboard"))

    return redirect(url_for(default_endpoint))


def superadmin_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != User.ROLE_SUPERADMIN:
            abort(403, description="Only superadmins can access this page.")
        return view_func(*args, **kwargs)

    return wrapped


def clinic_admin_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != User.ROLE_CLINIC_ADMIN:
            abort(403, description="Only clinic admins can access this page.")
        if not current_user.clinic_id:
            abort(403, description="Your account is not assigned to a clinic.")
        return view_func(*args, **kwargs)

    return wrapped


def admin_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role not in {User.ROLE_SUPERADMIN, User.ROLE_CLINIC_ADMIN}:
            abort(403, description="You are not allowed to access the admin area.")
        return view_func(*args, **kwargs)

    return wrapped


def normalize_phone(raw_phone):
    digits = "".join(char for char in (raw_phone or "") if char.isdigit())
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits


def upgrade_legacy_password_if_needed(user, raw_password):
    if not user.password_uses_hash():
        user.set_password(raw_password)
        db.session.commit()


def verify_password(user, raw_password):
    if not user or not raw_password:
        return False

    if user.check_password(raw_password):
        upgrade_legacy_password_if_needed(user, raw_password)
        return True

    return False


def seed_default_clinics():
    if Clinic.query.count() > 0:
        return

    clinics = [
        Clinic(clinic_name="Downtown Medical Centre", doctor_name="Dr. Anika Sharma"),
        Clinic(clinic_name="Northside Family Clinic", doctor_name="Dr. Raj Malhotra"),
        Clinic(clinic_name="Lakeside Health Hub", doctor_name="Dr. Priya Mehta"),
    ]
    db.session.add_all(clinics)
    db.session.commit()


def seed_initial_superadmin():
    username = (os.getenv("DEFAULT_SUPERADMIN_USERNAME") or "").strip()
    password = os.getenv("DEFAULT_SUPERADMIN_PASSWORD") or ""

    if not username or not password:
        return

    existing_superadmin = db.session.scalar(
        select(User).where(User.role == User.ROLE_SUPERADMIN).limit(1)
    )
    if existing_superadmin:
        return

    username_taken = db.session.scalar(
        select(User).where(func.lower(User.username) == username.lower()).limit(1)
    )
    if username_taken:
        return

    user = User(username=username, role=User.ROLE_SUPERADMIN)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()


def repair_queue_entries():
    clinics = db.session.execute(select(Clinic.id)).scalars().all()

    for clinic_id in clinics:
        active_entries = (
            db.session.execute(
                select(QueueEntry)
                .where(
                    QueueEntry.clinic_id == clinic_id,
                    QueueEntry.status == QueueEntry.STATUS_IN_CONSULTATION,
                )
                .order_by(QueueEntry.token_number.asc())
            )
            .scalars()
            .all()
        )

        for stale_entry in active_entries[1:]:
            stale_entry.status = QueueEntry.STATUS_WAITING
            stale_entry.consultation_started_at = None

        served_without_start = (
            db.session.execute(
                select(QueueEntry).where(
                    QueueEntry.clinic_id == clinic_id,
                    QueueEntry.status == QueueEntry.STATUS_SERVED,
                    QueueEntry.consultation_started_at.is_(None),
                    QueueEntry.served_at.is_not(None),
                )
            )
            .scalars()
            .all()
        )

        for entry in served_without_start:
            entry.consultation_started_at = entry.served_at

    db.session.commit()


def ensure_queue_schema():
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()

    if "queue_entries" not in tables:
        return

    existing_columns = {column["name"] for column in inspector.get_columns("queue_entries")}
    if "consultation_started_at" not in existing_columns:
        with db.engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE queue_entries ADD COLUMN consultation_started_at TIMESTAMP NULL")
            )

    repair_queue_entries()

    existing_indexes = {index["name"] for index in inspector.get_indexes("queue_entries")}
    if "unique_active_queue_entry_per_clinic" not in existing_indexes:
        dialect = db.session.bind.dialect.name
        create_index_sql = None
        if dialect == "postgresql":
            create_index_sql = """
                CREATE UNIQUE INDEX IF NOT EXISTS unique_active_queue_entry_per_clinic
                ON queue_entries (clinic_id)
                WHERE status = 'in_consultation'
            """
        elif dialect == "sqlite":
            create_index_sql = """
                CREATE UNIQUE INDEX IF NOT EXISTS unique_active_queue_entry_per_clinic
                ON queue_entries (clinic_id)
                WHERE status = 'in_consultation'
            """

        if create_index_sql:
            with db.engine.begin() as connection:
                connection.execute(text(create_index_sql))


def bootstrap_database():
    db.create_all()
    ensure_queue_schema()
    seed_initial_superadmin()


def compute_estimated_wait_minutes(entry):
    if entry.status != QueueEntry.STATUS_WAITING:
        return 0 if entry.status == QueueEntry.STATUS_IN_CONSULTATION else None

    recent_served_entries = (
        db.session.execute(
            select(QueueEntry)
            .where(
                QueueEntry.clinic_id == entry.clinic_id,
                QueueEntry.status == QueueEntry.STATUS_SERVED,
                QueueEntry.consultation_started_at.is_not(None),
                QueueEntry.served_at.is_not(None),
            )
            .order_by(QueueEntry.served_at.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )

    durations = []
    for served_entry in recent_served_entries:
        duration = served_entry.served_at - served_entry.consultation_started_at
        duration_minutes = int(duration.total_seconds() // 60)
        if duration_minutes > 0:
            durations.append(duration_minutes)

    if not durations:
        return None

    average_duration = round(sum(durations) / len(durations))
    patients_ahead = db.session.scalar(
        select(func.count(QueueEntry.id)).where(
            QueueEntry.clinic_id == entry.clinic_id,
            QueueEntry.status == QueueEntry.STATUS_WAITING,
            QueueEntry.token_number < entry.token_number,
        )
    ) or 0
    has_active = db.session.scalar(
        select(func.count(QueueEntry.id)).where(
            QueueEntry.clinic_id == entry.clinic_id,
            QueueEntry.status == QueueEntry.STATUS_IN_CONSULTATION,
        )
    )
    slots_ahead = patients_ahead + (1 if has_active else 0)
    return average_duration * slots_ahead


def build_queue_snapshot(entry):
    now_serving_entry = db.session.scalar(
        select(QueueEntry)
        .where(
            QueueEntry.clinic_id == entry.clinic_id,
            QueueEntry.status == QueueEntry.STATUS_IN_CONSULTATION,
        )
        .order_by(QueueEntry.token_number.asc())
        .limit(1)
    )

    patients_ahead = 0
    if entry.status == QueueEntry.STATUS_WAITING:
        patients_ahead = db.session.scalar(
            select(func.count(QueueEntry.id)).where(
                QueueEntry.clinic_id == entry.clinic_id,
                QueueEntry.status == QueueEntry.STATUS_WAITING,
                QueueEntry.token_number < entry.token_number,
            )
        ) or 0

    status_labels = {
        QueueEntry.STATUS_WAITING: "Waiting",
        QueueEntry.STATUS_IN_CONSULTATION: "In Consultation",
        QueueEntry.STATUS_SERVED: "Served",
    }

    return {
        "entry_status": entry.status,
        "status_label": status_labels.get(entry.status, "Unknown"),
        "now_serving": now_serving_entry.token_number if now_serving_entry else None,
        "patients_ahead": patients_ahead,
        "estimated_wait": compute_estimated_wait_minutes(entry),
    }


def build_landing_context():
    clinic_count = db.session.scalar(select(func.count(Clinic.id))) or 0
    waiting_count = db.session.scalar(
        select(func.count(QueueEntry.id)).where(QueueEntry.status == QueueEntry.STATUS_WAITING)
    ) or 0
    active_count = db.session.scalar(
        select(func.count(QueueEntry.id)).where(
            QueueEntry.status == QueueEntry.STATUS_IN_CONSULTATION
        )
    ) or 0

    return {
        "clinic_count": clinic_count,
        "waiting_count": waiting_count,
        "active_count": active_count,
    }


def build_clinics_context():
    clinics = (
        db.session.execute(select(Clinic).order_by(Clinic.clinic_name.asc()))
        .scalars()
        .all()
    )
    return {
        "clinics": clinics,
        "clinic_count": len(clinics),
    }


def build_clinic_page_context(clinic):
    active_entry = db.session.scalar(
        select(QueueEntry)
        .where(
            QueueEntry.clinic_id == clinic.id,
            QueueEntry.status == QueueEntry.STATUS_IN_CONSULTATION,
        )
        .order_by(QueueEntry.token_number.asc())
        .limit(1)
    )
    waiting_count = db.session.scalar(
        select(func.count(QueueEntry.id)).where(
            QueueEntry.clinic_id == clinic.id,
            QueueEntry.status == QueueEntry.STATUS_WAITING,
        )
    ) or 0

    return {
        "clinic": clinic,
        "active_entry": active_entry,
        "waiting_count": waiting_count,
    }


def build_clinic_admin_dashboard_context(clinic_id):
    clinic = db.session.get(Clinic, clinic_id)
    if not clinic:
        abort(404, description="Clinic not found.")

    entries = (
        db.session.execute(
            select(QueueEntry)
            .options(joinedload(QueueEntry.patient))
            .where(QueueEntry.clinic_id == clinic_id)
            .order_by(QueueEntry.token_number.asc())
        )
        .scalars()
        .all()
    )
    active_entry = next(
        (entry for entry in entries if entry.status == QueueEntry.STATUS_IN_CONSULTATION),
        None,
    )
    waiting_count = sum(1 for entry in entries if entry.status == QueueEntry.STATUS_WAITING)
    served_count = sum(1 for entry in entries if entry.status == QueueEntry.STATUS_SERVED)

    return {
        "dashboard_role": "clinic_admin",
        "clinic": clinic,
        "entries": entries,
        "active_entry": active_entry,
        "waiting_count": waiting_count,
        "served_count": served_count,
        "can_call_next": bool(waiting_count) and active_entry is None,
    }


def build_superadmin_dashboard_context():
    clinics = (
        db.session.execute(
            select(Clinic)
            .options(joinedload(Clinic.users), joinedload(Clinic.queue_entries))
            .order_by(Clinic.clinic_name.asc())
        )
        .unique()
        .scalars()
        .all()
    )

    clinic_rows = []
    total_waiting = 0
    for clinic in clinics:
        admins = [user for user in clinic.users if user.role == User.ROLE_CLINIC_ADMIN]
        waiting_entries = [
            entry for entry in clinic.queue_entries if entry.status == QueueEntry.STATUS_WAITING
        ]
        active_entry = next(
            (
                entry
                for entry in sorted(clinic.queue_entries, key=lambda row: row.token_number)
                if entry.status == QueueEntry.STATUS_IN_CONSULTATION
            ),
            None,
        )
        total_waiting += len(waiting_entries)
        clinic_rows.append(
            {
                "clinic": clinic,
                "admins": admins,
                "waiting_count": len(waiting_entries),
                "total_entries": len(clinic.queue_entries),
                "has_active_consultation": active_entry is not None,
                "active_token": active_entry.token_number if active_entry else None,
            }
        )

    admin_users = (
        db.session.execute(
            select(User)
            .options(joinedload(User.clinic))
            .where(User.role == User.ROLE_CLINIC_ADMIN)
            .order_by(User.username.asc())
        )
        .scalars()
        .all()
    )

    return {
        "dashboard_role": "superadmin",
        "clinic_rows": clinic_rows,
        "admin_users": admin_users,
        "total_clinics": len(clinics),
        "total_admins": len(admin_users),
        "total_waiting": total_waiting,
    }


def build_assign_admin_context():
    clinics = (
        db.session.execute(select(Clinic).order_by(Clinic.clinic_name.asc()))
        .scalars()
        .all()
    )
    admin_users = (
        db.session.execute(
            select(User)
            .options(joinedload(User.clinic))
            .where(User.role == User.ROLE_CLINIC_ADMIN)
            .order_by(User.username.asc())
        )
        .scalars()
        .all()
    )
    return {
        "clinics": clinics,
        "admin_users": admin_users,
    }


def create_queue_entry(clinic_id, patient_name, phone_number):
    for _ in range(3):
        patient = Patient(name=patient_name, phone=phone_number)
        db.session.add(patient)
        db.session.flush()

        next_token = (
            db.session.scalar(
                select(func.coalesce(func.max(QueueEntry.token_number), 0)).where(
                    QueueEntry.clinic_id == clinic_id
                )
            )
            or 0
        ) + 1

        entry = QueueEntry(
            clinic_id=clinic_id,
            patient_id=patient.id,
            token_number=next_token,
            status=QueueEntry.STATUS_WAITING,
        )
        db.session.add(entry)

        try:
            db.session.commit()
            return entry
        except IntegrityError:
            db.session.rollback()

    raise IntegrityError("Unable to create queue entry.", params=None, orig=None)


@app.context_processor
def inject_navigation_state():
    return {
        "is_superadmin": current_user.is_authenticated
        and current_user.role == User.ROLE_SUPERADMIN,
        "is_clinic_admin": current_user.is_authenticated
        and current_user.role == User.ROLE_CLINIC_ADMIN,
    }


@app.cli.command("init-db")
def init_db_command():
    bootstrap_database()
    seed_default_clinics()
    print("Database initialized successfully.")


@app.route("/init-db")
def init_db_route():
    bootstrap_database()
    seed_default_clinics()
    return "Database initialized successfully."


@app.route("/")
def home():
    if current_user.is_authenticated and current_user.role in {
        User.ROLE_SUPERADMIN,
        User.ROLE_CLINIC_ADMIN,
    }:
        return redirect(url_for("admin_dashboard"))

    return render_template("landing.html", **build_landing_context())


@app.route("/clinics")
def clinics_list():
    return render_template("clinics.html", **build_clinics_context())


@app.route("/clinic/<int:clinic_id>")
def clinic_page(clinic_id):
    clinic = db.session.get(Clinic, clinic_id)
    if not clinic:
        abort(404, description="Clinic not found.")
    return render_template("clinic.html", **build_clinic_page_context(clinic))


@app.route("/join_queue", methods=["POST"])
def join_queue():
    clinic_id = request.form.get("clinic_id", type=int)
    clinic = db.session.get(Clinic, clinic_id) if clinic_id else None
    if not clinic:
        abort(404, description="Clinic not found.")

    name = (request.form.get("name") or "").strip()
    phone = normalize_phone(request.form.get("phone"))

    if not name:
        flash("Please enter your full name.", "danger")
        return redirect(url_for("clinic_page", clinic_id=clinic.id))

    if len(phone) != 10:
        flash("Please enter a valid 10-digit phone number.", "danger")
        return redirect(url_for("clinic_page", clinic_id=clinic.id))

    try:
        entry = create_queue_entry(clinic.id, name, phone)
    except IntegrityError:
        db.session.rollback()
        flash("We could not generate your token right now. Please try again.", "danger")
        return redirect(url_for("clinic_page", clinic_id=clinic.id))

    flash(
        f"You're in the queue for {clinic.clinic_name}. Your token is {entry.token_number}.",
        "success",
    )
    return redirect(url_for("queue_status", entry_id=entry.id))


@app.route("/queue/<int:entry_id>")
def queue_status(entry_id):
    entry = db.session.execute(
        select(QueueEntry)
        .options(joinedload(QueueEntry.patient), joinedload(QueueEntry.clinic))
        .where(QueueEntry.id == entry_id)
    ).scalar_one_or_none()
    if not entry:
        abort(404, description="Queue entry not found.")

    return render_template(
        "queue.html",
        entry=entry,
        clinic=entry.clinic,
        **build_queue_snapshot(entry),
    )


@app.route("/api/queue_status/<int:entry_id>")
def queue_status_api(entry_id):
    entry = db.session.execute(
        select(QueueEntry).where(QueueEntry.id == entry_id)
    ).scalar_one_or_none()
    if not entry:
        return jsonify({"error": "Queue entry not found."}), 404

    return jsonify(build_queue_snapshot(entry))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect_after_login()

    error_message = None
    next_url = request.args.get("next") or request.form.get("next") or ""

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if not username or not password:
            error_message = "Please enter both username and password."
        else:
            user = db.session.scalar(
                select(User).where(func.lower(User.username) == username.lower()).limit(1)
            )
            if not verify_password(user, password):
                error_message = "Invalid username or password."
            else:
                login_user(user)
                flash(f"Welcome back, {user.username}.", "success")
                return redirect_after_login()

    return render_template("login.html", error_message=error_message, next_url=next_url)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("home"))


@app.route("/admin")
@admin_required
def admin_root():
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    if current_user.role == User.ROLE_SUPERADMIN:
        return render_template("admin_dashboard.html", **build_superadmin_dashboard_context())

    return render_template(
        "admin_dashboard.html",
        **build_clinic_admin_dashboard_context(current_user.clinic_id),
    )


@app.route("/admin/add_clinic", methods=["GET", "POST"])
@app.route("/add_clinic", methods=["GET", "POST"])
@superadmin_required
def add_clinic():
    if request.method == "POST":
        clinic_name = (request.form.get("clinic_name") or "").strip()
        doctor_name = (request.form.get("doctor_name") or "").strip()

        if not clinic_name or not doctor_name:
            flash("Clinic name and doctor name are both required.", "danger")
            return render_template("add_clinic.html")

        clinic = Clinic(clinic_name=clinic_name, doctor_name=doctor_name)
        db.session.add(clinic)
        db.session.commit()
        flash(f"{clinic_name} has been added successfully.", "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("add_clinic.html")


@app.route("/admin/assign_admin", methods=["GET", "POST"])
@superadmin_required
def assign_admin():
    context = build_assign_admin_context()

    if request.method == "GET" and not context["clinics"]:
        flash("Add a clinic before assigning a clinic admin.", "warning")
        return redirect(url_for("add_clinic"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        clinic_id = request.form.get("clinic_id", type=int)
        clinic = db.session.get(Clinic, clinic_id) if clinic_id else None

        if not username or not password or not clinic:
            flash("Username, password, and clinic assignment are required.", "danger")
            return render_template("assign_admin.html", **context)

        if len(password) < 8:
            flash("Clinic admin passwords must be at least 8 characters long.", "danger")
            return render_template("assign_admin.html", **context)

        existing_user = db.session.scalar(
            select(User).where(func.lower(User.username) == username.lower()).limit(1)
        )
        if existing_user:
            flash("That username is already in use.", "danger")
            return render_template("assign_admin.html", **context)

        admin_user = User(
            username=username,
            role=User.ROLE_CLINIC_ADMIN,
            clinic_id=clinic.id,
        )
        admin_user.set_password(password)
        db.session.add(admin_user)
        db.session.commit()
        flash(f"Clinic admin {username} has been assigned to {clinic.clinic_name}.", "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("assign_admin.html", **context)


@app.route("/call_next/<int:clinic_id>", methods=["POST"])
@clinic_admin_required
def call_next(clinic_id):
    if clinic_id != current_user.clinic_id:
        abort(403, description="You can only manage your assigned clinic.")

    clinic = db.session.get(Clinic, clinic_id)
    if not clinic:
        abort(404, description="Clinic not found.")

    try:
        db.session.execute(select(Clinic).where(Clinic.id == clinic_id).with_for_update()).scalar_one()

        active_entry = db.session.execute(
            select(QueueEntry)
            .where(
                QueueEntry.clinic_id == clinic_id,
                QueueEntry.status == QueueEntry.STATUS_IN_CONSULTATION,
            )
            .with_for_update()
            .limit(1)
        ).scalar_one_or_none()
        if active_entry:
            flash("A patient is already in consultation.", "warning")
            db.session.rollback()
            return redirect(url_for("admin_dashboard"))

        next_entry = db.session.execute(
            select(QueueEntry)
            .where(
                QueueEntry.clinic_id == clinic_id,
                QueueEntry.status == QueueEntry.STATUS_WAITING,
            )
            .order_by(QueueEntry.token_number.asc())
            .with_for_update()
            .limit(1)
        ).scalar_one_or_none()
        if not next_entry:
            flash("There are no waiting patients to call.", "info")
            db.session.rollback()
            return redirect(url_for("admin_dashboard"))

        next_entry.status = QueueEntry.STATUS_IN_CONSULTATION
        next_entry.consultation_started_at = datetime.utcnow()
        db.session.commit()
        flash(f"Token {next_entry.token_number} is now in consultation.", "success")
    except IntegrityError:
        db.session.rollback()
        flash("Queue state changed just now. Please try again.", "warning")

    return redirect(url_for("admin_dashboard"))


@app.route("/mark_served/<int:entry_id>", methods=["POST"])
@app.route("/complete/<int:entry_id>", methods=["POST"])
@clinic_admin_required
def mark_served(entry_id):
    entry = db.session.execute(
        select(QueueEntry)
        .options(joinedload(QueueEntry.patient))
        .where(QueueEntry.id == entry_id)
        .with_for_update()
    ).scalar_one_or_none()
    if not entry:
        abort(404, description="Queue entry not found.")

    if entry.clinic_id != current_user.clinic_id:
        abort(403, description="You can only update queue entries from your clinic.")

    if entry.status != QueueEntry.STATUS_IN_CONSULTATION:
        flash("Only the active consultation can be marked as served.", "warning")
        db.session.rollback()
        return redirect(url_for("admin_dashboard"))

    entry.status = QueueEntry.STATUS_SERVED
    entry.served_at = datetime.utcnow()
    if entry.consultation_started_at is None:
        entry.consultation_started_at = entry.served_at

    db.session.commit()
    flash(f"Token {entry.token_number} has been marked as served.", "success")
    return redirect(url_for("admin_dashboard"))


@app.errorhandler(403)
def forbidden_page(error):
    return (
        render_template(
            "403.html",
            error_message=getattr(
                error,
                "description",
                "You do not have access to this page.",
            ),
        ),
        403,
    )


@app.errorhandler(404)
def not_found_page(error):
    return (
        render_template(
            "404.html",
            error_message=getattr(
                error,
                "description",
                "The page you requested could not be found.",
            ),
        ),
        404,
    )


with app.app_context():
    bootstrap_database()

# -----------------------------
# FIXED BOOTSTRAP SECTION
# -----------------------------

if __name__ != "__main__":
    with app.app_context():
        bootstrap_database()


if __name__ == "__main__":
    app.run(debug=True)