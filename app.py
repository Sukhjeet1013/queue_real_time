from flask import Flask, render_template, request, redirect, url_for, jsonify
from config import Config
from models import db, Clinic, Patient, QueueEntry
from sqlalchemy import func
from datetime import datetime

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)


# -------------------------------
# 🔥 CLI COMMAND (DB INIT)
# -------------------------------
@app.cli.command("init-db")
def init_db():
    """Initialize the database."""
    db.create_all()

    if not Clinic.query.first():
        clinic = Clinic(clinic_name="City Clinic", doctor_name="Dr. Sharma")
        db.session.add(clinic)
        db.session.commit()

    print("Database initialized.")


# -------------------------------
# ✅ TEMP ROUTE (USE ONCE)
# -------------------------------
@app.route("/init-db")
def init_db_route():
    db.create_all()

    if not Clinic.query.first():
        clinic = Clinic(clinic_name="City Clinic", doctor_name="Dr. Sharma")
        db.session.add(clinic)
        db.session.commit()

    return "Database initialized!"


# -------------------------------
# ✅ HOME ROUTE
# -------------------------------
@app.route("/")
def home():
    return redirect(url_for("clinic_page", clinic_id=1))


# -------------------------------
# 1. Clinic Page
# -------------------------------
@app.route("/clinic/<int:clinic_id>")
def clinic_page(clinic_id):
    clinic = Clinic.query.get_or_404(clinic_id)
    return render_template("clinic.html", clinic=clinic)


# -------------------------------
# 2. Join Queue
# -------------------------------
@app.route("/join_queue", methods=["POST"])
def join_queue():
    name = request.form.get("name")
    phone = request.form.get("phone")
    clinic_id = int(request.form.get("clinic_id"))

    patient = Patient(name=name, phone=phone)
    db.session.add(patient)
    db.session.flush()

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
        status="waiting"
    )

    db.session.add(entry)
    db.session.commit()

    return redirect(url_for("queue_status", entry_id=entry.id))


# -------------------------------
# 3. Queue Status Page
# -------------------------------
@app.route("/queue/<int:entry_id>")
def queue_status(entry_id):
    entry = QueueEntry.query.get_or_404(entry_id)

    current = (
        QueueEntry.query
        .filter_by(clinic_id=entry.clinic_id, status="in_consultation")
        .first()
    )

    if current:
        now_serving_token = current.token_number
    else:
        next_waiting = (
            QueueEntry.query
            .filter_by(clinic_id=entry.clinic_id, status="waiting")
            .order_by(QueueEntry.token_number)
            .first()
        )
        now_serving_token = next_waiting.token_number if next_waiting else None

    patients_ahead = (
        QueueEntry.query
        .filter(
            QueueEntry.clinic_id == entry.clinic_id,
            QueueEntry.status.in_(["waiting", "in_consultation"]),
            QueueEntry.token_number < entry.token_number
        )
        .count()
    )

    completed = (
        QueueEntry.query
        .filter(
            QueueEntry.clinic_id == entry.clinic_id,
            QueueEntry.status == "served",
            QueueEntry.served_at.isnot(None),
            QueueEntry.joined_at.isnot(None)
        )
        .all()
    )

    avg_time = None
    if completed:
        total_time = 0
        count = 0

        for e in completed:
            duration = (e.served_at - e.joined_at).total_seconds() / 60
            total_time += duration
            count += 1

        if count > 0:
            avg_time = total_time / count

    estimated_wait = None

    if avg_time is not None:
        wait = 0

        if current and current.joined_at:
            elapsed = (datetime.utcnow() - current.joined_at).total_seconds() / 60
            remaining = max(avg_time - elapsed, 0)
            wait += remaining

        wait += patients_ahead * avg_time

        estimated_wait = round(wait, 2)

    return render_template(
        "queue.html",
        entry=entry,
        now_serving=now_serving_token,
        patients_ahead=patients_ahead,
        estimated_wait=estimated_wait
    )


# -------------------------------
# 4. API Endpoint
# -------------------------------
@app.route("/api/queue_status/<int:entry_id>")
def queue_status_api(entry_id):
    entry = QueueEntry.query.get_or_404(entry_id)

    current = (
        QueueEntry.query
        .filter_by(clinic_id=entry.clinic_id, status="in_consultation")
        .first()
    )

    if current:
        now_serving_token = current.token_number
    else:
        next_waiting = (
            QueueEntry.query
            .filter_by(clinic_id=entry.clinic_id, status="waiting")
            .order_by(QueueEntry.token_number)
            .first()
        )
        now_serving_token = next_waiting.token_number if next_waiting else None

    patients_ahead = (
        QueueEntry.query
        .filter(
            QueueEntry.clinic_id == entry.clinic_id,
            QueueEntry.status.in_(["waiting", "in_consultation"]),
            QueueEntry.token_number < entry.token_number
        )
        .count()
    )

    return jsonify({
        "token": entry.token_number,
        "now_serving": now_serving_token,
        "patients_ahead": patients_ahead,
        "estimated_wait": None
    })


# -------------------------------
# 5. Admin Dashboard
# -------------------------------
@app.route("/admin/<int:clinic_id>")
def admin_dashboard(clinic_id):
    entries = (
        QueueEntry.query
        .filter_by(clinic_id=clinic_id)
        .order_by(QueueEntry.token_number)
        .all()
    )

    return render_template(
        "admin.html",
        entries=entries,
        clinic_id=clinic_id
    )


# -------------------------------
# 6. Call Next
# -------------------------------
@app.route("/call_next/<int:clinic_id>", methods=["POST"])
def call_next(clinic_id):

    current = (
        QueueEntry.query
        .filter_by(clinic_id=clinic_id, status="in_consultation")
        .first()
    )

    if current:
        return redirect(url_for("admin_dashboard", clinic_id=clinic_id))

    next_patient = (
        QueueEntry.query
        .filter_by(clinic_id=clinic_id, status="waiting")
        .order_by(QueueEntry.token_number)
        .first()
    )

    if next_patient:
        next_patient.status = "in_consultation"
        db.session.commit()

    return redirect(url_for("admin_dashboard", clinic_id=clinic_id))


# -------------------------------
# 7. Complete Consultation
# -------------------------------
@app.route("/complete/<int:entry_id>", methods=["POST"])
def complete_consultation(entry_id):
    entry = QueueEntry.query.get_or_404(entry_id)

    if entry.status == "in_consultation":
        entry.status = "served"
        entry.served_at = datetime.utcnow()
        db.session.commit()

    return redirect(url_for("admin_dashboard", clinic_id=entry.clinic_id))


# -------------------------------
# Run App
# -------------------------------
if __name__ == "__main__":
    app.run(debug=True)