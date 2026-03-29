import logging
import os
import traceback
from collections import defaultdict
from datetime import timedelta, datetime
from functools import wraps
from urllib.parse import urlsplit

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from sqlalchemy import case, func, inspect, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import joinedload

from config import Config
from models import Clinic, IST, Patient, QueueEntry, User, db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

login_manager = LoginManager()


def utc_now():
    return datetime.utcnow()


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
    if minutes is None or minutes <= 0:
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


def normalize_phone(raw_phone):
    digits = "".join(char for char in (raw_phone or "") if char.isdigit())
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits


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


def log_database_exception(message):
    db.session.rollback()
    logger.exception(message)


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


def verify_password(user, raw_password):
    if not user or not raw_password:
        return False

    password_valid = user.check_password(raw_password)
    if password_valid and not user.password_uses_hash():
        user.set_password(raw_password)
        db.session.commit()

    return password_valid


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
                entry.served_at = (
                    to_ist(entry.consultation_started_at)
                    or to_ist(entry.joined_at)
                    or to_ist(utc_now())
                )

            if entry.consultation_started_at is None:
                joined_at = to_ist(entry.joined_at)
                served_at = to_ist(entry.served_at)
                fallback_start = served_at - timedelta(
                    minutes=Config.DEFAULT_CONSULTATION_TIME
                )
                entry.consultation_started_at = (
                    joined_at if joined_at and joined_at < served_at else fallback_start
                )

    db.session.commit()