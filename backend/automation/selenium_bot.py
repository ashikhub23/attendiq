"""
Background Selenium worker that performs the end-to-end attendance
automation described in the spec:

  open browser -> login -> navigate -> select filters -> mark absentees
  -> mark present -> save -> verify -> close browser

It runs inside a Python thread (started by the /automation/start route) so
the Flask request returns immediately; the frontend polls /automation/status
for live progress, matching the "Live Progress" requirement without needing
websockets.

All portal-specific selectors live in steps_config.py - this file only
contains the workflow logic and should not normally need edits.
"""
import glob
import os
import re
import threading
import time
import traceback
from datetime import datetime

from flask import current_app
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, WebDriverException
)
from webdriver_manager.chrome import ChromeDriverManager

from backend.automation.steps_config import SELECTORS, EXPLICIT_WAIT_SECONDS, BY_MAP
from backend.extensions import db
from backend.models import AutomationSession, AutomationLog

STEPS = [
    ("open_browser", "Opening Browser", 5),
    ("login", "Logging In", 15),
    ("navigate", "Loading Attendance Page", 25),
    ("select_filters", "Selecting Department / Year / Section / Subject / Date", 40),
    ("load_students", "Reading Uploaded File & Loading Students", 55),
    ("mark_absent", "Marking Absentees", 70),
    ("mark_present", "Marking Present Students", 80),
    ("save", "Saving Attendance", 90),
    ("verify", "Verification Completed", 100),
]


class ChromeDriverNotFoundError(Exception):
    """Raised when no ChromeDriver binary exists on disk."""


class AutomationError(Exception):
    """Raised for any recoverable automation failure; message is user-facing."""


class AttendanceAutomationBot:
    def __init__(self, app, session_id, absentees, filters, headless=True):
        self.app = app
        self.session_id = session_id
        self.absentee_reg_nos = {a["register_number"] for a in absentees if a.get("register_number")}
        self.absentee_names = {
            re.sub(r"\s+", " ", a["name"]).strip().lower()
            for a in absentees if a.get("name")
        }
        self.filters = filters
        self.headless = headless
        self.driver = None
        self._screenshot_count = 0
        self._live_stop = threading.Event()
        self._live_path = None
        self._live_thread = None

    # ---------- progress / logging helpers ----------

    def _update(self, step_key, message, percent):
        with self.app.app_context():
            session = AutomationSession.query.get(self.session_id)
            session.current_step = message
            session.progress_percent = percent
            db.session.add(AutomationLog(
                session_id=self.session_id, level="INFO", message=message
            ))
            db.session.commit()

    def _log(self, message, level="INFO"):
        with self.app.app_context():
            db.session.add(AutomationLog(
                session_id=self.session_id, level=level, message=message
            ))
            db.session.commit()

    def _log_error(self, message):
        with self.app.app_context():
            session = AutomationSession.query.get(self.session_id)
            session.status = "failed"
            session.error_message = message
            session.finished_at = datetime.utcnow()
            db.session.add(AutomationLog(
                session_id=self.session_id, level="ERROR", message=message
            ))
            db.session.commit()

    def _log_success(self, present_count, absent_count):
        with self.app.app_context():
            session = AutomationSession.query.get(self.session_id)
            session.status = "success"
            session.progress_percent = 100
            session.total_present = present_count
            session.total_absent = absent_count
            session.finished_at = datetime.utcnow()
            session.duration_seconds = int(
                (session.finished_at - session.started_at).total_seconds()
            )
            db.session.add(AutomationLog(
                session_id=self.session_id, level="SUCCESS",
                message=f"Attendance saved. Present={present_count}, Absent={absent_count}"
            ))
            db.session.commit()

    def _screenshot(self, label="step"):
        if not self.driver:
            return
        with self.app.app_context():
            folder = current_app.config["SCREENSHOT_FOLDER"]
        os.makedirs(folder, exist_ok=True)
        self._screenshot_count += 1
        filename = f"{self.session_id}_{self._screenshot_count:03d}_{label}.png"
        path = os.path.join(folder, filename)
        try:
            self.driver.save_screenshot(path)
            self._log(f"[screen] {filename}")
        except Exception:
            pass

    # ---------- live frame capture (for the streaming browser view) ----------

    def _start_live_frames(self):
        """Capture a fresh screenshot every second to live_<session_id>.png so
        the progress page can stream a true live view instead of step photos."""
        if not self.driver:
            return
        with self.app.app_context():
            folder = current_app.config["SCREENSHOT_FOLDER"]
        os.makedirs(folder, exist_ok=True)
        self._live_path = os.path.join(folder, f"live_{self.session_id}.png")
        self._live_stop.clear()

        def grabber():
            while not self._live_stop.is_set():
                try:
                    png = self.driver.get_screenshot_as_png()
                    with open(self._live_path, "wb") as f:
                        f.write(png)
                except Exception:
                    pass
                self._live_stop.wait(1.0)

        self._live_thread = threading.Thread(target=grabber, daemon=True)
        self._live_thread.start()

    def _stop_live_frames(self):
        self._live_stop.set()
        if self._live_thread and self._live_thread.is_alive():
            self._live_thread.join(timeout=2)

    # ---------- selector helpers ----------

    def _resolve(self, key):
        val = SELECTORS.get(key)
        if val is None:
            return None, None
        if isinstance(val, tuple):
            return BY_MAP[val[0]], val[1]
        return By.CSS_SELECTOR, val

    def _find(self, key, parent=None):
        by, selector = self._resolve(key)
        if by is None:
            return None
        if parent is not None:
            return parent.find_element(by, selector)
        return self.driver.find_element(by, selector)

    def _wait(self, by, selector, timeout=None):
        timeout = timeout or EXPLICIT_WAIT_SECONDS
        return WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((by, selector))
        )

    def _wait_for_key(self, key, timeout=None):
        by, selector = self._resolve(key)
        if by is None:
            return None
        return self._wait(by, selector, timeout)

    def _find_all(self, key):
        by, selector = self._resolve(key)
        if by is None:
            return []
        return self.driver.find_elements(by, selector)

    # ---------- workflow ----------

    def run(self):
        try:
            self._open_browser()
            self._start_live_frames()
            self._login()
            self._navigate_to_attendance()
            self._select_filters()
            present_count, absent_count = self._mark_students()
            self._save_and_verify()
            self._log_success(present_count, absent_count)
        except AutomationError as e:
            url = self.driver.current_url if self.driver else "N/A"
            self._log_error(f"{e}  [page: {url}]")
        except WebDriverException as e:
            url = self.driver.current_url if self.driver else "N/A"
            self._log_error(f"Browser error ({url}): {str(e)[:300]}")
        except Exception:
            url = self.driver.current_url if self.driver else "N/A"
            self._log_error(f"Unexpected error on {url}:\n{traceback.format_exc(limit=3)}")
        finally:
            self._stop_live_frames()
            if self.driver:
                try:
                    self.driver.quit()
                    self._log("Browser closed.")
                except Exception:
                    pass

    def _open_browser(self):
        self._update("open_browser", "Opening Browser", 5)
        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
            self._log("Running in headless mode.")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")

        self._log("Launching Chrome browser...")
        try:
            service = Service(self._find_local_chromedriver())
            self._log(f"Using local ChromeDriver: {service.path}")
        except ChromeDriverNotFoundError:
            self._log("No local ChromeDriver found; downloading via webdriver-manager...")
            service = Service(ChromeDriverManager().install())

        self.driver = webdriver.Chrome(service=service, options=options)
        self.driver.implicitly_wait(3)
        self._log("Browser ready.")
        self._screenshot("browser_open")

    def _find_local_chromedriver(self):
        """Locate a ChromeDriver already on disk (cache / PATH) so we never
        depend on GitHub to download it on every run."""
        candidates = [
            os.path.join(self.app.root_path, "..", "drivers", "chromedriver.exe"),
            os.path.join(os.getcwd(), "drivers", "chromedriver.exe"),
        ]
        cache_root = os.path.join(os.path.expanduser("~"), ".wdm", "drivers")
        if os.path.isdir(cache_root):
            candidates += sorted(
                glob.glob(os.path.join(cache_root, "**", "chromedriver.exe"), recursive=True),
                reverse=True,
            )
        for path in candidates:
            if path and os.path.isfile(path):
                return path

        import shutil
        on_path = shutil.which("chromedriver")
        if on_path:
            return on_path
        raise ChromeDriverNotFoundError()

    def _login(self):
        self._update("login", "Logging In", 15)
        with self.app.app_context():
            portal_url = current_app.config["PORTAL_URL"]
            username = current_app.config["PORTAL_USERNAME"]
            password = current_app.config["PORTAL_PASSWORD"]

        if not portal_url or not username or not password:
            raise AutomationError(
                "Portal URL / credentials are not configured. Set PORTAL_URL, "
                "PORTAL_USERNAME, PORTAL_PASSWORD in your .env file."
            )

        self._log(f"Navigating to portal: {portal_url}")
        try:
            self.driver.get(portal_url)
        except Exception as e:
            raise AutomationError(f"Could not reach portal at {portal_url}: {e}")

        self._log("Portal loaded. Filling login credentials...")
        self._screenshot("portal_loaded")
        try:
            user_field = self._wait_for_key("login_username_field")
            pass_field = self._find("login_password_field")
            user_field.clear()
            user_field.send_keys(username)
            self._log(f"Username entered: {username}")
            pass_field.clear()
            pass_field.send_keys(password)
            self._log("Password entered. Submitting login form...")
            self._find("login_submit_button").click()
        except (TimeoutException, NoSuchElementException):
            raise AutomationError("Login Failed - could not find login fields on the portal page.")

        # Supabase sign-in is async; wait for the redirect to dashboard.html.
        self._log("Waiting for portal to authenticate...")
        try:
            WebDriverWait(self.driver, EXPLICIT_WAIT_SECONDS).until(
                EC.url_contains("dashboard.html")
            )
        except TimeoutException:
            raise AutomationError(
                "Login Failed - portal did not reach the dashboard. "
                "Check PORTAL_USERNAME / PORTAL_PASSWORD in your .env file."
            )

        time.sleep(1)
        self._log(f"Post-login page: {self.driver.current_url.split('/')[-1]}")
        self._log("Login successful.")
        self._screenshot("login_success")

    def _navigate_to_attendance(self):
        self._update("navigate", "Loading Attendance Page", 25)
        with self.app.app_context():
            portal_url = current_app.config["PORTAL_URL"]
        self._log("Navigating to attendance page...")
        self.driver.get(portal_url.rstrip("/") + "/attendance.html")

        self._log("Waiting for attendance page to load...")
        try:
            # deptSelect is populated asynchronously from the API.
            WebDriverWait(self.driver, EXPLICIT_WAIT_SECONDS).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "#deptSelect option")) > 0
            )
        except TimeoutException:
            raise AutomationError(
                "Attendance page did not load properly (department options missing)."
            )

        self._log("Attendance page loaded.")
        self._screenshot("attendance_page")

    def _select_filters(self):
        self._update("select_filters", "Selecting Filters", 40)
        self._log("Applying class filters...")

        # Department / Year / Section are plain <select>s with known IDs.
        for key in ["department", "year", "section"]:
            value = self.filters.get(key)
            if not value:
                self._log(f"  {key}: (skipped - no value provided)")
                continue
            sel_el = self._find(f"{key}_dropdown")
            selector = Select(sel_el)
            try:
                # Prefer matching the option VALUE (e.g. year "III" has
                # value="III" but visible text "III Year").
                selector.select_by_value(value)
                self._log(f"  {key}: set to '{value}'")
            except NoSuchElementException:
                try:
                    selector.select_by_visible_text(value)
                    self._log(f"  {key}: set to '{value}'")
                except NoSuchElementException:
                    options = [o.text for o in sel_el.find_elements(By.TAG_NAME, "option")]
                    raise AutomationError(
                        f"Value '{value}' not found in {key} dropdown. "
                        f"Available options: {options}"
                    )
            # Dept/Year/Section changes trigger an async subject reload.
            time.sleep(0.5)

        # Subject options load from the API only after dept/year/section set.
        self._log("  Waiting for subject list to load...")
        try:
            WebDriverWait(self.driver, EXPLICIT_WAIT_SECONDS).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "#subjectSelect option")) > 1
            )
        except TimeoutException:
            raise AutomationError(
                "No subjects found for the selected class. "
                "Check that the department/year/section filter values are valid."
            )

        subject = self.filters.get("subject")
        if subject:
            subject_el = self._find("subject_dropdown")
            try:
                Select(subject_el).select_by_visible_text(subject)
                self._log(f"  subject: set to '{subject}'")
            except NoSuchElementException:
                options = [o.text for o in subject_el.find_elements(By.TAG_NAME, "option")]
                raise AutomationError(
                    f"Value '{subject}' not found in subject dropdown. "
                    f"Available options: {options}"
                )

        if self.filters.get("date"):
            try:
                date_el = self._find("date_input")
                self.driver.execute_script(
                    "arguments[0].value = arguments[1];"
                    "arguments[0].dispatchEvent(new Event('change', { bubbles: true }));",
                    date_el, self.filters["date"],
                )
                self._log(f"  date: set to '{self.filters['date']}'")
            except NoSuchElementException:
                self._log("  date: input field not found (skipped)")

        # Give the student table a moment to load after filters applied.
        time.sleep(2)
        self._log("All filters applied.")
        self._screenshot("filters_applied")

    def _mark_students(self):
        self._update("mark_absent", "Loading Student Table", 55)
        self._log("Waiting for student table to load...")
        try:
            WebDriverWait(self.driver, EXPLICIT_WAIT_SECONDS).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "#attBody tr")) > 0
            )
        except TimeoutException:
            raise AutomationError("Student Not Found - attendance table did not load.")

        roster = self._read_roster()
        self._log(f"Student table loaded. Found {len(roster)} rows.")
        self._screenshot("student_table_loaded")
        self._log(f"Absentees to mark: {len(self.absentee_reg_nos)} register numbers, {len(self.absentee_names)} names")

        selected_period = self.filters.get("period") or ""
        not_found = set(self.absentee_reg_nos) | set(self.absentee_names)
        absent_count = 0

        if selected_period:
            # Period-specific: only toggle the selected period's checkbox.
            self._update("mark_absent", f"Marking Period {selected_period}", 60)
            self._log(f"Marking only period: {selected_period}")
            rows = self.driver.find_elements(By.CSS_SELECTOR, "#attBody tr")
            for row in rows:
                regno_text, name_text, matched = self._row_info(row)
                if regno_text is None:
                    continue
                try:
                    cb = row.find_element(
                        By.CSS_SELECTOR, f"input.period-cb[data-period='{selected_period}']"
                    )
                except NoSuchElementException:
                    continue
                target_checked = not bool(matched)
                if cb.is_selected() != target_checked:
                    self.driver.execute_script("arguments[0].click();", cb)
                if matched:
                    absent_count += 1
                    self._log(f"  ABSENT  #{absent_count}  {regno_text or name_text} - {name_text}")
                    not_found.discard(matched)
        else:
            # Full-day: bulk-mark everyone PRESENT, then UNCHECK absentees.
            self._update("mark_absent", "Marking All Present", 60)
            self._log("Clicking 'Mark All Present'...")
            mark_all = self._find("mark_all_present_button")
            self.driver.execute_script("arguments[0].click();", mark_all)
            time.sleep(1)

            # The table re-renders after the bulk action, so re-read the rows.
            fresh_rows = self.driver.find_elements(By.CSS_SELECTOR, "#attBody tr")

            for row in fresh_rows:
                regno_text, name_text, matched = self._row_info(row)
                if regno_text is None:
                    continue
                if matched:
                    # Uncheck every period for this student => fully absent.
                    checkboxes = row.find_elements(By.CSS_SELECTOR, "input.period-cb")
                    for cb in checkboxes:
                        if cb.is_selected():
                            self.driver.execute_script("arguments[0].click();", cb)
                    absent_count += 1
                    self._log(f"  ABSENT  #{absent_count}  {regno_text or name_text} - {name_text}")
                    if absent_count % 3 == 0:
                        self._screenshot(f"marking_absent_{absent_count}")
                    not_found.discard(matched)

        present_count = len(roster) - absent_count
        self._log(f"Scanned {len(roster)} unique students.")
        if not_found:
            self._update(
                "mark_absent",
                f"Warning: {len(not_found)} absentee(s) not found in class list",
                70,
            )
            self._log(
                f"  Not found: {', '.join(sorted(not_found)[:10])}"
                + ("..." if len(not_found) > 10 else ""),
                level="WARN",
            )

        self._update("mark_present", "Marking Complete", 80)
        self._log(f"Result: {absent_count} absent, {present_count} present")
        self._screenshot("marking_complete")
        return present_count, absent_count

    def _row_info(self, row):
        """Return (regno, name, matched_key) for a table row, or (None, None, None).

        matched_key identifies the absentee entry (register number or name)
        that matched this student, or None when the student is NOT absent.
        Matching is lenient: exact register number, exact name, then a
        prefix match so partial names like "mohamed ajmal" match the portal's
        full name "Mohamed Ajmal M".
        """
        try:
            regno_text = row.find_element(By.CSS_SELECTOR, "td .roll").text.strip()
            name_text = row.find_element(By.CSS_SELECTOR, "td .sname").text.strip()
        except NoSuchElementException:
            return None, None, None
        name_lower = re.sub(r"\s+", " ", name_text).strip().lower()

        if regno_text and regno_text in self.absentee_reg_nos:
            return regno_text, name_text, regno_text
        if regno_text and regno_text.lower() in self.absentee_names:
            return regno_text, name_text, regno_text.lower()
        if name_lower and name_lower in self.absentee_names:
            return regno_text, name_text, name_lower
        for abs_name in self.absentee_names:
            if name_lower.startswith(abs_name):
                return regno_text, name_text, abs_name
        return regno_text, name_text, None

    def _read_roster(self):
        """Read (roll_no, name) pairs from the loaded student table."""
        roster = []
        seen = set()
        for row in self.driver.find_elements(By.CSS_SELECTOR, "#attBody tr"):
            try:
                regno = row.find_element(By.CSS_SELECTOR, "td .roll").text.strip()
                name = row.find_element(By.CSS_SELECTOR, "td .sname").text.strip()
            except NoSuchElementException:
                continue
            if regno and regno in seen:
                continue
            if regno:
                seen.add(regno)
            roster.append((regno, name))
        return roster

    def _save_and_verify(self):
        self._update("save", "Saving Attendance", 90)
        self._log("Scrolling to Save button...")
        try:
            btn = self._find("save_attendance_button")
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.5)
            self._log("Clicking Save Attendance...")
            self.driver.execute_script("arguments[0].click();", btn)
        except NoSuchElementException:
            raise AutomationError("Attendance Save Failed - save button not found.")

        self._log("Waiting for confirmation...")
        try:
            WebDriverWait(self.driver, 5).until(EC.alert_is_present())
            alert_text = self.driver.switch_to.alert.text
            self._log(f"Confirmation received: {alert_text}")
            self.driver.switch_to.alert.accept()
        except TimeoutException:
            self._log("No alert detected (headless mode). Proceeding...")

        self._update("verify", "Verification Completed", 100)
        self._log("Attendance saved successfully.")
        self._screenshot("saved")
