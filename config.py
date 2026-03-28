import os


class Config:
    DEFAULT_CONSULTATION_TIME = 5
    MIN_CONSULTATION_TIME = 3
    MAX_CONSULTATION_TIME = 30
    WAIT_TIME_SAMPLE_SIZE = 10

    db_url = os.getenv("DATABASE_URL")
    if db_url and db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    if not db_url:
        db_url = "sqlite:///local.db"

    SECRET_KEY = os.getenv("SECRET_KEY") or "dev-fallback-key"

    SQLALCHEMY_DATABASE_URI = db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
    }

    SESSION_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = (
        os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
    )
