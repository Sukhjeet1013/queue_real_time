import logging
import os
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError

from config import Config
from models import Clinic, IST, Patient, QueueEntry, User, db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


# ======================
# UTILS
# ======================

def ist_now():
    return datetime.now(IST)


def to_ist(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


@app.template_filter("format_ist")
def format_ist(value):
    localized_value = to_ist(value)
    if not localized_value:
        return "-"
    return localized_value.strftime("%d %b %Y, %I:%M %p")


@app.context_processor
def inject_navigation_state():
    return {
        "is_superadmin": current_user.is_authenticated and current_user.role == User.ROLE_SUPERADMIN,
        "is_clinic_admin": current_user.is_authenticated and current_user.role == User.ROLE_CLINIC_ADMIN,
    }


# ======================
# LOGIN
# ======================

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@login_manager.unauthorized_handler
def unauthorized():
    return redirect(url_for("login"))


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)

        if current_user.role not in [User.ROLE_SUPERADMIN, User.ROLE_CLINIC_ADMIN]:
            abort(403)

        return view_func(*args, **kwargs)

    return wrapped


# ======================
# ROUTES
# ======================

@app.route("/")
def home():
    clinics = Clinic.query.order_by(Clinic.clinic_name).all()
    clinic_count = len(clinics)
    waiting_count = QueueEntry.query.filter_by(status=QueueEntry.STATUS_WAITING).count()
    active_count = QueueEntry.query.filter_by(
        status=QueueEntry.STATUS_IN_CONSULTATION
    ).count()
    return render_template(
        "landing.html",
        clinics=clinics,
        clinic_count=clinic_count,
        waiting_count=waiting_count,
        active_count=active_count,
    )


@app.route("/clinics")
def clinics():
    clinics = Clinic.query.order_by(Clinic.clinic_name).all()
    return render_template("clinics.html", clinics=clinics, clinic_count=len(clinics))


@app.route("/clinic/<int:clinic_id>")
def clinic_detail(clinic_id):
    clinic = Clinic.query.get_or_404(clinic_id)
    active_entry = (
        QueueEntry.query
        .filter_by(
            clinic_id=clinic_id,
            status=QueueEntry.STATUS_IN_CONSULTATION,
        )
        .order_by(QueueEntry.token_number)
        .first()
    )
    waiting_count = QueueEntry.query.filter_by(
        clinic_id=clinic_id,
        status=QueueEntry.STATUS_WAITING,
    ).count()

    return render_template(
        "clinic.html",
        clinic=clinic,
        active_entry=active_entry,
        waiting_count=waiting_count,
    )


@app.route("/join_queue", methods=["POST"])
@app.route("/join_queue/<int:clinic_id>", methods=["POST"])
def join_queue(clinic_id=None):
    if clinic_id is None:
        clinic_id = request.form.get("clinic_id", type=int)

    clinic = Clinic.query.get_or_404(clinic_id)
    name = request.form.get("name")
    phone = request.form.get("phone")

    if not name or not phone:
        flash("Name and phone required", "danger")
        return redirect(url_for("clinic_detail", clinic_id=clinic_id))

    try:
        last_token = db.session.query(func.max(QueueEntry.token_number)).filter_by(clinic_id=clinic_id).scalar()
        next_token = (last_token or 0) + 1

        patient = Patient(name=name, phone=phone)
        db.session.add(patient)
        db.session.flush()

        entry = QueueEntry(
            clinic_id=clinic_id,
            patient_id=patient.id,
            token_number=next_token,
            status=QueueEntry.STATUS_WAITING,
            joined_at=ist_now()
        )

        db.session.add(entry)
        db.session.commit()

        flash(f"Joined queue. Your token: {next_token}", "success")
        return redirect(url_for("queue_status", entry_id=entry.id))

    except SQLAlchemyError:
        db.session.rollback()
        flash("Something went wrong", "danger")

    return redirect(url_for("clinic_detail", clinic_id=clinic_id))


# ======================
# ADMIN
# ======================

@app.route("/login", methods=["GET", "POST"])
def login():
    error_message = None
    next_url = request.args.get("next", "")

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        next_url = request.form.get("next", "")

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user)
            if next_url:
                return redirect(next_url)
            return redirect(url_for("admin_dashboard"))

        error_message = "Invalid credentials"
        flash("Invalid credentials", "danger")

    return render_template("login.html", error_message=error_message, next_url=next_url)


@app.route("/logout", methods=["GET", "POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("home"))


@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    if current_user.role == User.ROLE_SUPERADMIN:
        clinics = Clinic.query.order_by(Clinic.clinic_name).all()
        admin_users = User.query.filter_by(role=User.ROLE_CLINIC_ADMIN).all()
        clinic_rows = []
        total_waiting = 0

        for clinic in clinics:
            clinic_entries = (
                QueueEntry.query
                .filter_by(clinic_id=clinic.id)
                .order_by(QueueEntry.token_number)
                .all()
            )
            waiting_count = sum(
                1 for entry in clinic_entries if entry.status == QueueEntry.STATUS_WAITING
            )
            active_entry = next(
                (
                    entry
                    for entry in clinic_entries
                    if entry.status == QueueEntry.STATUS_IN_CONSULTATION
                ),
                None,
            )
            total_waiting += waiting_count
            clinic_rows.append(
                {
                    "clinic": clinic,
                    "admins": [user for user in admin_users if user.clinic_id == clinic.id],
                    "waiting_count": waiting_count,
                    "total_entries": len(clinic_entries),
                    "active_token": active_entry.token_number if active_entry else None,
                }
            )

        return render_template(
            "admin_dashboard.html",
            dashboard_role="superadmin",
            clinic_rows=clinic_rows,
            admin_users=admin_users,
            total_clinics=len(clinics),
            total_admins=len(admin_users),
            total_waiting=total_waiting,
        )

    clinic_id = current_user.clinic_id
    clinic = Clinic.query.get_or_404(clinic_id)
    entries = (
        QueueEntry.query
        .filter_by(clinic_id=clinic_id)
        .order_by(QueueEntry.token_number)
        .all()
    )
    active_entry = next(
        (entry for entry in entries if entry.status == QueueEntry.STATUS_IN_CONSULTATION),
        None,
    )
    waiting_count = sum(1 for entry in entries if entry.status == QueueEntry.STATUS_WAITING)
    served_count = sum(1 for entry in entries if entry.status == QueueEntry.STATUS_SERVED)

    return render_template(
        "admin_dashboard.html",
        dashboard_role="clinic_admin",
        clinic=clinic,
        entries=entries,
        active_entry=active_entry,
        waiting_count=waiting_count,
        served_count=served_count,
        can_call_next=active_entry is None and waiting_count > 0,
    )


@app.route("/call_next", methods=["POST"])
@app.route("/call_next/<int:clinic_id>", methods=["POST"])
@login_required
@admin_required
def call_next(clinic_id=None):
    clinic_id = clinic_id or current_user.clinic_id

    current = QueueEntry.query.filter_by(
        clinic_id=clinic_id,
        status=QueueEntry.STATUS_IN_CONSULTATION
    ).first()

    if current:
        current.status = QueueEntry.STATUS_SERVED
        current.served_at = ist_now()

    next_entry = QueueEntry.query.filter_by(
        clinic_id=clinic_id,
        status=QueueEntry.STATUS_WAITING
    ).order_by(QueueEntry.token_number).first()

    if next_entry:
        next_entry.status = QueueEntry.STATUS_IN_CONSULTATION
        next_entry.consultation_started_at = ist_now()

    db.session.commit()

    return redirect(url_for("admin_dashboard"))


@app.route("/queue/<int:entry_id>")
def queue_status(entry_id):
    entry = QueueEntry.query.get_or_404(entry_id)
    clinic = Clinic.query.get_or_404(entry.clinic_id)

    active_entry = (
        QueueEntry.query
        .filter_by(
            clinic_id=entry.clinic_id,
            status=QueueEntry.STATUS_IN_CONSULTATION,
        )
        .order_by(QueueEntry.token_number)
        .first()
    )
    patients_ahead = QueueEntry.query.filter(
        QueueEntry.clinic_id == entry.clinic_id,
        QueueEntry.status == QueueEntry.STATUS_WAITING,
        QueueEntry.token_number < entry.token_number,
    ).count()
    if active_entry and active_entry.token_number < entry.token_number:
        patients_ahead += 1

    status_label_map = {
        QueueEntry.STATUS_WAITING: "Waiting",
        QueueEntry.STATUS_IN_CONSULTATION: "In Consultation",
        QueueEntry.STATUS_SERVED: "Served",
    }

    recent_served = (
        QueueEntry.query
        .filter(
            QueueEntry.clinic_id == entry.clinic_id,
            QueueEntry.status == QueueEntry.STATUS_SERVED,
            QueueEntry.consultation_started_at.isnot(None),
            QueueEntry.served_at.isnot(None),
        )
        .order_by(QueueEntry.served_at.desc())
        .limit(10)
        .all()
    )

    durations = []
    for served_entry in recent_served:
        duration = served_entry.served_at - served_entry.consultation_started_at
        minutes = max(1, round(duration.total_seconds() / 60))
        durations.append(minutes)

    average_minutes = round(sum(durations) / len(durations)) if durations else 5
    estimated_wait = None
    if entry.status == QueueEntry.STATUS_IN_CONSULTATION:
        estimated_wait = 0
    elif entry.status == QueueEntry.STATUS_WAITING:
        estimated_wait = average_minutes * max(1, patients_ahead)

    return render_template(
        "queue.html",
        entry=entry,
        clinic=clinic,
        entry_status=entry.status,
        status_label=status_label_map.get(entry.status, "Unknown"),
        now_serving=active_entry.token_number if active_entry else None,
        patients_ahead=patients_ahead,
        estimated_wait=estimated_wait,
    )


@app.route("/api/queue_status/<int:entry_id>")
def queue_status_api(entry_id):
    entry = QueueEntry.query.get_or_404(entry_id)
    active_entry = (
        QueueEntry.query
        .filter_by(
            clinic_id=entry.clinic_id,
            status=QueueEntry.STATUS_IN_CONSULTATION,
        )
        .order_by(QueueEntry.token_number)
        .first()
    )
    patients_ahead = QueueEntry.query.filter(
        QueueEntry.clinic_id == entry.clinic_id,
        QueueEntry.status == QueueEntry.STATUS_WAITING,
        QueueEntry.token_number < entry.token_number,
    ).count()
    if active_entry and active_entry.token_number < entry.token_number:
        patients_ahead += 1

    status_label_map = {
        QueueEntry.STATUS_WAITING: "Waiting",
        QueueEntry.STATUS_IN_CONSULTATION: "In Consultation",
        QueueEntry.STATUS_SERVED: "Served",
    }

    recent_served = (
        QueueEntry.query
        .filter(
            QueueEntry.clinic_id == entry.clinic_id,
            QueueEntry.status == QueueEntry.STATUS_SERVED,
            QueueEntry.consultation_started_at.isnot(None),
            QueueEntry.served_at.isnot(None),
        )
        .order_by(QueueEntry.served_at.desc())
        .limit(10)
        .all()
    )

    durations = []
    for served_entry in recent_served:
        duration = served_entry.served_at - served_entry.consultation_started_at
        minutes = max(1, round(duration.total_seconds() / 60))
        durations.append(minutes)

    average_minutes = round(sum(durations) / len(durations)) if durations else 5
    estimated_wait = None
    if entry.status == QueueEntry.STATUS_IN_CONSULTATION:
        estimated_wait = 0
    elif entry.status == QueueEntry.STATUS_WAITING:
        estimated_wait = average_minutes * max(1, patients_ahead)

    return jsonify(
        {
            "entry_status": entry.status,
            "status_label": status_label_map.get(entry.status, "Unknown"),
            "now_serving": active_entry.token_number if active_entry else None,
            "patients_ahead": patients_ahead,
            "estimated_wait": estimated_wait,
        }
    )


@app.route("/mark_served/<int:entry_id>", methods=["POST"])
@login_required
@admin_required
def mark_served(entry_id):
    entry = QueueEntry.query.get_or_404(entry_id)

    entry.status = QueueEntry.STATUS_SERVED
    entry.served_at = ist_now()

    db.session.commit()

    return redirect(url_for("admin_dashboard"))


@app.route("/add_clinic", methods=["GET", "POST"])
@login_required
@admin_required
def add_clinic():
    if current_user.role != User.ROLE_SUPERADMIN:
        abort(403)

    if request.method == "POST":
        clinic_name = request.form.get("clinic_name")
        doctor_name = request.form.get("doctor_name")

        if not clinic_name or not doctor_name:
            flash("Clinic name and doctor name are required", "danger")
            return render_template("add_clinic.html")

        clinic = Clinic(clinic_name=clinic_name, doctor_name=doctor_name)
        db.session.add(clinic)
        db.session.commit()
        flash("Clinic created successfully", "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("add_clinic.html")


@app.route("/assign_admin", methods=["GET", "POST"])
@login_required
@admin_required
def assign_admin():
    if current_user.role != User.ROLE_SUPERADMIN:
        abort(403)

    clinics = Clinic.query.order_by(Clinic.clinic_name).all()
    admin_users = User.query.filter_by(role=User.ROLE_CLINIC_ADMIN).all()

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        clinic_id = request.form.get("clinic_id", type=int)

        if not username or not password or not clinic_id:
            flash("Username, password, and clinic are required", "danger")
            return render_template("assign_admin.html", clinics=clinics, admin_users=admin_users)

        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash("Username already exists", "danger")
            return render_template("assign_admin.html", clinics=clinics, admin_users=admin_users)

        user = User(
            username=username,
            role=User.ROLE_CLINIC_ADMIN,
            clinic_id=clinic_id,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash("Clinic admin assigned successfully", "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("assign_admin.html", clinics=clinics, admin_users=admin_users)


# ======================
# ERRORS
# ======================

@app.errorhandler(403)
def forbidden(e):
    return render_template("403.html", error_message=str(e)), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


# ======================
# DB INIT
# ======================

with app.app_context():
    db.create_all()

# ======================
# RUN
# ======================

if __name__ == "__main__":
    app.run(debug=True)
