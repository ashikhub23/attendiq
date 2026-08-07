import os
from datetime import datetime, timedelta
from flask import Blueprint, render_template
from flask_login import login_required, current_user

from backend.utils.timeutils import IST_OFFSET, now_ist
from backend.models import AutomationSession, AutomationLog

dashboard_bp = Blueprint("dashboard", __name__, template_folder="../frontend/templates")


@dashboard_bp.route("/")
@login_required
def index():
    now = now_ist()
    today_start_ist = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_ist - IST_OFFSET

    all_sessions = (AutomationSession.query
                     .filter_by(user_id=current_user.id)
                     .order_by(AutomationSession.started_at.desc())
                     .all())

    todays_count = sum(1 for s in all_sessions if s.started_at and s.started_at >= today_start_utc)
    total_present = sum(s.total_present for s in all_sessions)
    total_absent = sum(s.total_absent for s in all_sessions)
    last_automation = all_sessions[0].started_at if all_sessions else None

    recent_sessions = all_sessions[:5]
    recent_logs = (AutomationLog.query
                    .join(AutomationSession)
                    .filter(AutomationSession.user_id == current_user.id)
                    .order_by(AutomationLog.timestamp.desc())
                    .limit(15)
                    .all())

    return render_template(
        "dashboard.html",
        todays_count=todays_count,
        total_sessions=len(all_sessions),
        last_automation=last_automation,
        total_present=total_present,
        total_absent=total_absent,
        recent_sessions=recent_sessions,
        recent_logs=recent_logs,
    )
