from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from flask_login import UserMixin

db = SQLAlchemy()


# -------------------------------
# 🏥 CLINIC
# -------------------------------
class Clinic(db.Model):
    __tablename__ = "clinics"

    id = db.Column(db.Integer, primary_key=True)
    clinic_name = db.Column(db.String(100), nullable=False)
    doctor_name = db.Column(db.String(100), nullable=False)

    # 🔥 relationship: one clinic → many users (admins)
    users = db.relationship("User", backref="clinic", lazy=True)

    # 🔥 relationship: one clinic → many queue entries
    queue_entries = db.relationship("QueueEntry", backref="clinic", lazy=True)


# -------------------------------
# 👤 USER (NEW - VERY IMPORTANT)
# -------------------------------
class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)

    # roles: superadmin | clinic_admin
    role = db.Column(db.String(50), nullable=False)

    # nullable because superadmin doesn't belong to a clinic
    clinic_id = db.Column(db.Integer, db.ForeignKey("clinics.id"), nullable=True)


# -------------------------------
# 🧑‍🤝‍🧑 PATIENT
# -------------------------------
class Patient(db.Model):
    __tablename__ = "patients"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    queue_entries = db.relationship("QueueEntry", backref="patient", lazy=True)


# -------------------------------
# 🎫 QUEUE ENTRY
# -------------------------------
class QueueEntry(db.Model):
    __tablename__ = "queue_entries"

    # ✅ Prevent duplicate tokens per clinic
    __table_args__ = (
        db.UniqueConstraint('clinic_id', 'token_number', name='unique_token_per_clinic'),
    )

    id = db.Column(db.Integer, primary_key=True)

    clinic_id = db.Column(db.Integer, db.ForeignKey("clinics.id"), nullable=False)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False)

    token_number = db.Column(db.Integer, nullable=False)

    # queue state
    status = db.Column(db.String(20), default="waiting")
    # waiting | in_consultation | served

    joined_at = db.Column(db.DateTime, default=datetime.utcnow)
    served_at = db.Column(db.DateTime, nullable=True)