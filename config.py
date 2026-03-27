import os

class Config:
    db_url = os.getenv("https://dashboard.render.com/d/dpg-d73cv6khg0os73ba4kn0-a")

    # Fix Render PostgreSQL URL issue
    if db_url and db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    # Fallback for local development
    if not db_url:
        db_url = "postgresql://postgres:password@localhost:5432/smartqueue"

    SQLALCHEMY_DATABASE_URI = db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
