import os
from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _resolve_database_uri() -> str:
    configured_uri = os.environ.get("DATABASE_URL", "")
    if configured_uri:
        if configured_uri.startswith("sqlite:///"):
            relative_path = configured_uri[len("sqlite:///"):]
            if relative_path and not os.path.isabs(relative_path):
                return f"sqlite:///{os.path.abspath(os.path.join(BASE_DIR, relative_path))}"
        return configured_uri

    return f"sqlite:///{os.path.join(BASE_DIR, 'database', 'attendance.db')}"


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = _resolve_database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
    REPORTS_FOLDER = os.path.join(BASE_DIR, "generated_reports")
    LOG_FOLDER = os.path.join(BASE_DIR, "logs")
    SCREENSHOT_FOLDER = os.path.join(BASE_DIR, "screenshots")
    ALLOWED_EXTENSIONS = {"pdf", "xlsx", "xls", "csv", "txt"}
    MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25 MB

    # Portal credentials used only inside the Selenium worker
    PORTAL_URL = os.environ.get("PORTAL_URL", "")
    PORTAL_USERNAME = os.environ.get("PORTAL_USERNAME", "")
    PORTAL_PASSWORD = os.environ.get("PORTAL_PASSWORD", "")

    # Portal Supabase backend (live subject lists without Selenium)
    PORTAL_SUPABASE_URL = os.environ.get("PORTAL_SUPABASE_URL", "")
    PORTAL_SUPABASE_ANON_KEY = os.environ.get("PORTAL_SUPABASE_ANON_KEY", "")

    APP_ADMIN_USERNAME = os.environ.get("APP_ADMIN_USERNAME", "admin")
    APP_ADMIN_PASSWORD = os.environ.get("APP_ADMIN_PASSWORD", "admin123")

    SELENIUM_HEADLESS = os.environ.get("SELENIUM_HEADLESS", "true").lower() == "true"
    SELENIUM_IMPLICIT_WAIT = int(os.environ.get("SELENIUM_IMPLICIT_WAIT", "10"))

    # Register-number pattern used by the extractor. Adjust to match your
    # institution's format (default assumes 12-digit numeric register numbers).
    REGISTER_NUMBER_REGEX = r"\b\d{10,15}\b"
