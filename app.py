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
    return redirect(url_for("login", next=request.full_path))


def is_safe_redirect_target(target):
    if not target:
        return False

    parsed_target = urlsplit(target)
    return not parsed_target.netloc and parsed_target.path.startswith("/")


def redirect_after_login():
    next_url = request.args.get("next") or request.form.get("next")
    if is_safe_redirect_target(next_url):
        return redirect(next_url)

    if current_user.role in {User.ROLE_SUPERADMIN, User.ROLE_CLINIC_ADMIN}:
        return redirect(url_for("admin_dashboard"))

    return redirect(url_for("home"))


def superadmin_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped_view(*args, **kwargs):
        if current_user.role != User.ROLE_SUPERADMIN:
            abort(403)
        return view_func(*args, **kwargs)

    return wrapped_view


def clinic_admin_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped_view(*args, **kwargs):
        if current_user.role != User.ROLE_CLINIC_ADMIN or current_user.clinic_id is None:
            abort(403)
        return view_func(*args, **kwargs)

    return wrapped_view


def admin_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped_view(*args, **kwargs):
        if current_user.role not in {User.ROLE_SUPERADMIN, User.ROLE_CLINIC_ADMIN}:
            abort(403)
        return view_func(*args, **kwargs)

    return wrapped_view


def normalize_phone(phone_value):
    return "".join(character for character in (phone_value or "") if character.isdigit())


def upgrade_legacy_password_if_needed(user, raw_password):
    if user.password_uses_hash():
        return

    if user.password == raw_password:
        user.set_password(raw_password)
        db.session.commit()


def verify_password(user, raw_password):
    if user is None or not raw_password:
        return False

    is_valid = user.check_password(raw_password)
    if is_valid:
        upgrade_legacy_password_if_needed(user, raw_password)

    return is_valid


def seed_default_clinics():
    if Clinic.query.first():
        return

    clinics = [
        Clinic(clinic_name="City Clinic", doctor_name="Dr. Sharma"),
        Clinic(clinic_name="HealthCare Plus", doctor_name="Dr. Mehta"),
        Clinic(clinic_name="Wellness Center", doctor_name="Dr. Rao"),
    ]
    db.session.add_all(clinics)
    db.session.commit()


def seed_initial_superadmin():
    username = (os.getenv("DEFAULT_SUPERADMIN_USERNAME") or "").strip()
    password = os.getenv("DEFAULT_SUPERADMIN_PASSWORD") or ""

    if not username or not password:
        return

    existing_superadmin = User.query.filter_by(role=User.ROLE_SUPERADMIN).first()
    username_taken = User.query.filter(
        func.lower(User.username) == username.lower()
    ).first()

    if existing_superadmin or username_taken:
        return

    superadmin = User(username=username, role=User.ROLE_SUPERADMIN)
    superadmin.set_password(password)
    db.session.add(superadmin)
    db.session.commit()


def repair_queue_entries():
    entries = (
        QueueEntry.query.order_by(
            QueueEntry.clinic_id.asc(),
            QueueEntry.token_number.asc(),
            QueueEntry.id.asc(),
        ).all()
    )

    repaired = False
    active_clinics = set()
    now = datetime.utcnow()

    for entry in entries:
        if entry.status not in QueueEntry.VALID_STATUSES:
            entry.status = QueueEntry.STATUS_WAITING
            entry.consultation_started_at = None
            entry.served_at = None
            repaired = True

        if entry.status == QueueEntry.STATUS_WAITING:
            if entry.consultation_started_at is not None or entry.served_at is not None:
                entry.consultation_started_at = None
                entry.served_at = None
                repaired = True
            continue

        if entry.status == QueueEntry.STATUS_IN_CONSULTATION:
            if entry.clinic_id in active_clinics:
                entry.status = QueueEntry.STATUS_WAITING
                entry.consultation_started_at = None
                entry.served_at = None
                repaired = True
                continue

            active_clinics.add(entry.clinic_id)

            if entry.consultation_started_at is None:
                entry.consultation_started_at = entry.joined_at or now
                repaired = True

            if entry.served_at is not None:
                entry.served_at = None
                repaired = True

            continue

        if entry.status == QueueEntry.STATUS_SERVED and entry.served_at is None:
            entry.served_at = entry.consultation_started_at or entry.joined_at or now
            repaired = True

    if repaired:
        db.session.commit()


def ensure_queue_schema():
    inspector = inspect(db.engine)
    if "queue_entries" not in set(inspector.get_table_names()):
        return

    queue_columns = {column["name"] for column in inspector.get_columns("queue_entries")}

    if "consultation_started_at" not in queue_columns:
        db.session.execute(
            text(
                "ALTER TABLE queue_entries "
                "ADD COLUMN consultation_started_at TIMESTAMP NULL"
            )
        )
        db.session.commit()

    repair_queue_entries()

    inspector = inspect(db.engine)
    queue_indexes = {index["name"] for index in inspector.get_indexes("queue_entries")}

    if "unique_active_queue_entry_per_clinic" not in queue_indexes:
        try:
            db.session.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "unique_active_queue_entry_per_clinic "
                    "ON queue_entries (clinic_id) "
                    "WHERE status = 'in_consultation'"
                )
            )
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            app.logger.warning(
                "Could not create the active queue index automatically: %s",
                exc,
            )


def bootstrap_database():
    db.create_all()
    ensure_queue_schema()
    seed_initial_superadmin()


def build_queue_snapshot(entry):
    active_entry = (
        QueueEntry.query.filter_by(
            clinic_id=entry.clinic_id,
            status=QueueEntry.STATUS_IN_CONSULTATION,
        )
        .order_by(QueueEntry.token_number.asc(), QueueEntry.id.asc())
        .first()
    )

    next_waiting = (
        QueueEntry.query.filter_by(
            clinic_id=entry.clinic_id,
            status=QueueEntry.STATUS_WAITING,
        )
        .order_by(QueueEntry.token_number.asc(), QueueEntry.id.asc())
        .first()
    )

    if active_entry is not None:
        now_serving_token = active_entry.token_number
    else:
        now_serving_token = next_waiting.token_number if next_waiting else None

    patients_ahead = (
        QueueEntry.query.filter(
            QueueEntry.clinic_id == entry.clinic_id,
            QueueEntry.status.in_(
                [
                    QueueEntry.STATUS_WAITING,
                    QueueEntry.STATUS_IN_CONSULTATION,
                ]
            ),
            QueueEntry.token_number < entry.token_number,
        ).count()
    )

    completed_entries = (
        QueueEntry.query.filter(
            QueueEntry.clinic_id == entry.clinic_id,
            QueueEntry.status == QueueEntry.STATUS_SERVED,
            QueueEntry.served_at.isnot(None),
        )
        .order_by(QueueEntry.served_at.desc())
        .all()
    )

    durations = []
    for completed_entry in completed_entries:
        start_time = completed_entry.consultation_started_at or completed_entry.joined_at
        if start_time and completed_entry.served_at >= start_time:
            durations.append(
                (completed_entry.served_at - start_time).total_seconds() / 60
            )

    average_consultation_time = (
        sum(durations) / len(durations) if durations else None
    )
    estimated_wait = None

    if average_consultation_time is not None:
        wait_time = patients_ahead * average_consultation_time

        if active_entry is not None and active_entry.token_number < entry.token_number:
            active_start_time = (
                active_entry.consultation_started_at or active_entry.joined_at
            )
            if active_start_time:
                elapsed_minutes = (
                    datetime.utcnow() - active_start_time
                ).total_seconds() / 60
                wait_time += max(average_consultation_time - elapsed_minutes, 0)

        estimated_wait = round(wait_time, 2)

    return {
        "entry_status": entry.status,
        "status_label": entry.status.replace("_", " ").title(),
        "now_serving": now_serving_token,
        "patients_ahead": patients_ahead,
        "estimated_wait": estimated_wait,
    }


def build_landing_context():
    return {
        "clinic_count": Clinic.query.count(),
        "waiting_count": QueueEntry.query.filter_by(
            status=QueueEntry.STATUS_WAITING
        ).count(),
        "active_count": QueueEntry.query.filter_by(
            status=QueueEntry.STATUS_IN_CONSULTATION
        ).count(),
    }


def build_clinics_context():
    clinics = Clinic.query.order_by(Clinic.clinic_name.asc()).all()
    return {
        "clinics": clinics,
        "clinic_count": len(clinics),
    }


def build_clinic_page_context(clinic):
    active_entry = (
        QueueEntry.query.filter_by(
            clinic_id=clinic.id,
            status=QueueEntry.STATUS_IN_CONSULTATION,
        )
        .order_by(QueueEntry.token_number.asc(), QueueEntry.id.asc())
        .first()
    )

    waiting_count = QueueEntry.query.filter_by(
        clinic_id=clinic.id,
        status=QueueEntry.STATUS_WAITING,
    ).count()

    return {
        "clinic": clinic,
        "active_entry": active_entry,
        "waiting_count": waiting_count,
    }


def build_clinic_admin_dashboard_context(clinic_id):
    clinic = db.session.get(Clinic, clinic_id)
    if clinic is None:
        abort(404)

    entries = (
        QueueEntry.query.options(joinedload(QueueEntry.patient))
        .filter_by(clinic_id=clinic_id)
        .order_by(QueueEntry.token_number.asc(), QueueEntry.id.asc())
        .all()
    )

    active_entry = next(
        (
            entry
            for entry in entries
            if entry.status == QueueEntry.STATUS_IN_CONSULTATION
        ),
        None,
    )

    waiting_count = sum(
        1 for entry in entries if entry.status == QueueEntry.STATUS_WAITING
    )
    served_count = sum(
        1 for entry in entries if entry.status == QueueEntry.STATUS_SERVED
    )

    return {
        "dashboard_role": "clinic_admin",
        "clinic": clinic,
        "entries": entries,
        "active_entry": active_entry,
        "waiting_count": waiting_count,
        "served_count": served_count,
        "can_call_next": waiting_count > 0 and active_entry is None,
    }


def build_superadmin_dashboard_context():
    clinics = Clinic.query.options(joinedload(Clinic.users)).order_by(
        Clinic.clinic_name.asc()
    ).all()

    queue_stats = {
        row.clinic_id: row
        for row in db.session.query(
            QueueEntry.clinic_id.label("clinic_id"),
            func.count(QueueEntry.id).label("total_entries"),
            func.sum(
                case(
                    (QueueEntry.status == QueueEntry.STATUS_WAITING, 1),
                    else_=0,
                )
            ).label("waiting_count"),
            func.sum(
                case(
                    (QueueEntry.status == QueueEntry.STATUS_IN_CONSULTATION, 1),
                    else_=0,
                )
            ).label("active_count"),
        )
        .group_by(QueueEntry.clinic_id)
        .all()
    }

    active_tokens = {
        entry.clinic_id: entry.token_number
        for entry in QueueEntry.query.filter_by(
            status=QueueEntry.STATUS_IN_CONSULTATION
        ).all()
    }

    clinic_rows = []
    for clinic in clinics:
        clinic_queue_stats = queue_stats.get(clinic.id)
        clinic_rows.append(
            {
                "clinic": clinic,
                "admins": [
                    user
                    for user in clinic.users
                    if user.role == User.ROLE_CLINIC_ADMIN
                ],
                "waiting_count": (
                    clinic_queue_stats.waiting_count if clinic_queue_stats else 0
                ),
                "total_entries": (
                    clinic_queue_stats.total_entries if clinic_queue_stats else 0
                ),
                "has_active_consultation": bool(
                    clinic_queue_stats and clinic_queue_stats.active_count
                ),
                "active_token": active_tokens.get(clinic.id),
            }
        )

    admin_users = (
        User.query.options(joinedload(User.clinic))
        .filter_by(role=User.ROLE_CLINIC_ADMIN)
        .order_by(User.username.asc())
        .all()
    )

    return {
        "dashboard_role": "superadmin",
        "clinic_rows": clinic_rows,
        "admin_users": admin_users,
        "total_clinics": len(clinics),
        "total_admins": len(admin_users),
        "total_waiting": sum(row["waiting_count"] for row in clinic_rows),
    }


def build_assign_admin_context():
    clinics = Clinic.query.order_by(Clinic.clinic_name.asc()).all()
    admin_users = (
        User.query.options(joinedload(User.clinic))
        .filter_by(role=User.ROLE_CLINIC_ADMIN)
        .order_by(User.username.asc())
        .all()
    )

    return {
        "clinics": clinics,
        "admin_users": admin_users,
    }


def create_queue_entry(clinic_id, patient_name, phone_number):
    while True:
        patient = Patient(name=patient_name, phone=phone_number)
        db.session.add(patient)
        db.session.flush()

        try:
            last_token = (
                db.session.query(func.max(QueueEntry.token_number))
                .filter_by(clinic_id=clinic_id)
                .scalar()
            )
            next_token = 1 if last_token is None else last_token + 1

            entry = QueueEntry(
                clinic_id=clinic_id,
                patient_id=patient.id,
                token_number=next_token,
                status=QueueEntry.STATUS_WAITING,
            )
            db.session.add(entry)
            db.session.commit()
            return entry
        except IntegrityError:
            db.session.rollback()


@app.context_processor
def inject_navigation_state():
    return {
        "is_superadmin": current_user.is_authenticated
        and current_user.role == User.ROLE_SUPERADMIN,
        "is_clinic_admin": current_user.is_authenticated
        and current_user.role == User.ROLE_CLINIC_ADMIN,
    }


@app.cli.command("init-db")
def init_db():
    bootstrap_database()
    seed_default_clinics()
    print("Database initialized.")


@app.route("/init-db")
def init_db_route():
    bootstrap_database()
    seed_default_clinics()
    return "Database initialized!"


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
    if clinic is None:
        abort(404)

    return render_template("clinic.html", **build_clinic_page_context(clinic))


@app.route("/join_queue", methods=["POST"])
def join_queue():
    clinic_id_raw = (request.form.get("clinic_id") or "").strip()
    patient_name = (request.form.get("name") or "").strip()
    phone_number = normalize_phone(request.form.get("phone"))

    try:
        clinic_id = int(clinic_id_raw)
    except (TypeError, ValueError):
        flash("Please choose a valid clinic.", "danger")
        return redirect(url_for("clinics_list"))

    clinic = db.session.get(Clinic, clinic_id)
    if clinic is None:
        abort(404)

    if not patient_name:
        flash("Please enter your name to join the queue.", "danger")
        return redirect(url_for("clinic_page", clinic_id=clinic_id))

    if len(phone_number) != 10:
        flash("Please enter a valid 10-digit phone number.", "danger")
        return redirect(url_for("clinic_page", clinic_id=clinic_id))

    entry = create_queue_entry(clinic_id, patient_name, phone_number)
    flash(
        f"You're in the queue for {clinic.clinic_name}. Your token is {entry.token_number}.",
        "success",
    )
    return redirect(url_for("queue_status", entry_id=entry.id))


@app.route("/queue/<int:entry_id>")
def queue_status(entry_id):
    entry = db.session.get(QueueEntry, entry_id)
    if entry is None:
        abort(404)

    queue_snapshot = build_queue_snapshot(entry)

    return render_template(
        "queue.html",
        entry=entry,
        clinic=entry.clinic,
        **queue_snapshot,
    )


@app.route("/api/queue_status/<int:entry_id>")
def queue_status_api(entry_id):
    entry = db.session.get(QueueEntry, entry_id)
    if entry is None:
        abort(404)

    return jsonify(build_queue_snapshot(entry))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect_after_login()

    next_url = request.args.get("next") or request.form.get("next") or ""

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if not username or not password:
            return render_template(
                "login.html",
                error_message="Username and password are required.",
                next_url=next_url,
            ), 400

        user = User.query.filter(
            func.lower(User.username) == username.lower()
        ).first()

        if not verify_password(user, password):
            return render_template(
                "login.html",
                error_message="Invalid username or password.",
                next_url=next_url,
            ), 403

        login_user(user)
        flash(f"Welcome back, {user.username}.", "success")
        return redirect_after_login()

    return render_template("login.html", error_message=None, next_url=next_url)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("You've been logged out.", "info")
    return redirect(url_for("home"))


@app.route("/admin")
@admin_required
def admin_dashboard_legacy():
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    if current_user.role == User.ROLE_SUPERADMIN:
        return render_template(
            "admin_dashboard.html",
            **build_superadmin_dashboard_context(),
        )

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
            flash("Clinic name and doctor name are required.", "danger")
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
    clinics = Clinic.query.order_by(Clinic.clinic_name.asc()).all()

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        clinic_id_raw = (request.form.get("clinic_id") or "").strip()

        try:
            clinic_id = int(clinic_id_raw)
        except (TypeError, ValueError):
            flash("Please choose a valid clinic.", "danger")
            return render_template("assign_admin.html", **build_assign_admin_context())

        clinic = db.session.get(Clinic, clinic_id)
        if clinic is None:
            flash("The selected clinic does not exist.", "danger")
            return render_template("assign_admin.html", **build_assign_admin_context())

        if not username:
            flash("Username is required.", "danger")
            return render_template("assign_admin.html", **build_assign_admin_context())

        if len(password) < 8:
            flash("Password must be at least 8 characters long.", "danger")
            return render_template("assign_admin.html", **build_assign_admin_context())

        existing_user = User.query.filter(
            func.lower(User.username) == username.lower()
        ).first()
        if existing_user is not None:
            flash("That username is already in use.", "danger")
            return render_template("assign_admin.html", **build_assign_admin_context())

        clinic_admin = User(
            username=username,
            role=User.ROLE_CLINIC_ADMIN,
            clinic_id=clinic.id,
        )
        clinic_admin.set_password(password)
        db.session.add(clinic_admin)
        db.session.commit()

        flash(
            f"{username} has been assigned to {clinic.clinic_name}.",
            "success",
        )
        return redirect(url_for("admin_dashboard"))

    if not clinics:
        flash("Add a clinic before assigning clinic admins.", "warning")
        return redirect(url_for("add_clinic"))

    return render_template("assign_admin.html", **build_assign_admin_context())


@app.route("/call_next/<int:clinic_id>", methods=["POST"])
@clinic_admin_required
def call_next(clinic_id):
    if clinic_id != current_user.clinic_id:
        abort(403)

    try:
        clinic = db.session.execute(
            select(Clinic).where(Clinic.id == clinic_id).with_for_update()
        ).scalar_one_or_none()

        if clinic is None:
            db.session.rollback()
            abort(404)

        active_entry = db.session.execute(
            select(QueueEntry)
            .where(
                QueueEntry.clinic_id == clinic_id,
                QueueEntry.status == QueueEntry.STATUS_IN_CONSULTATION,
            )
            .order_by(QueueEntry.token_number.asc(), QueueEntry.id.asc())
            .with_for_update()
        ).scalars().first()

        if active_entry is not None:
            db.session.rollback()
            flash("A patient is already in consultation.", "warning")
            return redirect(url_for("admin_dashboard"))

        next_entry = db.session.execute(
            select(QueueEntry)
            .where(
                QueueEntry.clinic_id == clinic_id,
                QueueEntry.status == QueueEntry.STATUS_WAITING,
            )
            .order_by(QueueEntry.token_number.asc(), QueueEntry.id.asc())
            .with_for_update()
        ).scalars().first()

        if next_entry is None:
            db.session.rollback()
            flash("No waiting patients are left in the queue.", "info")
            return redirect(url_for("admin_dashboard"))

        next_entry.status = QueueEntry.STATUS_IN_CONSULTATION
        next_entry.consultation_started_at = datetime.utcnow()
        next_entry.served_at = None

        db.session.flush()
        db.session.commit()

        flash(
            f"Token {next_entry.token_number} is now in consultation.",
            "success",
        )
    except IntegrityError:
        db.session.rollback()
        flash(
            "Unable to call the next patient right now. Please try again.",
            "danger",
        )

    return redirect(url_for("admin_dashboard"))


@app.route("/mark_served/<int:entry_id>", methods=["POST"])
@app.route("/complete/<int:entry_id>", methods=["POST"])
@clinic_admin_required
def mark_served(entry_id):
    try:
        entry = db.session.execute(
            select(QueueEntry)
            .where(QueueEntry.id == entry_id)
            .with_for_update()
        ).scalars().first()

        if entry is None:
            db.session.rollback()
            abort(404)

        if entry.clinic_id != current_user.clinic_id:
            db.session.rollback()
            abort(403)

        if entry.status != QueueEntry.STATUS_IN_CONSULTATION:
            db.session.rollback()
            flash(
                "Only the patient currently in consultation can be marked as served.",
                "warning",
            )
            return redirect(url_for("admin_dashboard"))

        entry.status = QueueEntry.STATUS_SERVED
        entry.served_at = datetime.utcnow()

        if entry.consultation_started_at is None:
            entry.consultation_started_at = entry.joined_at

        db.session.flush()
        db.session.commit()

        flash(f"Token {entry.token_number} marked as served.", "success")
    except IntegrityError:
        db.session.rollback()
        flash(
            "Unable to update the queue right now. Please try again.",
            "danger",
        )

    return redirect(url_for("admin_dashboard"))


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


with app.app_context():
    bootstrap_database()


if __name__ == "__main__":
    app.run(debug=True)
