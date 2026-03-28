import os
from datetime import datetime

from flask import Flask, render_template, redirect, url_for, flash, abort
from flask_login import LoginManager, login_required, current_user
from sqlalchemy import select, func
from sqlalchemy.orm import joinedload

from config import Config
from models import db, Clinic, User, Patient, QueueEntry


# -----------------------------
# APP SETUP
# -----------------------------
app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


# -----------------------------
# AUTH HELPERS (ASSUMING YOU ALREADY HAVE THIS)
# -----------------------------
def clinic_admin_required(func):
    from functools import wraps

    @wraps(func)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()

        if current_user.role != User.ROLE_CLINIC_ADMIN:
            abort(403)

        return func(*args, **kwargs)

    return wrapper


# -----------------------------
# WAIT TIME LOGIC (FIXED)
# -----------------------------
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
        duration_minutes = max(1, int(duration.total_seconds() // 60))
        durations.append(duration_minutes)

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

    if not durations:
        return 5 * slots_ahead

    avg = round(sum(durations) / len(durations))
    return avg * slots_ahead


# -----------------------------
# CALL NEXT
# -----------------------------
@app.route("/call_next/<int:clinic_id>", methods=["POST"])
@clinic_admin_required
def call_next(clinic_id):
    if clinic_id != current_user.clinic_id:
        abort(403)

    clinic = db.session.get(Clinic, clinic_id)
    if not clinic:
        abort(404)

    active_entry = db.session.execute(
        select(QueueEntry)
        .where(
            QueueEntry.clinic_id == clinic_id,
            QueueEntry.status == QueueEntry.STATUS_IN_CONSULTATION,
        )
        .limit(1)
    ).scalar_one_or_none()

    if active_entry:
        flash("A patient is already in consultation.", "warning")
        return redirect(url_for("admin_dashboard"))

    next_entry = db.session.execute(
        select(QueueEntry)
        .where(
            QueueEntry.clinic_id == clinic_id,
            QueueEntry.status == QueueEntry.STATUS_WAITING,
        )
        .order_by(QueueEntry.token_number.asc())
        .limit(1)
    ).scalar_one_or_none()

    if not next_entry:
        flash("No patients waiting.", "info")
        return redirect(url_for("admin_dashboard"))

    next_entry.status = QueueEntry.STATUS_IN_CONSULTATION
    next_entry.consultation_started_at = datetime.utcnow()

    db.session.commit()

    flash(f"Token {next_entry.token_number} is now in consultation.", "success")
    return redirect(url_for("admin_dashboard"))


# -----------------------------
# MARK SERVED (FIXED)
# -----------------------------
@app.route("/mark_served/<int:entry_id>", methods=["POST"])
@app.route("/complete/<int:entry_id>", methods=["POST"])
@clinic_admin_required
def mark_served(entry_id):

    entry = db.session.execute(
        select(QueueEntry)
        .options(joinedload(QueueEntry.patient))
        .where(QueueEntry.id == entry_id)
    ).scalar_one_or_none()

    if not entry:
        abort(404)

    if entry.clinic_id != current_user.clinic_id:
        abort(403)

    if entry.status != QueueEntry.STATUS_IN_CONSULTATION:
        flash("Only active consultation can be marked served.", "warning")
        return redirect(url_for("admin_dashboard"))

    entry.status = QueueEntry.STATUS_SERVED
    entry.served_at = datetime.utcnow()

    if entry.consultation_started_at is None:
        entry.consultation_started_at = entry.served_at

    db.session.commit()

    flash(f"Token {entry.token_number} marked served.", "success")
    return redirect(url_for("admin_dashboard"))


# -----------------------------
# BOOTSTRAP (ONLY ON DEPLOY)
# -----------------------------
def bootstrap_database():
    db.create_all()


if __name__ != "__main__":
    with app.app_context():
        bootstrap_database()


if __name__ == "__main__":
    app.run(debug=True)