import os
from collections import defaultdict
from datetime import timedelta
from functools import wraps
from urllib.parse import urlsplit

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from sqlalchemy import case, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from config import Config
from models import Clinic, IST, Patient, QueueEntry, User, db, ist_now

login_manager = LoginManager()


# -----------------------------
# HELPERS (UNCHANGED)
# -----------------------------
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


# -----------------------------
# AUTH
# -----------------------------
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


# -----------------------------
# ROUTES
# -----------------------------
def register_routes(app):

    @app.route("/")
    def home():
        if current_user.is_authenticated and current_user.role in {
            User.ROLE_SUPERADMIN,
            User.ROLE_CLINIC_ADMIN,
        }:
            return redirect(url_for("admin_dashboard"))

        return render_template("landing.html", **build_landing_context())

    # ✅ FIXED ROUTE (THIS IS WHAT YOU NEEDED)
    @app.route("/clinics")
    def clinics():
        clinics = db.session.execute(
            select(Clinic).order_by(Clinic.clinic_name.asc())
        ).scalars().all()

        return render_template("clinics.html", clinics=clinics)

    @app.route("/clinic/<int:clinic_id>")
    def clinic_page(clinic_id):
        clinic = db.session.get(Clinic, clinic_id)
        if not clinic:
            abort(404)

        return render_template("clinic.html", clinic=clinic)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("home"))

        if request.method == "POST":
            username = request.form.get("username")
            password = request.form.get("password")

            user = db.session.scalar(
                select(User).where(User.username == username)
            )

            if not user or not user.check_password(password):
                flash("Invalid credentials", "danger")
                return render_template("login.html")

            login_user(user)
            return redirect(url_for("home"))

        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("home"))


# -----------------------------
# APP FACTORY
# -----------------------------
def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)

    register_routes(app)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)