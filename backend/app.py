import os
from datetime import datetime
import click
from flask import Flask
from flask_login import LoginManager

from backend.config import Config
from backend.extensions import db, login_manager
from backend.models import User
from backend.utils.timeutils import IST_OFFSET, utc_to_ist, now_ist


def create_app():
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "..", "frontend", "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "..", "frontend", "static"),
    )
    app.config.from_object(Config)

    @app.template_filter("ist")
    def ist_filter(dt):
        return utc_to_ist(dt)

    @app.context_processor
    def inject_globals():
        return {
            "current_year": now_ist().year,
            "current_date": now_ist().strftime("%Y-%m-%d"),
        }

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["REPORTS_FOLDER"], exist_ok=True)
    os.makedirs(app.config["LOG_FOLDER"], exist_ok=True)
    os.makedirs(app.config["SCREENSHOT_FOLDER"], exist_ok=True)
    os.makedirs(os.path.dirname(app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")), exist_ok=True) \
        if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///") else None

    db.init_app(app)
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.auth.routes import auth_bp
    from backend.upload.routes import upload_bp
    from backend.automation.routes import automation_bp
    from backend.reports.routes import reports_bp
    from backend.dashboard import dashboard_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(automation_bp)
    app.register_blueprint(reports_bp)

    with app.app_context():
        db.create_all()
        _seed_admin_user(app)

    @app.cli.command("create-admin")
    @click.argument("username")
    @click.argument("password")
    def create_admin(username, password):
        """Create (or update) an HOD login: flask create-admin <username> <password>"""
        user = User.query.filter_by(username=username).first()
        if user is None:
            user = User(username=username, full_name="HOD")
            db.session.add(user)
        user.set_password(password)
        db.session.commit()
        click.echo(f"Admin user '{username}' created/updated.")

    return app


def _seed_admin_user(app):
    """Ensure a default HOD login exists on first run, from .env values."""
    username = app.config["APP_ADMIN_USERNAME"]
    password = app.config["APP_ADMIN_PASSWORD"]
    if not User.query.filter_by(username=username).first():
        user = User(username=username, full_name="Head of Department", department="")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
