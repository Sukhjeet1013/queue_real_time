from datetime import datetime

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, Index, text
from sqlalchemy.orm import validates
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class Clinic(db.Model):
    __tablename__ = "clinics"

    id = db.Column(db.Integer, primary_key=True)
    clinic_name = db.Column(db.String(100), nullable=False)
    doctor_name = db.Column(db.String(100), nullable=False)

    users = db.relationship("User", backref="clinic", lazy=True)
    queue_entries = db.relationship("QueueEntry", backref="clinic", lazy=True)


class User(db.Model, UserMixin):
    __tablename__ = "users"

    ROLE_SUPERADMIN = "superadmin"
    ROLE_CLINIC_ADMIN = "clinic_admin"
    VALID_ROLES = {
        ROLE_SUPERADMIN,
        ROLE_CLINIC_ADMIN,
    }

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), nullable=False)
    clinic_id = db.Column(db.Integer, db.ForeignKey("clinics.id"), nullable=True)

    @validates("role")
    def validate_role(self, key, value):
        if value not in self.VALID_ROLES:
            raise ValueError(f"Invalid user role: {value}")
        return value

    def set_password(self, raw_password):
        self.password = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        if not self.password or raw_password is None:
            return False

        try:
            return check_password_hash(self.password, raw_password)
        except ValueError:
            return self.password == raw_password

    def password_uses_hash(self):
        return self.password.startswith(("pbkdf2:", "scrypt:", "argon2:", "sha256:"))


class Patient(db.Model):
    __tablename__ = "patients"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    queue_entries = db.relationship("QueueEntry", backref="patient", lazy=True)


class QueueEntry(db.Model):
    __tablename__ = "queue_entries"

    STATUS_WAITING = "waiting"
    STATUS_IN_CONSULTATION = "in_consultation"
    STATUS_SERVED = "served"
    VALID_STATUSES = {
        STATUS_WAITING,
        STATUS_IN_CONSULTATION,
        STATUS_SERVED,
    }

    __table_args__ = (
        db.UniqueConstraint("clinic_id", "token_number", name="unique_token_per_clinic"),
        CheckConstraint(
            "status IN ('waiting', 'in_consultation', 'served')",
            name="check_queue_entry_status",
        ),
        Index(
            "unique_active_queue_entry_per_clinic",
            "clinic_id",
            unique=True,
            postgresql_where=text("status = 'in_consultation'"),
            sqlite_where=text("status = 'in_consultation'"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    clinic_id = db.Column(db.Integer, db.ForeignKey("clinics.id"), nullable=False)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False)
    token_number = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, default=STATUS_WAITING)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)
    consultation_started_at = db.Column(db.DateTime, nullable=True)
    served_at = db.Column(db.DateTime, nullable=True)

    @validates("status")
    def validate_status(self, key, value):
        if value not in self.VALID_STATUSES:
            raise ValueError(f"Invalid queue entry status: {value}")
        return value
