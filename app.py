from flask import Flask, render_template, request, redirect, url_for, jsonify
from config import Config
from models import db, Clinic, Patient, QueueEntry
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from datetime import datetime

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)


# -------------------------------
# 🔥 CLI COMMAND (DB INIT)
# -------------------------------
@app.cli.command("init-db")
def init_db():
    db.create_all()

    if not Clinic.query.first():
        clinics = [
            Clinic(clinic_name="City Clinic", doctor_name="Dr. Sharma"),
            Clinic(clinic_name="HealthCare Plus", doctor_name="Dr. Mehta"),
            Clinic(clinic_name="Wellness Center", doctor_name="Dr. Rao"),
        ]
        db.session.add_all(clinics)
        db.session.commit()

    print("Database initialized.")


# -------------------------------
# 🚀 ROUTE-BASED DB INIT (NEW)
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
# ✅ HOME → CLINICS LIST
# -------------------------------
@app.route("/")
def home():
    return redirect(url_for("clinics_list"))


# -------------------------------
# 🔥 CLINICS LIST
# -------------------------------
@app.route("/clinics")
def clinics_list():
    clinics = Clinic.query.all()
    return render_template("clinics.html", clinics=clinics)


# -------------------------------
# 🔥 ADD CLINIC
# -------------------------------
@app.route("/add_clinic", methods=["GET", "POST"])
def add_clinic():
    if request.method == "POST":
        name = request.form.get("clinic_name")
        doctor = request.form.get("doctor_name")

        if not name or not doctor:
            return "Invalid input", 400

        clinic = Clinic(clinic_name=name, doctor_name=doctor)
        db.session.add(clinic)
        db.session.commit()

        return redirect(url_for("clinics_list"))

    return render_template("add_clinic.html")


# -------------------------------
# 1. CLINIC PAGE
# -------------------------------
@app.route("/clinic/<int:clinic_id>")
def clinic_page(clinic_id):
    clinic = Clinic.query.get_or_404(clinic_id)
    return render_template("clinic.html", clinic=clinic)


# -------------------------------
# 2. JOIN QUEUE (SAFE)
# -------------------------------
@app.route("/join_queue", methods=["POST"])
def join_queue():
    name = request.form.get("name")
    phone = request.form.get("phone")
    clinic_id = int(request.form.get("clinic_id"))

    patient = Patient(name=name, phone=phone)
    db.session.add(patient)
    db.session.flush()

    while True:
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
                status="waiting"
            )

            db.session.add(entry)
            db.session.commit()
            break

        except IntegrityError:
            db.session.rollback()

    return redirect(url_for("queue_status", entry_id=entry.id))


# -------------------------------
# 3. QUEUE STATUS
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
        for e in completed:
            duration = (e.served_at - e.joined_at).total_seconds() / 60
            total_time += duration

        avg_time = total_time / len(completed)

    estimated_wait = None

    if avg_time is not None:
        wait = 0

        if current and current.joined_at:
            elapsed = (datetime.utcnow() - current.joined_at).total_seconds() / 60
            wait += max(avg_time - elapsed, 0)

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
# RUN APP
# -------------------------------
if __name__ == "__main__":
    app.run(debug=True)