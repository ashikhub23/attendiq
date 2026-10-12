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
import shutil
import subprocess
import threading
import time
import traceback
import urllib.parse
from datetime import datetime

import requests
from flask import current_app
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, WebDriverException,
    StaleElementReferenceException,
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


# One JS pass over the whole table: unchecks every period checkbox for each
# absentee, then returns {marked, matched:[...keys...]}.  Turns N per-student
# Selenium round-trips into a single execute_script call.
_UNCHECK_ABSENTEES_JS = """
function normName(s) {
  if (!s) return '';
  return s.trim().toLowerCase()
    .replace(/^\\d{1,3}\\s*[.)\\-]\\s*/, '')
    .replace(/\\./g, ' ')
    .replace(/\\s+/g, ' ').trim();
}
var absentees = arguments[0];
var rows = document.querySelectorAll('#attBody tr');
var marked = 0;
var matchedKeys = [];
for (var i = 0; i < rows.length; i++) {
  var row = rows[i];
  var rollEl = row.querySelector('td .roll');
  if (!rollEl) continue;
  var regno = rollEl.textContent.trim();
  var nameEl = row.querySelector('td .sname');
  var name = nameEl ? normName(nameEl.textContent) : '';
  var hit = null;
  for (var j = 0; j < absentees.length; j++) {
    var key = absentees[j];
    if (regno === key) { hit = key; break; }
    if (name === key) { hit = key; break; }
    if (name.indexOf(key) === 0) { hit = key; break; }
  }
  if (hit) {
    var boxes = row.querySelectorAll('input.period-cb');
    for (var k = 0; k < boxes.length; k++) {
      if (boxes[k].checked) boxes[k].click();
    }
    marked++;
    if (matchedKeys.indexOf(hit) === -1) matchedKeys.push(hit);
  }
}
return { marked: marked, matched: matchedKeys };
"""


# Period-specific variant: only toggles the selected period's checkbox for each
# row (check = present, uncheck = absent) in a single execute_script call. The
# portal's own change handler keeps its in-memory state in sync, and it does NOT
# re-render the table on period checkbox changes, so this avoids stale element
# races entirely.
_MARK_PERIOD_JS = """
function normName(s) {
  if (!s) return '';
  return s.trim().toLowerCase()
    .replace(/^\\d{1,3}\\s*[.)\\-]\\s*/, '')
    .replace(/\\./g, ' ')
    .replace(/\\s+/g, ' ').trim();
}
var absentees = arguments[0];
var period = arguments[1];
var rows = document.querySelectorAll('#attBody tr');
var marked = 0;
var matchedKeys = [];
for (var i = 0; i < rows.length; i++) {
  var row = rows[i];
  var rollEl = row.querySelector('td .roll');
  if (!rollEl) continue;
  var regno = rollEl.textContent.trim();
  var nameEl = row.querySelector('td .sname');
  var name = nameEl ? normName(nameEl.textContent) : '';
  var hit = null;
  for (var j = 0; j < absentees.length; j++) {
    var key = absentees[j];
    if (regno === key) { hit = key; break; }
    if (name === key) { hit = key; break; }
    if (name.indexOf(key) === 0) { hit = key; break; }
  }
  var cb = row.querySelector('input.period-cb[data-period="' + period + '"]');
  if (!cb) continue;
  var target = !hit;
  if (cb.checked !== target) { cb.click(); }
  if (hit) {
    marked++;
    if (matchedKeys.indexOf(hit) === -1) matchedKeys.push(hit);
  }
}
return { marked: marked, matched: matchedKeys };
"""


class AttendanceAutomationBot:
    def __init__(self, app, session_id, absentees, filters, headless=True):
        self.app = app
        self.session_id = session_id
        self.absentee_reg_nos = {a["register_number"] for a in absentees if a.get("register_number")}
        self.absentee_names = {
            self._normalize_name(a["name"])
            for a in absentees if a.get("name")
        }
        self.absentee_names.discard("")
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

    def _retry(self, fn, attempts=4, delay=0.4, what="element"):
        """Run fn(), retrying when the portal re-rendered the DOM underneath us
        and invalidated a previously-found element (stale element reference)."""
        last = None
        for i in range(attempts):
            try:
                return fn()
            except StaleElementReferenceException as e:
                last = e
                if i < attempts - 1:
                    self._log(
                        f"  {what} re-rendered; retrying ({i + 1}/{attempts})...",
                        level="WARN",
                    )
                    time.sleep(delay)
        raise last

    def _wait_for_table_stable(self, timeout=None):
        """Wait until the student table stops changing. The portal fires several
        async reloads (one per filter change), each rebuilding #attBody, so we
        wait for two identical consecutive snapshots before reading it."""
        timeout = timeout or EXPLICIT_WAIT_SECONDS
        deadline = time.time() + timeout
        script = (
            "var b = document.querySelector('#attBody');"
            "if (!b) return null;"
            "return b.children.length + '|' + "
            "  ((b.querySelector('td .roll') || {}).textContent || '') + '|' + "
            "  ((b.querySelector('td .sname') || {}).textContent || '');"
        )
        last = None
        stable = 0
        while time.time() < deadline:
            try:
                sig = self.driver.execute_script(script)
            except WebDriverException:
                return
            if sig and sig == last:
                stable += 1
                if stable >= 2:
                    return
            else:
                stable = 0
            last = sig
            time.sleep(0.35)

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
        chrome_major = self._installed_chrome_version()
        if chrome_major:
            self._log(f"Detected installed Chrome major version: {chrome_major}")

        service = self._resolve_driver_service(chrome_major)
        options.page_load_strategy = "eager"

        try:
            self.driver = webdriver.Chrome(service=service, options=options)
        except WebDriverException as exc:
            # A stale/ mismatched local driver was used — let Selenium Manager
            # (built into Selenium >= 4.6) fetch a matching one and retry.
            if service is not None:
                self._log(
                    "Chrome launch failed with the local driver; retrying with "
                    f"Selenium Manager. ({str(exc)[:140]})"
                )
                self.driver = webdriver.Chrome(options=options)
            else:
                raise

        self.driver.implicitly_wait(3)
        self._log("Browser ready.")
        self._screenshot("browser_open")

    def _resolve_driver_service(self, chrome_major):
        """Return a Service for a ChromeDriver that matches the installed
        Chrome, downloading one if necessary. Returns None to let Selenium
        Manager resolve it automatically (last resort)."""
        try:
            path = self._find_local_chromedriver(chrome_major)
            self._log(f"Using local ChromeDriver: {path}")
            return Service(path)
        except ChromeDriverNotFoundError:
            pass
        try:
            self._log("No matching local ChromeDriver; downloading via webdriver-manager...")
            path = ChromeDriverManager().install()
            self._log(f"ChromeDriver ready: {path}")
            return Service(path)
        except Exception as exc:
            self._log(
                f"webdriver-manager unavailable ({str(exc)[:120]}); "
                "letting Selenium Manager resolve the driver."
            )
            return None

    def _installed_chrome_version(self):
        """Return the installed Chrome/Chromium major version, or None."""
        if os.name == "nt":
            try:
                import winreg
                for hive, key in (
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Google\Chrome\BLBeacon"),
                    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Google\Chrome\BLBeacon"),
                ):
                    try:
                        with winreg.OpenKey(hive, key) as k:
                            version, _ = winreg.QueryValueEx(k, "version")
                            return int(str(version).split(".")[0])
                    except OSError:
                        continue
            except Exception:
                pass

        for exe in self._chrome_binary_candidates():
            try:
                out = subprocess.run(
                    [exe, "--version"], capture_output=True, text=True, timeout=10
                )
                match = re.search(r"(\d+)\.", out.stdout or "")
                if match:
                    return int(match.group(1))
            except Exception:
                continue
        return None

    def _chrome_binary_candidates(self):
        candidates = []
        if os.name == "nt":
            local = os.environ.get("LOCALAPPDATA", "")
            candidates += [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
            ]
        else:
            for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
                path = shutil.which(name)
                if path:
                    candidates.append(path)
        return [c for c in candidates if c and os.path.isfile(c)]

    @staticmethod
    def _chromedriver_major(path):
        """Parse the major version of a ChromeDriver binary."""
        try:
            out = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=10
            )
            match = re.search(r"ChromeDriver\s+(\d+)\.", out.stdout or "")
            if match:
                return int(match.group(1))
        except Exception:
            pass
        # Fall back to the version embedded in the cache path, e.g.
        # ~/.wdm/drivers/chromedriver/win64/154.0.xxxx/chromedriver-win64/...
        match = re.search(r"[\\/](\d+)\.\d+\.\d+[\\/]", path)
        return int(match.group(1)) if match else None

    def _find_local_chromedriver(self, required_major=None):
        """Locate a ChromeDriver already on disk that matches the installed
        Chrome (by major version) so we never download a mismatched one."""
        candidates = [
            os.path.join(self.app.root_path, "..", "drivers", "chromedriver.exe"),
            os.path.join(os.getcwd(), "drivers", "chromedriver.exe"),
        ]
        cache_root = os.path.join(os.path.expanduser("~"), ".wdm", "drivers")
        if os.path.isdir(cache_root):
            candidates += sorted(
                glob.glob(os.path.join(cache_root, "**", "chromedriver*"), recursive=True),
                reverse=True,
            )
        on_path = shutil.which("chromedriver")
        if on_path:
            candidates.append(on_path)

        for path in candidates:
            if not path or not os.path.isfile(path):
                continue
            if required_major is None:
                return path
            major = self._chromedriver_major(path)
            if major == required_major:
                return path
            self._log(
                f"Skipping ChromeDriver v{major} (needs v{required_major}): {path}"
            )
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

        time.sleep(0.3)
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
            self._select_dropdown_value(f"{key}_dropdown", value)
            self._log(f"  {key}: set to '{value}'")
            # Dept/Year/Section changes trigger an async subject reload.
            time.sleep(0.1)

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
            try:
                self._select_dropdown_value("subject_dropdown", subject)
            except StaleElementReferenceException:
                # Reloads can swap the option list mid-selection; retry once.
                time.sleep(0.5)
                self._select_dropdown_value("subject_dropdown", subject)
            self._log(f"  subject: set to '{subject}'")

        if self.filters.get("date"):
            self._set_date_input(self.filters["date"])
            self._log(f"  date: set to '{self.filters['date']}'")

        # The portal fires an async reload for every filter change; wait for the
        # resulting re-renders to settle before touching the table.
        self._wait_for_table_stable()
        self._log("All filters applied.")
        self._screenshot("filters_applied")

    def _select_dropdown_value(self, key, value):
        """Select an option in a <select>, retrying if the dropdown re-renders
        underneath us (stale element)."""
        def _do():
            try:
                sel_el = self._find(key)
            except NoSuchElementException:
                sel_el = None
            if sel_el is None:
                raise AutomationError(f"{key.replace('_dropdown', '')} dropdown not found.")
            selector = Select(sel_el)
            try:
                selector.select_by_value(value)
            except NoSuchElementException:
                try:
                    selector.select_by_visible_text(value)
                except NoSuchElementException:
                    options = [o.text for o in sel_el.find_elements(By.TAG_NAME, "option")]
                    raise AutomationError(
                        f"Value '{value}' not found in {key} dropdown. "
                        f"Available options: {options}"
                    )
        self._retry(_do, what=f"{key} dropdown")

    def _set_date_input(self, value):
        def _do():
            date_el = self._find("date_input")
            if date_el is None:
                self._log("  date: input field not found (skipped)")
                return
            self.driver.execute_script(
                "arguments[0].value = arguments[1];"
                "arguments[0].dispatchEvent(new Event('change', { bubbles: true }));",
                date_el, value,
            )
        self._retry(_do, what="date input")

    def _mark_students(self):
        self._update("mark_absent", "Loading Student Table", 55)
        self._log("Waiting for student table to load...")
        try:
            # Wait for REAL student rows (the portal renders a loading row in
            # #attBody that has no .roll cell - matching that would make us
            # start marking before the table is ready).
            WebDriverWait(self.driver, EXPLICIT_WAIT_SECONDS).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "#attBody tr td .roll")) > 0
            )
        except TimeoutException:
            raise AutomationError("Student Not Found - attendance table did not load.")

        # The portal fires an async reload per filter change; wait until the
        # table stops rebuilding so rows don't go stale while we read them.
        self._wait_for_table_stable()

        roster = self._read_roster()
        self._log(f"Student table loaded. Found {len(roster)} rows.")
        self._screenshot("student_table_loaded")
        self._log(f"Absentees to mark: {len(self.absentee_reg_nos)} register numbers, {len(self.absentee_names)} names")

        selected_period = self.filters.get("period") or ""
        not_found = set(self.absentee_reg_nos) | set(self.absentee_names)
        absent_count = 0

        if selected_period:
            # Period-specific: one JS pass toggles only the selected period's
            # checkbox per row (checked = present, unchecked = absent).
            self._update("mark_absent", f"Marking Period {selected_period}", 60)
            self._log(f"Marking only period: {selected_period}")
            absent_keys = sorted(self.absentee_reg_nos) + sorted(self.absentee_names)
            result = self._retry(
                lambda: self.driver.execute_script(
                    _MARK_PERIOD_JS, absent_keys, selected_period
                ),
                what="period marking",
            )
            absent_count = result.get("marked", 0)
            for key in result.get("matched", []):
                not_found.discard(key)
            self._log(f"  ABSENT marked: {absent_count}")
        else:
            # Full-day: bulk-mark everyone PRESENT, then UNCHECK absentees.
            self._update("mark_absent", "Marking All Present", 60)
            self._log("Clicking 'Mark All Present'...")
            self._retry(
                lambda: self.driver.execute_script(
                    "arguments[0].click();", self._find("mark_all_present_button")
                ),
                what="mark-all button",
            )
            time.sleep(0.3)

            # Build the absentee key list (register numbers + normalized names).
            absent_keys = sorted(self.absentee_reg_nos) + sorted(self.absentee_names)
            result = self._retry(
                lambda: self.driver.execute_script(_UNCHECK_ABSENTEES_JS, absent_keys),
                what="absentee marking",
            )
            absent_count = result.get("marked", 0)
            for key in result.get("matched", []):
                not_found.discard(key)
            self._log(f"  ABSENT marked: {absent_count}")

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

    @staticmethod
    def _normalize_name(raw):
        """Normalize a name for lenient matching: lowercase, strip leading list
        numbers ("1. Athithi" -> "athithi"), dots to spaces ("nandhini.b" ->
        "nandhini b"), and collapse whitespace."""
        if not raw:
            return ""
        n = re.sub(r"^\d{1,3}\s*[.)\-]\s*", "", str(raw).strip().lower())
        n = n.replace(".", " ")
        n = re.sub(r"\s+", " ", n).strip()
        return n

    def _read_roster(self):
        """Read (roll_no, name) pairs from the loaded student table, retrying if
        the portal rebuilds the table mid-read."""
        def _read():
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
        return self._retry(_read, what="student table")

    def _save_and_verify(self):
        self._update("save", "Saving Attendance", 90)
        self._log("Scrolling to Save button...")
        try:
            self._retry(
                lambda: self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});",
                    self._find("save_attendance_button"),
                ),
                what="save button",
            )
            time.sleep(0.2)
        except NoSuchElementException:
            raise AutomationError("Attendance Save Failed - save button not found.")

        meta = {
            "department": self.filters.get("department") or "",
            "year": self.filters.get("year") or "",
            "section": self.filters.get("section") or "",
            "subject": self.filters.get("subject") or "",
            "date": self.filters.get("date") or "",
        }
        supabase = self._portal_headers()

        verified = False
        for attempt in range(1, 4):
            self._log(f"Clicking Save Attendance (attempt {attempt}/3)...")
            try:
                self._retry(
                    lambda: self.driver.execute_script(
                        "arguments[0].click();", self._find("save_attendance_button")
                    ),
                    what="save button",
                )
            except WebDriverException as e:
                raise AutomationError(f"Attendance Save Failed - could not click save: {str(e)[:200]}")

            # Accept the JS confirm() (shown when some students have no period
            # marked as present).
            try:
                WebDriverWait(self.driver, 5).until(EC.alert_is_present())
                alert_text = self.driver.switch_to.alert.text
                self._log(f"Confirmation received: {alert_text}")
                self.driver.switch_to.alert.accept()
            except TimeoutException:
                self._log("No alert detected; waiting for save to complete...")

            if supabase is not None:
                # Authoritative check: the record must actually exist in the
                # portal's Supabase backend (the portal saves asynchronously,
                # so the browser must stay alive until it lands).
                deadline = time.time() + 15
                while time.time() < deadline:
                    record, entries = self._fetch_portal_record(supabase, meta)
                    if record:
                        verified = True
                        self._log(
                            f"Save verified in portal: record {record.get('id')} "
                            f"with {len(entries)} entries."
                        )
                        break
                    time.sleep(1.0)
            else:
                # No portal API configured - fall back to the portal's own UI
                # signals (saved banner / success toast).
                def _ui_saved(d):
                    try:
                        banner = d.find_element(By.ID, "savedBanner").get_attribute("innerHTML") or ""
                        toast = d.find_element(By.ID, "toastStack").get_attribute("innerHTML") or ""
                    except (NoSuchElementException, StaleElementReferenceException):
                        return False
                    return "saved" in banner.lower() or "attendance saved" in toast.lower()

                try:
                    WebDriverWait(self.driver, 15).until(_ui_saved)
                    verified = True
                    self._log("Save confirmed by the portal UI.")
                except (TimeoutException, NoSuchElementException):
                    self._log(f"Portal did not confirm the save (attempt {attempt}/3).")

            if verified:
                break
            if attempt < 3:
                self._log("Save not confirmed; retrying...")

        if not verified:
            raise AutomationError(
                "Attendance Save Failed - the portal did not save the record. "
                "Please retry from the progress page."
            )

        self._update("verify", "Verification Completed", 100)
        self._log("Attendance saved and verified successfully.")

    def _portal_headers(self):
        """Return (supabase_url, anon_key, access_token) for the portal's
        Supabase backend, or None when not configured. The HOD login is reused
        to get an authenticated token so RLS policies allow reads."""
        with self.app.app_context():
            url = current_app.config.get("PORTAL_SUPABASE_URL", "")
            anon = current_app.config.get("PORTAL_SUPABASE_ANON_KEY", "")
            p_user = current_app.config.get("PORTAL_USERNAME", "")
            p_pass = current_app.config.get("PORTAL_PASSWORD", "")
        if not (url and anon and p_user and p_pass):
            return None
        try:
            resp = requests.post(
                f"{url}/auth/v1/token?grant_type=password",
                json={"email": p_user, "password": p_pass},
                headers={"apikey": anon, "Content-Type": "application/json"},
                timeout=20,
            )
            token = resp.json().get("access_token")
            if not token:
                self._log("Portal API login failed for save verification.", level="WARN")
                return None
            return (url, anon, token)
        except Exception as e:
            self._log(
                f"Could not create portal API session for verification: {str(e)[:150]}",
                level="WARN",
            )
            return None

    def _fetch_portal_record(self, supabase, meta):
        """Fetch the attendance_records row + entries for the given class/date."""
        url, anon, token = supabase
        headers = {"apikey": anon, "Authorization": f"Bearer {token}"}
        query = "&".join(
            f"{k}=eq.{urllib.parse.quote(str(v))}" for k, v in meta.items() if v
        )
        try:
            resp = requests.get(
                f"{url}/rest/v1/attendance_records?{query}", headers=headers, timeout=20
            )
            data = resp.json()
        except Exception:
            return None, []
        if not isinstance(data, list) or not data:
            return None, []
        record = data[0]
        record_id = record.get("id")
        entries = []
        if record_id:
            try:
                resp2 = requests.get(
                    f"{url}/rest/v1/attendance_entries?record_id=eq.{record_id}",
                    headers=headers,
                    timeout=20,
                )
                raw = resp2.json()
                entries = raw if isinstance(raw, list) else []
            except Exception:
                entries = []
        return record, entries
        self._screenshot("saved")
