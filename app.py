import logging
import os
from datetime import datetime, timedelta

from flask import Flask, abort, flash, redirect, render_template, request, url_for
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


# ======================
# LOGIN
# ======================

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@login_manager.unauthorized_handler
def unauthorized():
    return redirect(url_for("login"))


# ======================
# ROUTES
# ======================

@app.route("/")
def home():
    clinics = Clinic.query.all()
    return render_template("landing.html", clinics=clinics)


@app.route("/clinics")
def clinics():
    clinics = Clinic.query.all()
    return render_template("clinics.html", clinics=clinics)


@app.route("/clinic/<int:clinic_id>")
def clinic_detail(clinic_id):
    clinic = Clinic.query.get_or_404(clinic_id)

    queue = (
        QueueEntry.query
        .filter_by(clinic_id=clinic_id)
        .order_by(QueueEntry.token_number)
        .all()
    )

    return render_template("clinic.html", clinic=clinic, queue=queue)


@app.route("/join_queue/<int:clinic_id>", methods=["POST"])
def join_queue(clinic_id):
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

    except SQLAlchemyError:
        db.session.rollback()
        flash("Something went wrong", "danger")

    return redirect(url_for("clinic_detail", clinic_id=clinic_id))


# ======================
# ADMIN
# ======================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("admin_dashboard"))

        flash("Invalid credentials", "danger")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("home"))


@app.route("/admin")
@login_required
def admin_dashboard():
    if current_user.role not in [User.ROLE_SUPERADMIN, User.ROLE_CLINIC_ADMIN]:
        abort(403)

    clinic_id = current_user.clinic_id

    queue = (
        QueueEntry.query
        .filter_by(clinic_id=clinic_id)
        .order_by(QueueEntry.token_number)
        .all()
    )

    return render_template("admin_dashboard.html", queue=queue)


@app.route("/call_next", methods=["POST"])
@login_required
def call_next():
    clinic_id = current_user.clinic_id

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


@app.route("/mark_served/<int:entry_id>", methods=["POST"])
@login_required
def mark_served(entry_id):
    entry = QueueEntry.query.get_or_404(entry_id)

    entry.status = QueueEntry.STATUS_SERVED
    entry.served_at = ist_now()

    db.session.commit()

    return redirect(url_for("admin_dashboard"))


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

@app.before_first_request
def setup():
    db.create_all()


# ======================
# RUN
# ======================

if __name__ == "__main__":
    app.run(debug=True)