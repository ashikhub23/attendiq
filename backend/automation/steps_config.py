"""
====================================================================
 PORTAL-SPECIFIC CONFIGURATION - EDIT THIS FILE FOR YOUR COLLEGE
====================================================================
Every college attendance portal has different HTML, so the exact CSS
selectors / element IDs can't be guessed. Fill in the values below by
opening your portal in Chrome, right-click each field -> Inspect, and
copying its `id`, `name`, or a stable CSS selector.

Tip: right-click the element -> "Copy" -> "Copy selector" in Chrome
DevTools gives you a working CSS selector for most of these instantly.

Selector values can be:
  - A plain string        -> treated as a CSS selector
  - A tuple ("xpath", ..) -> treated as an XPath expression
  - None                  -> step is optional and will be skipped

====================================================================
 CURRENT TARGET: M.A.M College Attendance ERP
   https://students-list-self.vercel.app/
   - Login via Supabase (async) on index.html
   - Attendance marked period-wise with CHECKBOXES (present = checked)
   - Save upserts to a real Supabase backend
====================================================================
"""
from selenium.webdriver.common.by import By

BY_MAP = {
    "css": By.CSS_SELECTOR,
    "xpath": By.XPATH,
}

SELECTORS = {
    # ---- Login page (index.html) ----
    "login_username_field": "#loginEmail",
    "login_password_field": "#loginPassword",
    "login_submit_button": "#loginBtn",

    # ---- Post-login navigation ----
    "attendance_menu_link": "a[href='attendance.html']",

    # ---- Attendance filter dropdowns (attendance.html) ----
    "department_dropdown": "#deptSelect",
    "year_dropdown": "#yearSelect",
    "section_dropdown": "#sectionSelect",
    "subject_dropdown": "#subjectSelect",
    "date_input": "#dateInput",

    # The student table auto-loads once filters are chosen; no Load button.
    "load_students_button": None,

    # ---- Student attendance table ----
    "student_row": "#attBody tr",
    "student_regno_cell": "td .roll",
    "student_name_cell": "td .sname",
    # Each student row has 8 period checkboxes; PRESENT = checked.
    "attendance_status_checkbox": "input.period-cb",

    # ---- Bulk actions ----
    "mark_all_present_button": "#markAllPresent",
    "mark_all_absent_button": "#markAllAbsent",

    # ---- Save / submit ----
    "save_attendance_button": "#saveBtn",
    # Save shows a JS confirm() if any student has zero periods present.
    "save_confirmation_alert": True,
}

# How long (seconds) Selenium waits for each dynamic element before giving up.
EXPLICIT_WAIT_SECONDS = 15

# The new portal uses a "Mark All Present" button. Students are present by
# default once that is clicked, so we only UNCHECK absentees. Keeping this
# False means present students are left untouched (already checked).
REQUIRE_EXPLICIT_PRESENT_MARKING = False
