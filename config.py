import os

class Config:
    db_url = os.getenv("DATABASE_URL")

    # Fix Railway / Render postgres URL issue
    if db_url and db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    # Optional fallback (only for local testing)
    if not db_url:
        db_url = "sqlite:///local.db"

    SQLALCHEMY_DATABASE_URI = db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False