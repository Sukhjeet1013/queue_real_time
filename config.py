import os


class Config:
    db_url = os.getenv("DATABASE_URL")

    # Fix Railway / Render postgres URL issue
    if db_url and db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    # Local fallback (only used if env not set)
    if not db_url:
        db_url = "sqlite:///local.db"

    SQLALCHEMY_DATABASE_URI = db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # -----------------------------
    # SECURITY (FIXED)
    # -----------------------------
    # Use env if available, otherwise fallback to prevent build crash
    SECRET_KEY = os.getenv("SECRET_KEY") or "dev-fallback-key"

    # -----------------------------
    # SESSION CONFIG
    # -----------------------------
    SESSION_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    SESSION_COOKIE_SECURE = (
        os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
    )