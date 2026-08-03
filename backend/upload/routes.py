import os
import json
import uuid
from flask import Blueprint, render_template, request, jsonify, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from backend.extensions import db
from backend.models import UploadedFile
from backend.upload.extractor import extract_absentees, extract_from_text

upload_bp = Blueprint("upload", __name__, template_folder="../../frontend/templates")


def _allowed_file(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in current_app.config["ALLOWED_EXTENSIONS"]


@upload_bp.route("/upload", methods=["GET"])
@login_required
def upload_page():
    return render_template("upload.html")


@upload_bp.route("/upload/file", methods=["POST"])
@login_required
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file part in request."}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected."}), 400

    if not _allowed_file(file.filename):
        return jsonify({"error": "Unsupported file type. Use PDF, XLSX, CSV, or TXT."}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower()
    safe_name = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
    save_path = os.path.join(current_app.config["UPLOAD_FOLDER"], safe_name)
    file.save(save_path)

    try:
        extracted = extract_absentees(save_path, ext)
    except Exception as e:
        return jsonify({"error": f"Failed to read file: {e}"}), 400

    record = UploadedFile(
        user_id=current_user.id,
        original_filename=file.filename,
        stored_filename=safe_name,
        file_type=ext,
        department=extracted.get("department") or "",
        section=extracted.get("section") or "",
        date_detected=extracted.get("date") or "",
        absentees_json=json.dumps(extracted["absentees"]),
        total_students_detected=extracted["total_students_detected"],
    )
    db.session.add(record)
    db.session.commit()

    return jsonify({
        "upload_id": record.id,
        "absentees": extracted["absentees"],
        "department": extracted.get("department"),
        "section": extracted.get("section"),
        "date": extracted.get("date"),
        "total_students_detected": extracted["total_students_detected"],
        "warnings": extracted.get("warnings", []),
    })


@upload_bp.route("/upload/<int:upload_id>/update", methods=["POST"])
@login_required
def update_absentees(upload_id):
    """Allow the HOD to edit/remove absentee register numbers before automation starts."""
    record = UploadedFile.query.get_or_404(upload_id)
    if record.user_id != current_user.id:
        return jsonify({"error": "Not authorized."}), 403

    data = request.get_json(force=True)
    absentees = data.get("absentees", [])
    record.absentees_json = json.dumps(absentees)
    record.total_students_detected = len(absentees)
    db.session.commit()
    return jsonify({"ok": True, "total_students_detected": len(absentees)})


@upload_bp.route("/upload/text", methods=["POST"])
@login_required
def upload_text():
    data = request.get_json(force=True)
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided."}), 400

    extracted = extract_from_text(text)

    record = UploadedFile(
        user_id=current_user.id,
        original_filename="pasted-list.txt",
        stored_filename="pasted",
        file_type="txt",
        absentees_json=json.dumps(extracted["absentees"]),
        total_students_detected=extracted["total_students_detected"],
    )
    db.session.add(record)
    db.session.commit()

    return jsonify({
        "upload_id": record.id,
        "absentees": extracted["absentees"],
        "total_students_detected": extracted["total_students_detected"],
        "warnings": extracted.get("warnings", []),
    })
