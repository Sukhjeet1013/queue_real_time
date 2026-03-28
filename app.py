import os
from datetime import timedelta
from functools import wraps
from threading import Lock
from urllib.parse import urlsplit

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from config import Config
from models import Clinic, IST, Patient, QueueEntry, User, db, ist_now

BOOTSTRAP_LOCK = Lock()
login_manager = LoginManager()


def to_ist(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def duration_minutes(start_at, end_at):
    if not start_at or not end_at:
        return None
    total_seconds = (to_ist(end_at) - to_ist(start_at)).total_seconds()
    if total_seconds <= 0:
        return None
    return max(1, round(total_seconds / 60))


def clamp_consultation_duration(minutes):
    if minutes is None:
        return None
    if minutes <= 0:
        return None
    if minutes > Config.MAX_CONSULTATION_TIME:
        return None
    return max(Config.MIN_CONSULTATION_TIME, minutes)


def weighted_average(values):
    if not values:
        return None

    weighted_total = 0
    weight_sum = 0
    total_values = len(values)
    for index, value in enumerate(values):
        weight = total_values - index
        weighted_total += value * weight
        weight_sum += weight

    return round(weighted_total / weight_sum) if weight_sum else None


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


@login_manager.unauthorized_handler
def unauthorized_handler():
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
            abort(403, description="You do not have access to the admin area.")
        return view_func(*args, **kwargs)

    return wrapped


def normalize_phone(raw_phone):
    digits = "".join(char for char in (raw_phone or "") if char.isdigit())
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits


def verify_password(user, raw_password):
    if not user or not raw_password:
        return False
    password_valid = user.check_password(raw_password)
    if password_valid and not user.password_uses_hash():
        user.set_password(raw_password)
        db.session.commit()
    return password_valid


def seed_default_clinics():
    if db.session.scalar(select(func.count(Clinic.id))) or 0:
        return
    db.session.add_all(
        [
            Clinic(clinic_name="Downtown Medical Centre", doctor_name="Dr. Anika Sharma"),
            Clinic(clinic_name="Northside Family Clinic", doctor_name="Dr. Raj Malhotra"),
            Clinic(clinic_name="Lakeside Health Hub", doctor_name="Dr. Priya Mehta"),
        ]
    )
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


def repair_queue_state():
    clinic_ids = db.session.execute(select(Clinic.id)).scalars().all()
    for clinic_id in clinic_ids:
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

        served_entries = (
            db.session.execute(
                select(QueueEntry).where(
                    QueueEntry.clinic_id == clinic_id,
                    QueueEntry.status == QueueEntry.STATUS_SERVED,
                )
            )
            .scalars()
            .all()
        )
        for entry in served_entries:
            if entry.served_at is None:
                entry.served_at = to_ist(entry.consultation_started_at) or to_ist(entry.joined_at) or ist_now()
            if entry.consultation_started_at is None:
                joined_at = to_ist(entry.joined_at)
                served_at = to_ist(entry.served_at)
                fallback_start = served_at - timedelta(minutes=Config.DEFAULT_CONSULTATION_TIME)
                entry.consultation_started_at = joined_at if joined_at and joined_at < served_at else fallback_start
    db.session.commit()


def ensure_column(connection, inspector, table_name, column_name, sql_type):
    existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name in existing_columns:
        return
    connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {sql_type} NULL"))


def ensure_postgres_timezone_columns(connection, inspector):
    column_targets = {
        "patients": ["created_at"],
        "queue_entries": ["joined_at", "consultation_started_at", "served_at"],
    }
    for table_name, column_names in column_targets.items():
        if table_name not in inspector.get_table_names():
            continue
        columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        for column_name in column_names:
            column = columns.get(column_name)
            if not column:
                continue
            column_type = column.get("type")
            if getattr(column_type, "timezone", False):
                continue
            connection.execute(
                text(
                    f"""
                    ALTER TABLE {table_name}
                    ALTER COLUMN {column_name}
                    TYPE TIMESTAMPTZ
                    USING CASE
                        WHEN {column_name} IS NULL THEN NULL
                        ELSE {column_name} AT TIME ZONE 'UTC'
                    END
                    """
                )
            )


def ensure_queue_schema():
    engine = db.engine
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    if "queue_entries" not in tables:
        return

    timestamp_sql = "TIMESTAMPTZ" if engine.dialect.name == "postgresql" else "TIMESTAMP"
    with engine.begin() as connection:
        ensure_column(connection, inspector, "clinics", "average_consultation_minutes", "INTEGER")
        ensure_column(connection, inspector, "queue_entries", "consultation_started_at", timestamp_sql)
        if engine.dialect.name == "postgresql":
            inspector = inspect(engine)
            ensure_postgres_timezone_columns(connection, inspector)
        existing_indexes = {index["name"] for index in inspector.get_indexes("queue_entries")}
        if "unique_active_queue_entry_per_clinic" not in existing_indexes:
            connection.execute(
                text(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS unique_active_queue_entry_per_clinic
                    ON queue_entries (clinic_id)
                    WHERE status = 'in_consultation'
                    """
                )
            )
    repair_queue_state()


def bootstrap_database():
    db.create_all()
    ensure_queue_schema()
    seed_initial_superadmin()


def ensure_bootstrapped(app):
    if app.config.get("_SMARTQUEUE_DB_READY"):
        return
    with BOOTSTRAP_LOCK:
        if app.config.get("_SMARTQUEUE_DB_READY"):
            return
        bootstrap_database()
        app.config["_SMARTQUEUE_DB_READY"] = True


def build_landing_context():
    clinic_count = db.session.scalar(select(func.count(Clinic.id))) or 0
    waiting_count = db.session.scalar(
        select(func.count(QueueEntry.id)).where(QueueEntry.status == QueueEntry.STATUS_WAITING)
    ) or 0
    active_count = db.session.scalar(
        select(func.count(QueueEntry.id)).where(QueueEntry.status == QueueEntry.STATUS_IN_CONSULTATION)
    ) or 0
    return {
        "clinic_count": clinic_count,
        "waiting_count": waiting_count,
        "active_count": active_count,
    }


def build_clinics_context():
    clinics = db.session.execute(select(Clinic).order_by(Clinic.clinic_name.asc())).scalars().all()
    return {"clinics": clinics, "clinic_count": len(clinics)}


def get_active_entry_for_clinic(clinic_id):
    return db.session.scalar(
        select(QueueEntry)
        .where(
            QueueEntry.clinic_id == clinic_id,
            QueueEntry.status == QueueEntry.STATUS_IN_CONSULTATION,
        )
        .order_by(QueueEntry.token_number.asc())
        .limit(1)
    )


def count_patients_ahead(entry):
    if entry.status != QueueEntry.STATUS_WAITING:
        return 0
    waiting_ahead = db.session.scalar(
        select(func.count(QueueEntry.id)).where(
            QueueEntry.clinic_id == entry.clinic_id,
            QueueEntry.status == QueueEntry.STATUS_WAITING,
            QueueEntry.token_number < entry.token_number,
        )
    ) or 0
    active_entry = get_active_entry_for_clinic(entry.clinic_id)
    active_ahead = 0
    if active_entry and active_entry.id != entry.id and active_entry.token_number < entry.token_number:
        active_ahead = 1
    return waiting_ahead + active_ahead


def get_recent_consultation_durations(clinic_id, sample_size=None):
    effective_sample_size = sample_size or Config.WAIT_TIME_SAMPLE_SIZE
    recent_served_entries = (
        db.session.execute(
            select(QueueEntry)
            .where(
                QueueEntry.clinic_id == clinic_id,
                QueueEntry.status == QueueEntry.STATUS_SERVED,
                QueueEntry.consultation_started_at.is_not(None),
                QueueEntry.served_at.is_not(None),
            )
            .order_by(QueueEntry.served_at.desc())
            .limit(effective_sample_size)
        )
        .scalars()
        .all()
    )

    durations = []
    for entry in recent_served_entries:
        minutes = duration_minutes(entry.consultation_started_at, entry.served_at)
        normalized_minutes = clamp_consultation_duration(minutes)
        if normalized_minutes is not None:
            durations.append(normalized_minutes)

    return durations


def get_clinic_consultation_baseline(clinic_id):
    clinic = db.session.get(Clinic, clinic_id)
    if clinic and clinic.average_consultation_minutes:
        return max(Config.MIN_CONSULTATION_TIME, clinic.average_consultation_minutes)
    return Config.DEFAULT_CONSULTATION_TIME


def compute_average_consultation_minutes(clinic_id):
    durations = get_recent_consultation_durations(clinic_id)
    weighted_minutes = weighted_average(durations)
    if weighted_minutes is not None:
        return max(Config.MIN_CONSULTATION_TIME, weighted_minutes)
    return get_clinic_consultation_baseline(clinic_id)


def refresh_clinic_average_consultation_minutes(clinic_id):
    clinic = db.session.get(Clinic, clinic_id)
    if not clinic:
        return Config.DEFAULT_CONSULTATION_TIME

    rolling_average = weighted_average(get_recent_consultation_durations(clinic_id))
    if rolling_average is None:
        clinic.average_consultation_minutes = clinic.average_consultation_minutes or Config.DEFAULT_CONSULTATION_TIME
    else:
        clinic.average_consultation_minutes = max(Config.MIN_CONSULTATION_TIME, rolling_average)

    return clinic.average_consultation_minutes


def compute_estimated_wait_minutes(entry):
    if entry.status == QueueEntry.STATUS_IN_CONSULTATION:
        return 0
    if entry.status == QueueEntry.STATUS_SERVED:
        return None

    consultation_minutes = compute_average_consultation_minutes(entry.clinic_id)
    queue_slots_ahead = max(1, count_patients_ahead(entry))
    return max(Config.MIN_CONSULTATION_TIME, consultation_minutes * queue_slots_ahead)


def build_queue_snapshot(entry):
    active_entry = get_active_entry_for_clinic(entry.clinic_id)
    labels = {
        QueueEntry.STATUS_WAITING: "Waiting",
        QueueEntry.STATUS_IN_CONSULTATION: "In Consultation",
        QueueEntry.STATUS_SERVED: "Served",
    }
    return {
        "entry_status": entry.status,
        "status_label": labels.get(entry.status, "Unknown"),
        "now_serving": active_entry.token_number if active_entry else None,
        "patients_ahead": count_patients_ahead(entry),
        "estimated_wait": compute_estimated_wait_minutes(entry),
    }


def build_clinic_page_context(clinic):
    waiting_count = db.session.scalar(
        select(func.count(QueueEntry.id)).where(
            QueueEntry.clinic_id == clinic.id,
            QueueEntry.status == QueueEntry.STATUS_WAITING,
        )
    ) or 0
    return {
        "clinic": clinic,
        "active_entry": get_active_entry_for_clinic(clinic.id),
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
    active_entry = next((entry for entry in entries if entry.status == QueueEntry.STATUS_IN_CONSULTATION), None)
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
        waiting_count = sum(1 for entry in clinic.queue_entries if entry.status == QueueEntry.STATUS_WAITING)
        active_entry = next(
            (
                entry
                for entry in sorted(clinic.queue_entries, key=lambda row: row.token_number)
                if entry.status == QueueEntry.STATUS_IN_CONSULTATION
            ),
            None,
        )
        total_waiting += waiting_count
        clinic_rows.append(
            {
                "clinic": clinic,
                "admins": admins,
                "waiting_count": waiting_count,
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
    clinics = db.session.execute(select(Clinic).order_by(Clinic.clinic_name.asc())).scalars().all()
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
    return {"clinics": clinics, "admin_users": admin_users}


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
            joined_at=ist_now(),
        )
        db.session.add(entry)
        try:
            db.session.commit()
            return entry
        except IntegrityError:
            db.session.rollback()
    raise IntegrityError("Could not create queue entry.", params=None, orig=None)


def register_template_helpers(app):
    @app.context_processor
    def inject_navigation_state():
        return {
            "is_superadmin": current_user.is_authenticated and current_user.role == User.ROLE_SUPERADMIN,
            "is_clinic_admin": current_user.is_authenticated and current_user.role == User.ROLE_CLINIC_ADMIN,
        }

    @app.template_filter("format_ist")
    def format_ist(value, fmt="%d %b %Y, %I:%M %p"):
        localized_value = to_ist(value)
        return localized_value.strftime(fmt) if localized_value else "-"


def register_cli(app):
    @app.cli.command("init-db")
    def init_db_command():
        with app.app_context():
            ensure_bootstrapped(app)
            seed_default_clinics()
        print("Database initialized successfully.")


def register_routes(app):
    @app.before_request
    def bootstrap_once():
        ensure_bootstrapped(app)

    @app.route("/init-db")
    def init_db_route():
        ensure_bootstrapped(app)
        seed_default_clinics()
        return "Database initialized successfully."

    @app.route("/")
    def home():
        if current_user.is_authenticated and current_user.role in {User.ROLE_SUPERADMIN, User.ROLE_CLINIC_ADMIN}:
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
        flash(f"You're in the queue for {clinic.clinic_name}. Your token is {entry.token_number}.", "success")
        return redirect(url_for("queue_status", entry_id=entry.id))

    @app.route("/queue/<int:entry_id>")
    def queue_status(entry_id):
        entry = db.session.execute(
            select(QueueEntry).options(joinedload(QueueEntry.clinic)).where(QueueEntry.id == entry_id)
        ).scalar_one_or_none()
        if not entry:
            abort(404, description="Queue entry not found.")
        return render_template("queue.html", entry=entry, clinic=entry.clinic, **build_queue_snapshot(entry))

    @app.route("/api/queue_status/<int:entry_id>")
    def queue_status_api(entry_id):
        entry = db.session.scalar(select(QueueEntry).where(QueueEntry.id == entry_id))
        if not entry:
            return jsonify({"error": "Queue entry not found."}), 404
        payload = build_queue_snapshot(entry)
        payload["updated_at"] = to_ist(ist_now()).isoformat()
        return jsonify(payload)

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
        return render_template("admin_dashboard.html", **build_clinic_admin_dashboard_context(current_user.clinic_id))

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
            db.session.add(Clinic(clinic_name=clinic_name, doctor_name=doctor_name))
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
            admin_user = User(username=username, role=User.ROLE_CLINIC_ADMIN, clinic_id=clinic.id)
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
        active_entry = get_active_entry_for_clinic(clinic_id)
        if active_entry:
            flash("A patient is already in consultation.", "warning")
            return redirect(url_for("admin_dashboard"))
        next_entry = db.session.scalar(
            select(QueueEntry)
            .where(
                QueueEntry.clinic_id == clinic_id,
                QueueEntry.status == QueueEntry.STATUS_WAITING,
            )
            .order_by(QueueEntry.token_number.asc())
            .limit(1)
        )
        if not next_entry:
            flash("There are no waiting patients to call.", "info")
            return redirect(url_for("admin_dashboard"))
        next_entry.status = QueueEntry.STATUS_IN_CONSULTATION
        next_entry.consultation_started_at = ist_now()
        try:
            db.session.commit()
            flash(f"Token {next_entry.token_number} is now in consultation.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("Queue state changed while calling the next patient. Please try again.", "warning")
        return redirect(url_for("admin_dashboard"))

    @app.route("/mark_served/<int:entry_id>", methods=["POST"])
    @app.route("/complete/<int:entry_id>", methods=["POST"])
    @clinic_admin_required
    def mark_served(entry_id):
        entry = db.session.execute(
            select(QueueEntry).options(joinedload(QueueEntry.patient)).where(QueueEntry.id == entry_id)
        ).scalar_one_or_none()
        if not entry:
            abort(404, description="Queue entry not found.")
        if entry.clinic_id != current_user.clinic_id:
            abort(403, description="You can only update queue entries from your clinic.")
        if entry.status != QueueEntry.STATUS_IN_CONSULTATION:
            flash("Only the active consultation can be marked as served.", "warning")
            return redirect(url_for("admin_dashboard"))
        entry.consultation_started_at = to_ist(entry.consultation_started_at) or ist_now()
        entry.status = QueueEntry.STATUS_SERVED
        entry.served_at = ist_now()
        try:
            db.session.flush()
            refresh_clinic_average_consultation_minutes(entry.clinic_id)
            db.session.commit()
            flash(f"Token {entry.token_number} has been marked as served.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("We could not mark that patient as served. Please try again.", "danger")
        return redirect(url_for("admin_dashboard"))


def register_error_handlers(app):
    @app.errorhandler(403)
    def forbidden_page(error):
        return render_template(
            "403.html",
            error_message=getattr(error, "description", "You do not have access to this page."),
        ), 403

    @app.errorhandler(404)
    def not_found_page(error):
        return render_template(
            "404.html",
            error_message=getattr(error, "description", "The page you requested could not be found."),
        ), 404

    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        return render_template(
            "404.html",
            error_message="Something went wrong while loading this page.",
        ), 500


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "login"
    login_manager.login_message = "Please sign in to continue."
    login_manager.login_message_category = "warning"
    register_template_helpers(app)
    register_cli(app)
    register_routes(app)
    register_error_handlers(app)
    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
