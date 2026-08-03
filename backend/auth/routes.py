from flask import Blueprint, render_template, redirect, url_for, request, flash, current_app
from flask_login import login_user, logout_user, login_required

from backend.extensions import db
from backend.models import User

auth_bp = Blueprint("auth", __name__, template_folder="../../frontend/templates")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("dashboard.index"))

        flash("Invalid username or password.", "error")

    return render_template("login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
