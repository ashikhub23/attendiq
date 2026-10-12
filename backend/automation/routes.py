import io
import json
import os
import threading
import time
from datetime import datetime
from urllib.parse import quote

import requests
from PIL import Image

from flask import (
    Blueprint, render_template, request, jsonify, current_app,
    send_from_directory, Response, stream_with_context,
)
from flask_login import login_required, current_user

from backend.extensions import db
from backend.models import UploadedFile, AutomationSession, AutomationLog
from backend.utils.timeutils import utc_to_ist
from backend.automation.selenium_bot import AttendanceAutomationBot

automation_bp = Blueprint("automation", __name__, template_folder="../../frontend/templates")

_SUPABASE_TOKEN_CACHE = {"token": None, "expires_at": 0}


def _supabase_headers(app):
    """Return headers with a valid Supabase access token for the HOD portal."""
    now = time.time()
    cached = _SUPABASE_TOKEN_CACHE
    if cached["token"] and now < cached["expires_at"]:
        return cached["headers"]

    supabase_url = app.config["PORTAL_SUPABASE_URL"].rstrip("/")
    anon_key = app.config["PORTAL_SUPABASE_ANON_KEY"]

    resp = requests.post(
        f"{supabase_url}/auth/v1/token?grant_type=password",
        headers={"apikey": anon_key, "Content-Type": "application/json"},
        json={"email": app.config["PORTAL_USERNAME"], "password": app.config["PORTAL_PASSWORD"]},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    token = body["access_token"]

    cached["token"] = token
    cached["expires_at"] = now + max(60, int(body.get("expires_in", 3600)) - 60)
    cached["headers"] = {"apikey": anon_key, "Authorization": f"Bearer {token}"}
    return cached["headers"]


@automation_bp.route("/automation/subjects")
@login_required
def portal_subjects():
    """Fetch the live subject list for a class from the portal's Supabase backend."""
    department = request.args.get("department", "").strip()
    year = request.args.get("year", "").strip()
    section = request.args.get("section", "").strip()
    if not (department and year and section):
        return jsonify({"subjects": []})

    try:
        supabase_url = current_app.config["PORTAL_SUPABASE_URL"].rstrip("/")
        if not supabase_url:
            return jsonify({
                "subjects": [],
                "error": "PORTAL_SUPABASE_URL is not configured on the server.",
            }), 500
        headers = _supabase_headers(current_app)

        def fetch(extra_fields):
            qs = "select=name&order=name"
            for field, value in extra_fields:
                qs += f"&{field}=eq.{quote(value, safe='')}"
            resp = requests.get(
                f"{supabase_url}/rest/v1/subjects?{qs}",
                headers=headers,
                timeout=20,
            )
            resp.raise_for_status()
            return [row["name"] for row in resp.json()]

        # Exact class match first. If the portal stores year/section in a
        # slightly different format, relax the filters so the HOD can still
        # pick a subject instead of seeing an empty list.
        attempts = [
            ("department", department), ("year", year), ("section", section),
        ]
        subjects = fetch(attempts)
        note = ""
        if not subjects:
            subjects = fetch([("department", department), ("section", section)])
            if subjects:
                note = "Showing subjects for this department/section (year filter relaxed)."
        if not subjects:
            subjects = fetch([("department", department)])
            if subjects:
                note = "Showing all subjects for this department."
    except Exception as exc:
        return jsonify({"subjects": [], "error": str(exc)}), 502

    payload = {"subjects": subjects}
    if note:
        payload["note"] = note
    return jsonify(payload)


@automation_bp.route("/automation/progress/<int:session_id>")
@login_required
def progress_page(session_id):
    session = AutomationSession.query.get_or_404(session_id)
    return render_template("progress.html", session_id=session_id)


@automation_bp.route("/automation/start", methods=["POST"])
@login_required
def start_automation():
    data = request.get_json(force=True)
    upload_id = data.get("upload_id")
    filters = data.get("filters", {})

    upload = UploadedFile.query.get_or_404(upload_id)
    if upload.user_id != current_user.id:
        return jsonify({"error": "Not authorized."}), 403

    absentees = json.loads(upload.absentees_json)
    if not absentees:
        return jsonify({"error": "No absentees to process. Upload a file with register numbers or student names."}), 400

    session = AutomationSession(
        user_id=current_user.id,
        uploaded_file_id=upload.id,
        status="running",
        current_step="Queued",
        progress_percent=0,
        department=filters.get("department", upload.department or ""),
        year=filters.get("year", ""),
        section=filters.get("section", upload.section or ""),
        subject=filters.get("subject", ""),
        date=filters.get("date", upload.date_detected or ""),
        period=filters.get("period", ""),
        started_at=datetime.utcnow(),
    )
    db.session.add(session)
    db.session.commit()

    app_obj = current_app._get_current_object()
    bot = AttendanceAutomationBot(
        app=app_obj,
        session_id=session.id,
        absentees=absentees,
        filters=session.__dict__,
        headless=current_app.config["SELENIUM_HEADLESS"],
    )
    thread = threading.Thread(target=bot.run, daemon=True)
    thread.start()

    return jsonify({"session_id": session.id})


def _latest_screenshot(session_id):
    """Find the most recent screenshot file for a given session."""
    with current_app.app_context():
        folder = current_app.config["SCREENSHOT_FOLDER"]
    if not os.path.isdir(folder):
        return None
    prefix = f"{session_id}_"
    files = sorted(
        (f for f in os.listdir(folder) if f.startswith(prefix) and f.endswith(".png")),
        reverse=True,
    )
    return files[0] if files else None


@automation_bp.route("/automation/status/<int:session_id>")
@login_required
def automation_status(session_id):
    session = AutomationSession.query.get_or_404(session_id)
    logs = (AutomationLog.query
            .filter_by(session_id=session_id)
            .order_by(AutomationLog.timestamp.asc())
            .all())
    screenshot = _latest_screenshot(session_id)
    return jsonify({
        "status": session.status,
        "current_step": session.current_step,
        "progress_percent": session.progress_percent,
        "error_message": session.error_message,
        "total_present": session.total_present,
        "total_absent": session.total_absent,
        "duration_seconds": session.duration_seconds,
        "screenshot": f"/automation/screenshot/{session_id}/{screenshot}" if screenshot else None,
        "logs": [
            {"time": utc_to_ist(log.timestamp).strftime("%H:%M:%S"), "level": log.level, "message": log.message}
            for log in logs
        ],
    })


@automation_bp.route("/automation/retry/<int:session_id>", methods=["POST"])
@login_required
def retry_automation(session_id):
    old_session = AutomationSession.query.get_or_404(session_id)
    if old_session.user_id != current_user.id:
        return jsonify({"error": "Not authorized."}), 403

    upload = UploadedFile.query.get(old_session.uploaded_file_id)
    absentees = json.loads(upload.absentees_json)

    new_session = AutomationSession(
        user_id=current_user.id,
        uploaded_file_id=upload.id,
        status="running",
        current_step="Queued (retry)",
        progress_percent=0,
        department=old_session.department,
        year=old_session.year,
        section=old_session.section,
        subject=old_session.subject,
        date=old_session.date,
        period=old_session.period,
        started_at=datetime.utcnow(),
    )
    db.session.add(new_session)
    db.session.commit()

    app_obj = current_app._get_current_object()
    bot = AttendanceAutomationBot(
        app=app_obj,
        session_id=new_session.id,
        absentees=absentees,
        filters=new_session.__dict__,
        headless=current_app.config["SELENIUM_HEADLESS"],
    )
    thread = threading.Thread(target=bot.run, daemon=True)
    thread.start()

    return jsonify({"session_id": new_session.id})


@automation_bp.route("/automation/live/<int:session_id>")
@login_required
def live_frame(session_id):
    """Serve the current live browser frame as a plain PNG (proxy-friendly).
    The frontend polls this every ~1s with a cache-busting query param;
    MJPEG streaming (multipart/x-mixed-replace) gets buffered by Render's
    proxy, so we avoid it and serve static images instead."""
    with current_app.app_context():
        folder = current_app.config["SCREENSHOT_FOLDER"]
    live_path = os.path.join(folder, f"live_{session_id}.png")
    if not os.path.exists(live_path):
        latest = _latest_screenshot(session_id)
        if not latest:
            return Response(status=204)
        live_path = os.path.join(folder, latest)
    try:
        with open(live_path, "rb") as f:
            data = f.read()
    except Exception:
        return Response(status=204)
    return Response(data, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@automation_bp.route("/automation/screenshot/<int:session_id>/<filename>")
@login_required
def serve_screenshot(session_id, filename):
    with current_app.app_context():
        folder = current_app.config["SCREENSHOT_FOLDER"]
    return send_from_directory(folder, filename, mimetype="image/png")


def _png_to_jpeg(png_bytes):
    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        return buf.getvalue()
    except Exception:
        return None


def _placeholder_jpeg():
    img = Image.new("RGB", (960, 540), (24, 24, 32))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()


@automation_bp.route("/automation/stream/<int:session_id>")
@login_required
def automation_stream(session_id):
    """True live MJPEG stream of the running browser (from live_<id>.png)."""
    with current_app.app_context():
        folder = current_app.config["SCREENSHOT_FOLDER"]
    live_path = os.path.join(folder, f"live_{session_id}.png")
    placeholder = _placeholder_jpeg()

    def gen():
        idle_after_done = 0
        while True:
            with current_app.app_context():
                session = AutomationSession.query.get(session_id)
            running = session is not None and session.status == "running"

            png = None
            if os.path.exists(live_path):
                try:
                    with open(live_path, "rb") as f:
                        png = f.read()
                except Exception:
                    png = None

            jpeg = _png_to_jpeg(png) if png else placeholder
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"

            if running:
                idle_after_done = 0
            else:
                idle_after_done += 1
                if idle_after_done >= 3:
                    break
            time.sleep(0.5)

    return Response(
        stream_with_context(gen()),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )
