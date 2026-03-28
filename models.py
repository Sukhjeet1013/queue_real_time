from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class Clinic(db.Model):
    __tablename__ = "clinics"

    id = db.Column(db.Integer, primary_key=True)
    clinic_name = db.Column(db.String(100), nullable=False)
    doctor_name = db.Column(db.String(100), nullable=False)


class Patient(db.Model):
    __tablename__ = "patients"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class QueueEntry(db.Model):
    __tablename__ = "queue_entries"

    # ✅ UNIQUE constraint added here
    __table_args__ = (
        db.UniqueConstraint('clinic_id', 'token_number', name='unique_token_per_clinic'),
    )

    id = db.Column(db.Integer, primary_key=True)
    clinic_id = db.Column(db.Integer, db.ForeignKey("clinics.id"), nullable=False)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False)

    token_number = db.Column(db.Integer, nullable=False)

    # NEW STATE SYSTEM
    status = db.Column(db.String(20), default="waiting")
    # values: waiting | in_consultation | served

    joined_at = db.Column(db.DateTime, default=datetime.utcnow)
    served_at = db.Column(db.DateTime, nullable=True)

    clinic = db.relationship("Clinic")
    patient = db.relationship("Patient")