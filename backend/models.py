from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from backend.extensions import db


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    full_name = db.Column(db.String(120), default="")
    password_hash = db.Column(db.String(255), nullable=False)
    department = db.Column(db.String(120), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class UploadedFile(db.Model):
    __tablename__ = "uploaded_files"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    original_filename = db.Column(db.String(255))
    stored_filename = db.Column(db.String(255))
    file_type = db.Column(db.String(10))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    # extracted metadata (JSON-serialized in extractor, stored as text)
    department = db.Column(db.String(120), default="")
    section = db.Column(db.String(50), default="")
    date_detected = db.Column(db.String(50), default="")
    absentees_json = db.Column(db.Text, default="[]")
    total_students_detected = db.Column(db.Integer, default=0)


class AutomationSession(db.Model):
    __tablename__ = "automation_sessions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    uploaded_file_id = db.Column(db.Integer, db.ForeignKey("uploaded_files.id"))

    status = db.Column(db.String(30), default="pending")  # pending/running/success/failed
    current_step = db.Column(db.String(120), default="")
    progress_percent = db.Column(db.Integer, default=0)
    error_message = db.Column(db.Text, default="")

    department = db.Column(db.String(120), default="")
    year = db.Column(db.String(20), default="")
    section = db.Column(db.String(50), default="")
    subject = db.Column(db.String(120), default="")
    date = db.Column(db.String(50), default="")
    period = db.Column(db.String(20), default="")

    total_present = db.Column(db.Integer, default=0)
    total_absent = db.Column(db.Integer, default=0)

    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)
    duration_seconds = db.Column(db.Integer, default=0)


class AutomationLog(db.Model):
    __tablename__ = "automation_logs"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("automation_sessions.id"))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    level = db.Column(db.String(10), default="INFO")  # INFO / SUCCESS / ERROR
    message = db.Column(db.String(255))


class AttendanceReport(db.Model):
    __tablename__ = "attendance_reports"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("automation_sessions.id"))
    pdf_path = db.Column(db.String(255))
    excel_path = db.Column(db.String(255))
    csv_path = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
