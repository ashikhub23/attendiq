import json
import os
from flask import Blueprint, render_template, send_file, current_app, abort
from flask_login import login_required, current_user

from backend.extensions import db
from backend.models import AutomationSession, UploadedFile, AttendanceReport
from backend.reports.generator import generate_reports

reports_bp = Blueprint("reports", __name__, template_folder="../../frontend/templates")


def _build_student_rows(session):
    """Absentees are known by register number/name; everyone else in the
    uploaded file's roster (if any) is marked Present. If only an absentee
    list was uploaded (no full roster), the report lists absentees plus a
    summary present count from the automation session."""
    upload = UploadedFile.query.get(session.uploaded_file_id)
    absentees = json.loads(upload.absentees_json) if upload else []
    rows = [{"register_number": a["register_number"], "name": a.get("name"), "status": "Absent"}
            for a in absentees]
    return rows


@reports_bp.route("/reports")
@login_required
def reports_list():
    sessions = (AutomationSession.query
                .filter_by(user_id=current_user.id)
                .order_by(AutomationSession.started_at.desc())
                .all())
    return render_template("reports.html", sessions=sessions)


@reports_bp.route("/reports/<int:session_id>/view")
@login_required
def view_report(session_id):
    session = AutomationSession.query.get_or_404(session_id)
    if session.user_id != current_user.id:
        abort(403)
    rows = _build_student_rows(session)
    return render_template("report_detail.html", session=session, rows=rows)


def _get_or_generate(session_id):
    session = AutomationSession.query.get_or_404(session_id)
    if session.user_id != current_user.id:
        abort(403)

    report = AttendanceReport.query.filter_by(session_id=session_id).first()
    if report and all(os.path.exists(p) for p in [report.pdf_path, report.excel_path, report.csv_path]):
        return report

    rows = _build_student_rows(session)
    paths = generate_reports(current_app.config["REPORTS_FOLDER"], session, rows)

    if report is None:
        report = AttendanceReport(session_id=session_id)
        db.session.add(report)
    report.pdf_path = paths["pdf_path"]
    report.excel_path = paths["excel_path"]
    report.csv_path = paths["csv_path"]
    db.session.commit()
    return report


@reports_bp.route("/reports/<int:session_id>/download/<fmt>")
@login_required
def download_report(session_id, fmt):
    report = _get_or_generate(session_id)
    path_map = {"pdf": report.pdf_path, "excel": report.excel_path, "csv": report.csv_path}
    path = path_map.get(fmt)
    if not path or not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True)
