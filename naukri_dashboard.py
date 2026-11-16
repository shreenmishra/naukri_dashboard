#!/usr/bin/env python3
"""
Naukri Multi-User Automation Web App & Scheduler for Windows (Port 8080)
========================================================================
Self-hosted web application on your Windows laptop where users can:
1. Register/update their Naukri profile (Email, Password, Run Frequency, CTC, Keywords).
2. Upload their PDF/DOCX Resume directly through the browser.
3. Run invisibly in the background on Windows WITHOUT triggering Akamai 'Access Denied'
   by using Patchright (CDP-patched Playwright) + genuine installed Chrome (`channel="chrome"`)
   with a persistent profile.

Setup:
    pip install patchright
    python naukri_dashboard-v3.py --port 8080
Then open in browser:
    http://localhost:8080
"""

import os
import sys
import time
import sqlite3
import threading
import traceback
import argparse
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse, quote_plus
from email.parser import BytesParser
from email.policy import default as default_email_policy

HAS_PLAYWRIGHT = False
USING_PATCHRIGHT = False
try:
    from patchright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
    USING_PATCHRIGHT = True
except ImportError:
    try:
        from playwright.sync_api import sync_playwright
        HAS_PLAYWRIGHT = True
    except ImportError:
        HAS_PLAYWRIGHT = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "naukri_app.db")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")

os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

NAUKRI_HOME_URL = "https://www.naukri.com/"
NAUKRI_LOGIN_URL = "https://login.naukri.com/nLogin/Login.php"
NAUKRI_PROFILE_URL = "https://www.naukri.com/mnjuser/profile"

running_profiles = set()
db_lock = threading.Lock()


# ==============================================================================
# Database Setup & Helpers
# ==============================================================================
def init_db():
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                naukri_email TEXT UNIQUE NOT NULL,
                naukri_password TEXT NOT NULL,
                frequency TEXT DEFAULT '1h',
                update_mode TEXT DEFAULT 'both',
                browser_mode TEXT DEFAULT 'invisible_background',
                auto_apply INTEGER DEFAULT 0,
                max_apply INTEGER DEFAULT 5,
                target_keywords TEXT DEFAULT 'Staff SDET Python Playwright, SDET Lead Playwright AI',
                location TEXT DEFAULT 'Bengaluru',
                exp_years TEXT DEFAULT '10',
                current_ctc TEXT DEFAULT '30',
                expected_ctc TEXT DEFAULT '42',
                notice_period TEXT DEFAULT '30',
                resume_path TEXT DEFAULT '',
                resume_filename TEXT DEFAULT '',
                last_run_at TEXT DEFAULT 'Never',
                next_run_at TEXT DEFAULT '',
                last_status TEXT DEFAULT 'Idle',
                last_log TEXT DEFAULT 'Profile registered. Ready for run.'
            )
        """)
        try:
            conn.execute("ALTER TABLE profiles ADD COLUMN browser_mode TEXT DEFAULT 'invisible_background'")
        except Exception:
            pass

        conn.execute("""
            CREATE TABLE IF NOT EXISTS applied_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER,
                naukri_email TEXT,
                timestamp TEXT,
                job_title TEXT,
                company TEXT,
                location TEXT,
                salary TEXT,
                url TEXT
            )
        """)
        conn.commit()


def compute_next_run(frequency, from_dt=None):
    now = from_dt or datetime.now()
    if frequency == "1h":
        nxt = now + timedelta(hours=1)
    elif frequency == "2h":
        nxt = now + timedelta(hours=2)
    elif frequency == "4h":
        nxt = now + timedelta(hours=4)
    elif frequency == "6h":
        nxt = now + timedelta(hours=6)
    elif frequency == "9am_1pm":
        candidates = [
            now.replace(hour=9, minute=0, second=0, microsecond=0),
            now.replace(hour=13, minute=0, second=0, microsecond=0),
            (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0),
        ]
        nxt = next(c for c in candidates if c > now)
    elif frequency == "daily_9am":
        candidate = now.replace(hour=9, minute=0, second=0, microsecond=0)
        nxt = candidate if candidate > now else candidate + timedelta(days=1)
    else:
        return "Paused"
    return nxt.strftime("%Y-%m-%d %H:%M:%S")


def append_profile_log(profile_id, message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    clean_msg = " ".join(str(message).splitlines()).strip()
    line = f"[{timestamp}] {clean_msg}"
    print(f"[Profile #{profile_id}] {clean_msg}")
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT last_log FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        existing = row[0] if row and row[0] else ""
        lines = [ln for ln in existing.splitlines() if ln.strip()]
        lines.append(line)
        lines = lines[-60:]
        conn.execute("UPDATE profiles SET last_log = ? WHERE id = ?", ("\n".join(lines), profile_id))
        conn.commit()


def update_profile_status(profile_id, status, last_run=None, next_run=None):
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        if last_run and next_run:
            conn.execute(
                "UPDATE profiles SET last_status = ?, last_run_at = ?, next_run_at = ? WHERE id = ?",
                (status, last_run, next_run, profile_id)
            )
        else:
            conn.execute("UPDATE profiles SET last_status = ? WHERE id = ?", (status, profile_id))
        conn.commit()


# ==============================================================================
# Akamai-Safe Headless / Invisible Background Chrome Launcher
# ==============================================================================
def launch_akamai_safe_browser(playwright, profile_id, browser_mode="invisible_background"):
    """
    Launches genuine installed Google Chrome (or Edge) with Patchright's
    CDP-leak fixes and clean persistent session storage.
    """
    user_data_dir = os.path.join(SESSIONS_DIR, f"chrome_profile_{profile_id}")
    os.makedirs(user_data_dir, exist_ok=True)

    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if browser_mode == "headless_new":
        args.extend(["--headless=new", "--window-size=1366,768"])
    elif browser_mode == "invisible_background":
        args.extend(["--window-position=-32000,-32000", "--window-size=1366,768"])
    else:
        args.append("--start-maximized")

    for channel in ["chrome", "msedge", None]:
        try:
            ctx_kwargs = {
                "user_data_dir": user_data_dir,
                "headless": False,
                "args": args,
                "no_viewport": True,
                "locale": "en-IN",
                "timezone_id": "Asia/Kolkata",
            }
            if channel:
                ctx_kwargs["channel"] = channel

            context = playwright.chromium.launch_persistent_context(**ctx_kwargs)
            return context, (channel or "chromium")
        except Exception:
            continue

    raise RuntimeError("Could not launch Chrome, Edge, or Chromium.")


def check_akamai_block(page, step_name=""):
    content_lower = page.content().lower()
    if "access denied" in content_lower or "errors.edgesuite.net" in content_lower:
        raise PermissionError(
            f"Akamai WAF returned Access Denied ({step_name}). Ensure 'patchright' is installed, "
            "run once in 'Visible Chrome Window' mode, and turn off any VPN."
        )


def dismiss_popups(page):
    selectors = [
        "span.crossIcon", "div.crossIcon", "button.drawer-close",
        ".nI-gNb-drawer__close", "div[data-testid='close-icon']",
        "button:has-text('Later')", "button:has-text('Maybe Later')", "span:has-text('Skip')"
    ]
    for sel in selectors:
        try:
            if page.locator(sel).first.is_visible():
                page.locator(sel).first.click(timeout=1500)
                time.sleep(0.4)
        except Exception:
            pass


def login_to_naukri(page, pid, email, password):
    """Logs into Naukri via the homepage right-side login drawer (with direct URL fallback)."""
    append_profile_log(pid, "Connecting to Naukri homepage to check session...")
    page.goto(NAUKRI_HOME_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(3.5)
    check_akamai_block(page, "homepage")

    logged_in_indicators = [
        "a[href*='mnjuser/profile']",
        "div.nI-gNb-drawer__bars",
        "img.nI-gNb-icon-img",
        "a:has-text('View profile')",
    ]
    for sel in logged_in_indicators:
        try:
            if page.locator(sel).first.is_visible():
                append_profile_log(pid, "Active Naukri session detected. Skipping login.")
                return
        except Exception:
            pass

    append_profile_log(pid, "Clicking Login button on Naukri homepage...")
    login_btn = page.locator("a#login_Layer").first
    try:
        login_btn.wait_for(state="visible", timeout=15000)
        login_btn.click()
        time.sleep(2.5)
    except Exception:
        page.goto(NAUKRI_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2.5)

    check_akamai_block(page, "login drawer")

    drawer = page.locator("div.naukri-drawer.open")
    if drawer.first.is_visible():
        append_profile_log(pid, "Login drawer opened. Entering credentials...")
        user_input = drawer.locator("input[placeholder='Enter your active Email ID / Username']").first
        pass_input = drawer.locator("input[placeholder='Enter your password']").first

        user_input.wait_for(state="visible", timeout=10000)
        user_input.click()
        user_input.fill("")
        user_input.type(email, delay=65)
        time.sleep(0.7)

        pass_input.click()
        pass_input.fill("")
        pass_input.type(password, delay=65)
        time.sleep(0.7)

        submit_btn = drawer.locator("button[type='submit'], button.loginButton").first
        if not submit_btn.is_visible():
            submit_btn = drawer.locator("button").filter(has_text="Login").first
        submit_btn.click()
    else:
        append_profile_log(pid, "Navigating to direct login page...")
        if "login" not in page.url.lower():
            page.goto(NAUKRI_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2.5)
        user_input = page.locator("#usernameField, input[placeholder*='Email'], input[placeholder*='Username']").first
        pass_input = page.locator("#passwordField, input[type='password'], input[placeholder*='Password']").first

        user_input.wait_for(state="visible", timeout=15000)
        user_input.click()
        user_input.type(email, delay=65)
        time.sleep(0.7)

        pass_input.click()
        pass_input.type(password, delay=65)
        time.sleep(0.7)

        page.locator("button[type='submit'], button.loginButton, button:has-text('Login')").first.click()

    append_profile_log(pid, "Credentials submitted. Waiting for authentication...")
    time.sleep(6)

    otp_field = page.locator("input[placeholder*='OTP'], input[maxlength='6']").first
    if "login" in page.url.lower() or otp_field.is_visible():
        append_profile_log(pid, "OTP/Verification prompt detected! Complete it in 'Visible Chrome Window' (waiting 45s)...")
        for _ in range(45):
            time.sleep(1)
            if "mnjuser" in page.url or page.locator("a[href*='mnjuser/profile']").first.is_visible():
                break

    append_profile_log(pid, "Login step complete.")


def update_resume_headline(page, pid):
    """Opens the Resume Headline popup modal, toggles trailing period, and clicks Save."""
    append_profile_log(pid, "Updating Resume Headline...")
    headline_card = page.locator("div.resumeHeadline, div.widgetHead:has-text('Resume headline')").first
    headline_card.wait_for(state="visible", timeout=20000)
    headline_card.scroll_into_view_if_needed()
    time.sleep(1)

    edit_btn = page.locator(
        "div.resumeHeadline span.edit.icon, "
        "div.resumeHeadline span:has-text('Edit'), "
        "span:has-text('Resume headline') ~ span.edit"
    ).first
    edit_btn.click()
    time.sleep(2)

    # Target the modal textarea (rendered as a top-level lightbox modal outside div.resumeHeadline)
    textarea = page.locator(
        "div.lightbox.model_open textarea, "
        "div[role='dialog'] textarea, "
        "textarea#resumeHeadlineTxt, "
        "form textarea"
    ).first
    textarea.wait_for(state="visible", timeout=15000)

    curr = textarea.input_value().strip()
    updated = curr[:-1].strip() if curr.endswith(".") else curr + "."

    textarea.click()
    textarea.fill(updated)
    # Trigger React input/change events so Naukri's modal enables and registers the Save state
    textarea.evaluate("""el => {
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }""")
    time.sleep(1.2)

    # Locate the Save button inside the active modal dialog
    modal_save_selectors = [
        "div.lightbox.model_open button:has-text('Save')",
        "div[role='dialog'] button:has-text('Save')",
        "form:has(textarea#resumeHeadlineTxt) button:has-text('Save')",
        "form:has(textarea) button[type='submit']:has-text('Save')",
        "button.btn-dark-ot:has-text('Save')",
        "button:has-text('Save')"
    ]

    clicked_save = False
    for sel in modal_save_selectors:
        btn = page.locator(sel).last
        try:
            if btn.is_visible():
                btn.scroll_into_view_if_needed()
                btn.click(timeout=5000)
                clicked_save = True
                break
        except Exception:
            continue

    if not clicked_save:
        raise RuntimeError("Could not locate or click the Save button inside the Resume Headline modal.")

    # Wait for modal to close to confirm save succeeded
    try:
        textarea.wait_for(state="hidden", timeout=10000)
    except Exception:
        time.sleep(2.5)

    append_profile_log(pid, "Resume Headline updated & saved successfully!")


def execute_naukri_run_for_profile(profile):
    """Runs the Naukri profile update & optional job applier."""
    pid = profile["id"]
    if pid in running_profiles:
        return
    running_profiles.add(pid)

    email = profile["naukri_email"]
    password = profile["naukri_password"]
    mode = profile["update_mode"]
    browser_mode = profile.get("browser_mode") or "invisible_background"
    resume_path = profile["resume_path"]
    auto_apply = bool(profile["auto_apply"])
    max_apply = int(profile["max_apply"] or 5)

    update_profile_status(pid, "Running...")
    engine_label = "patchright" if USING_PATCHRIGHT else "playwright"
    append_profile_log(pid, f"Starting run for {email} (engine={engine_label}, mode={mode}, browser={browser_mode}, auto_apply={auto_apply})")

    if not HAS_PLAYWRIGHT:
        append_profile_log(pid, "ERROR: Patchright/Playwright not installed. Run: pip install patchright")
        update_profile_status(pid, "Failed (Driver missing)")
        running_profiles.discard(pid)
        return

    context = None
    try:
        with sync_playwright() as p:
            context, used_channel = launch_akamai_safe_browser(p, pid, browser_mode=browser_mode)
            append_profile_log(pid, f"Launched {used_channel} via {engine_label} in '{browser_mode}' mode")

            page = context.pages[0] if context.pages else context.new_page()

            # 1. Authenticate via Naukri homepage login drawer first
            login_to_naukri(page, pid, email, password)

            # 2. Navigate to Profile page after login
            append_profile_log(pid, "Opening Naukri profile page...")
            page.goto(NAUKRI_PROFILE_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)
            check_akamai_block(page, "profile page")
            dismiss_popups(page)

            # 3. Update Resume Headline (Modal Popup)
            if mode in ("headline", "both"):
                update_resume_headline(page, pid)

            # 4. Re-upload Resume file if present
            if mode in ("resume", "both") and resume_path and os.path.exists(resume_path):
                append_profile_log(pid, f"Uploading resume: {os.path.basename(resume_path)}...")
                page.goto(NAUKRI_PROFILE_URL, wait_until="domcontentloaded", timeout=30000)
                time.sleep(3)
                dismiss_popups(page)
                file_input = page.locator("input[type='file']#attachCV, input[type='file']").first
                file_input.set_input_files(os.path.abspath(resume_path))
                time.sleep(5)
                append_profile_log(pid, "Resume uploaded successfully!")

            # 5. Optional Job Auto-Apply
            if auto_apply:
                queries = [q.strip() for q in (profile["target_keywords"] or "").split(",") if q.strip()]
                applied_count = 0
                for q in queries:
                    if applied_count >= max_apply:
                        break
                    slug = q.lower().replace(" ", "-")
                    loc = (profile["location"] or "bengaluru").lower()
                    exp = profile["exp_years"] or "10"
                    search_url = f"https://www.naukri.com/{slug}-jobs-in-{loc}?k={quote_plus(q)}&l={quote_plus(loc)}&experience={exp}"
                    append_profile_log(pid, f"Searching jobs for '{q}' in {loc}...")
                    page.goto(search_url, wait_until="domcontentloaded")
                    time.sleep(4)
                    dismiss_popups(page)

                    cards = page.locator("div.srp-jobtuple-wrapper, article.jobTuple").all()
                    for card in cards:
                        if applied_count >= max_apply:
                            break
                        try:
                            title_el = card.locator("a.title").first
                            comp_el = card.locator("a.comp-name, a.subTitle").first
                            sal_el = card.locator("span.sal, li.salary").first
                            if not title_el.is_visible():
                                continue
                            j_title = title_el.inner_text().strip()
                            j_comp = comp_el.inner_text().strip() if comp_el.is_visible() else "Company"
                            j_sal = sal_el.inner_text().strip() if sal_el.is_visible() else "Not Disclosed"
                            j_url = title_el.get_attribute("href")
                            if not j_url:
                                continue

                            jp = context.new_page()
                            jp.goto(j_url, wait_until="domcontentloaded")
                            time.sleep(3)
                            apply_btn = jp.locator("button#apply-button, button:has-text('Apply')").first
                            already = jp.locator("span#already-applied, button:has-text('Applied')").first
                            if not already.is_visible() and apply_btn.is_visible():
                                apply_btn.click()
                                time.sleep(2.5)
                                applied_count += 1
                                with db_lock, sqlite3.connect(DB_PATH) as conn:
                                    conn.execute(
                                        "INSERT INTO applied_jobs (profile_id, naukri_email, timestamp, job_title, company, location, salary, url) "
                                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                        (pid, email, datetime.now().strftime("%Y-%m-%d %H:%M"), j_title, j_comp, profile["location"], j_sal, j_url)
                                    )
                                    conn.commit()
                                append_profile_log(pid, f"Applied to: {j_title} at {j_comp}")
                            jp.close()
                        except Exception:
                            pass

            context.close()

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        next_str = compute_next_run(profile["frequency"])
        update_profile_status(pid, "Success", last_run=now_str, next_run=next_str)
        append_profile_log(pid, f"Run completed! Next scheduled run: {next_str}")

    except Exception as e:
        if context:
            try:
                context.close()
            except Exception:
                pass
        append_profile_log(pid, f"Error: {e}")
        append_profile_log(pid, traceback.format_exc().splitlines()[-1])
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        next_str = compute_next_run(profile["frequency"])
        update_profile_status(pid, "Failed (See Log)", last_run=now_str, next_run=next_str)
    finally:
        running_profiles.discard(pid)


# ==============================================================================
# Background Scheduler Daemon
# ==============================================================================
def scheduler_loop():
    while True:
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with db_lock, sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM profiles WHERE frequency != 'paused' AND next_run_at != '' AND next_run_at != 'Paused' AND next_run_at <= ?",
                    (now_str,)
                ).fetchall()
                due_profiles = [dict(r) for r in rows]

            for prof in due_profiles:
                if prof["id"] not in running_profiles:
                    nxt = compute_next_run(prof["frequency"])
                    with db_lock, sqlite3.connect(DB_PATH) as conn:
                        conn.execute("UPDATE profiles SET next_run_at = ? WHERE id = ?", (nxt, prof["id"]))
                        conn.commit()
                    threading.Thread(target=execute_naukri_run_for_profile, args=(prof,), daemon=True).start()
        except Exception as e:
            print(f"[Scheduler Error] {e}")
        time.sleep(30)


def parse_multipart_form(headers, body_bytes):
    content_type = headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        text = body_bytes.decode("utf-8", errors="replace")
        parsed = parse_qs(text)
        return {k: v[0] for k, v in parsed.items()}, {}

    raw_message = (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode("utf-8") + body_bytes
    msg = BytesParser(policy=default_email_policy).parsebytes(raw_message)

    fields, files = {}, {}
    for part in msg.iter_parts():
        cd = part.get("Content-Disposition", "")
        if "form-data" not in cd:
            continue
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files[name] = {"filename": filename, "content": payload}
        elif name:
            fields[name] = payload.decode("utf-8", errors="replace").strip()
    return fields, files


class NaukriWebAppHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/":
            selected_log_id = query.get("log_id", [None])[0]
            edit_id = query.get("edit_id", [None])[0]
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(self.render_page(selected_log_id, edit_id).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length) if length > 0 else b""
        fields, files = parse_multipart_form(self.headers, body_bytes)

        if parsed.path == "/save_profile":
            full_name = fields.get("full_name", "Candidate")
            email = fields.get("naukri_email", "").strip()
            password = fields.get("naukri_password", "").strip()
            frequency = fields.get("frequency", "1h")
            update_mode = fields.get("update_mode", "both")
            browser_mode = fields.get("browser_mode", "invisible_background")
            auto_apply = 1 if fields.get("auto_apply") == "1" else 0
            max_apply = int(fields.get("max_apply", "5") or 5)
            target_keywords = fields.get("target_keywords", "Staff SDET Python Playwright, SDET Lead Playwright AI")
            location = fields.get("location", "Bengaluru")
            exp_years = fields.get("exp_years", "10")
            current_ctc = fields.get("current_ctc", "30")
            expected_ctc = fields.get("expected_ctc", "42")
            notice_period = fields.get("notice_period", "30")

            if email:
                resume_path, resume_filename = "", ""
                if "resume_file" in files and files["resume_file"]["filename"] and len(files["resume_file"]["content"]) > 0:
                    orig_name = os.path.basename(files["resume_file"]["filename"])
                    safe_email = "".join(c if c.isalnum() else "_" for c in email)
                    resume_path = os.path.join(UPLOADS_DIR, f"{safe_email}_{orig_name}")
                    with open(resume_path, "wb") as f:
                        f.write(files["resume_file"]["content"])
                    resume_filename = orig_name

                next_run = compute_next_run(frequency)
                with db_lock, sqlite3.connect(DB_PATH) as conn:
                    conn.row_factory = sqlite3.Row
                    existing = conn.execute("SELECT * FROM profiles WHERE naukri_email = ?", (email,)).fetchone()
                    if existing:
                        final_pass = password if password else existing["naukri_password"]
                        final_rpath = resume_path if resume_path else existing["resume_path"]
                        final_rname = resume_filename if resume_filename else existing["resume_filename"]
                        conn.execute("""
                            UPDATE profiles SET
                                full_name=?, naukri_password=?, frequency=?, update_mode=?, browser_mode=?,
                                auto_apply=?, max_apply=?, target_keywords=?, location=?,
                                exp_years=?, current_ctc=?, expected_ctc=?, notice_period=?,
                                resume_path=?, resume_filename=?, next_run_at=?
                            WHERE naukri_email=?
                        """, (
                            full_name, final_pass, frequency, update_mode, browser_mode,
                            auto_apply, max_apply, target_keywords, location,
                            exp_years, current_ctc, expected_ctc, notice_period,
                            final_rpath, final_rname, next_run, email
                        ))
                    else:
                        conn.execute("""
                            INSERT INTO profiles (
                                full_name, naukri_email, naukri_password, frequency, update_mode, browser_mode,
                                auto_apply, max_apply, target_keywords, location,
                                exp_years, current_ctc, expected_ctc, notice_period,
                                resume_path, resume_filename, next_run_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            full_name, email, password, frequency, update_mode, browser_mode,
                            auto_apply, max_apply, target_keywords, location,
                            exp_years, current_ctc, expected_ctc, notice_period,
                            resume_path, resume_filename, next_run
                        ))
                    conn.commit()

        elif parsed.path == "/action":
            pid = int(fields.get("profile_id", "0"))
            act = fields.get("action", "")
            with db_lock, sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM profiles WHERE id = ?", (pid,)).fetchone()
                prof = dict(row) if row else None

            if prof:
                if act == "run_now":
                    threading.Thread(target=execute_naukri_run_for_profile, args=(prof,), daemon=True).start()
                elif act == "delete":
                    with db_lock, sqlite3.connect(DB_PATH) as conn:
                        conn.execute("DELETE FROM profiles WHERE id = ?", (pid,))
                        conn.commit()
                elif act == "toggle_pause":
                    new_freq = "1h" if prof["frequency"] == "paused" else "paused"
                    nxt = compute_next_run(new_freq)
                    with db_lock, sqlite3.connect(DB_PATH) as conn:
                        conn.execute("UPDATE profiles SET frequency = ?, next_run_at = ? WHERE id = ?", (new_freq, nxt, pid))
                        conn.commit()

        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def render_page(self, selected_log_id=None, edit_id=None):
        with db_lock, sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            profiles = [dict(r) for r in conn.execute("SELECT * FROM profiles ORDER BY id DESC").fetchall()]
            jobs = [dict(r) for r in conn.execute("SELECT * FROM applied_jobs ORDER BY id DESC LIMIT 30").fetchall()]

        edit_prof = None
        if edit_id:
            for p in profiles:
                if str(p["id"]) == str(edit_id):
                    edit_prof = p
                    break

        f_name = edit_prof["full_name"] if edit_prof else "Shriman Narayan Mishra"
        f_email = edit_prof["naukri_email"] if edit_prof else "shree.n.mishra@gmail.com"
        f_freq = edit_prof["frequency"] if edit_prof else "1h"
        f_mode = edit_prof["update_mode"] if edit_prof else "both"
        f_bmode = (edit_prof.get("browser_mode") or "invisible_background") if edit_prof else "invisible_background"
        f_auto = bool(edit_prof["auto_apply"]) if edit_prof else False
        f_max = edit_prof["max_apply"] if edit_prof else 5
        f_kw = edit_prof["target_keywords"] if edit_prof else "Staff SDET Python Playwright, Principal SDET Python, SDET Lead Playwright AI"
        f_loc = edit_prof["location"] if edit_prof else "Bengaluru"
        f_exp = edit_prof["exp_years"] if edit_prof else "10"
        f_cctc = edit_prof["current_ctc"] if edit_prof else "30"
        f_ectc = edit_prof["expected_ctc"] if edit_prof else "42"
        f_np = edit_prof["notice_period"] if edit_prof else "30"

        freq_labels = {
            "1h": "Every 1 Hour",
            "2h": "Every 2 Hours",
            "4h": "Every 4 Hours",
            "6h": "Every 6 Hours",
            "9am_1pm": "Twice Daily (9:00 AM & 1:00 PM)",
            "daily_9am": "Once Daily (9:00 AM)",
            "paused": "Paused / Manual Only"
        }

        profiles_html = ""
        selected_log_text = "Select a profile or click 'Logs' to inspect execution output."
        selected_log_title = "Latest Profile Execution Log"

        if profiles:
            if not selected_log_id:
                selected_log_id = str(profiles[0]["id"])
            for p in profiles:
                if str(p["id"]) == str(selected_log_id):
                    selected_log_text = (p["last_log"] or "No logs yet.").replace("\\n", "\n")
                    selected_log_title = f"Execution Logs — {p['naukri_email']}"

                status = p["last_status"] or "Idle"
                s_color = "#10b981" if "Success" in status else ("#f59e0b" if "Running" in status else ("#ef4444" if "Failed" in status else "#64748b"))
                res_badge = f"<span class='badge badge-blue'>{p['resume_filename']}</span>" if p["resume_filename"] else "<span class='badge badge-gray'>No Resume File</span>"
                pause_label = "Resume" if p["frequency"] == "paused" else "Pause"

                profiles_html += f"""
                <tr>
                  <td>
                    <strong>{p['full_name']}</strong><br>
                    <span style="color:#94a3b8;font-size:12.5px;">{p['naukri_email']}</span><br>
                    {res_badge}
                  </td>
                  <td>
                    <span class="badge badge-purple">{freq_labels.get(p['frequency'], p['frequency'])}</span><br>
                    <small style="color:#94a3b8;">Mode: {p['update_mode'].upper()} | Browser: {p.get('browser_mode','invisible_background')}</small>
                  </td>
                  <td style="font-size:12.5px;">
                    <strong>Last:</strong> {p['last_run_at']}<br>
                    <strong>Next:</strong> {p['next_run_at']}
                  </td>
                  <td><span class="status-pill" style="background:{s_color};">{status}</span></td>
                  <td>
                    <div class="action-btns">
                      <form method="POST" action="/action" style="display:inline;">
                        <input type="hidden" name="profile_id" value="{p['id']}">
                        <input type="hidden" name="action" value="run_now">
                        <button type="submit" class="btn-sm btn-green">Run Now</button>
                      </form>
                      <a href="/?edit_id={p['id']}" class="btn-sm btn-blue">Edit</a>
                      <a href="/?log_id={p['id']}" class="btn-sm btn-gray">Logs</a>
                      <form method="POST" action="/action" style="display:inline;">
                        <input type="hidden" name="profile_id" value="{p['id']}">
                        <input type="hidden" name="action" value="toggle_pause">
                        <button type="submit" class="btn-sm btn-amber">{pause_label}</button>
                      </form>
                      <form method="POST" action="/action" style="display:inline;" onsubmit="return confirm('Delete this profile?');">
                        <input type="hidden" name="profile_id" value="{p['id']}">
                        <input type="hidden" name="action" value="delete">
                        <button type="submit" class="btn-sm btn-red">Delete</button>
                      </form>
                    </div>
                  </td>
                </tr>
                """
        else:
            profiles_html = "<tr><td colspan='5' style='text-align:center;color:#94a3b8;padding:24px;'>No profiles registered yet. Use the form on the left to register your Naukri account and upload your resume.</td></tr>"

        jobs_html = ""
        if jobs:
            for idx, j in enumerate(jobs, 1):
                jobs_html += f"""
                <tr>
                  <td>{idx}</td>
                  <td>{j['timestamp']}</td>
                  <td>{j['naukri_email']}</td>
                  <td><strong>{j['job_title']}</strong></td>
                  <td>{j['company']}</td>
                  <td>{j['location']}</td>
                  <td><a href="{j['url']}" target="_blank">View Job</a></td>
                </tr>
                """
        else:
            jobs_html = "<tr><td colspan='7' style='text-align:center;color:#94a3b8;padding:18px;'>No auto-applied jobs recorded yet.</td></tr>"

        def sel(val, target):
            return "selected" if str(val) == str(target) else ""

        engine_badge = "Patchright (CDP-Stealth Active)" if USING_PATCHRIGHT else "Standard Playwright (Run: pip install patchright)"

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Naukri Profile Automation & Scheduler Portal</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif; background: #0b1120; color: #f1f5f9; margin: 0; padding: 24px; }}
    .header {{ max-width: 1320px; margin: 0 auto 20px auto; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1e293b; padding-bottom: 14px; }}
    .header h1 {{ margin: 0; font-size: 22px; color: #38bdf8; }}
    .grid {{ max-width: 1320px; margin: 0 auto; display: grid; grid-template-columns: 400px minmax(0, 1fr); gap: 22px; }}
    .right-col {{ min-width: 0; }}
    @media (max-width: 980px) {{ .grid {{ grid-template-columns: minmax(0, 1fr); }} }}
    .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 20px; margin-bottom: 20px; min-width: 0; }}
    .card h2 {{ margin-top: 0; font-size: 17px; color: #e2e8f0; border-bottom: 1px solid #334155; padding-bottom: 10px; }}
    label {{ display: block; font-size: 13px; color: #cbd5e1; margin-bottom: 5px; font-weight: 500; }}
    input[type="text"], input[type="email"], input[type="password"], input[type="number"], select, textarea {{
      width: 100%; padding: 9px 11px; margin-bottom: 13px; border-radius: 7px; border: 1px solid #475569;
      background: #0f172a; color: #f8fafc; font-size: 13.5px;
    }}
    input[type="file"] {{
      width: 100%; padding: 8px; margin-bottom: 14px; border: 1px dashed #38bdf8; border-radius: 7px;
      background: #0f172a; color: #cbd5e1; font-size: 13px;
    }}
    .row-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
    .btn-primary {{
      width: 100%; background: linear-gradient(135deg, #2563eb, #3b82f6); color: white; border: none;
      padding: 11px; border-radius: 8px; font-weight: 600; font-size: 14px; cursor: pointer;
    }}
    .btn-primary:hover {{ opacity: 0.94; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
    th, td {{ text-align: left; padding: 11px 10px; border-bottom: 1px solid #334155; vertical-align: middle; }}
    th {{ color: #94a3b8; font-weight: 600; background: #0f172a; }}
    .badge {{ display: inline-block; padding: 3px 8px; border-radius: 5px; font-size: 11.5px; font-weight: 600; margin-top: 3px; }}
    .badge-blue {{ background: #1e3a8a; color: #93c5fd; }}
    .badge-purple {{ background: #3b0764; color: #d8b4fe; }}
    .badge-gray {{ background: #334155; color: #cbd5e1; }}
    .status-pill {{ display: inline-block; padding: 4px 10px; border-radius: 999px; color: #020617; font-weight: 700; font-size: 12px; }}
    .action-btns {{ display: flex; gap: 6px; flex-wrap: wrap; }}
    .btn-sm {{
      padding: 5px 10px; border-radius: 5px; border: none; font-size: 12px; font-weight: 600;
      cursor: pointer; text-decoration: none; display: inline-block; color: white;
    }}
    .btn-green {{ background: #059669; }}
    .btn-blue {{ background: #2563eb; }}
    .btn-amber {{ background: #d97706; }}
    .btn-red {{ background: #dc2626; }}
    .btn-gray {{ background: #475569; }}
    .log-box {{
      background: #020617; color: #38bdf8; padding: 14px; border-radius: 8px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12.5px; line-height: 1.6; max-height: 300px; overflow-y: auto; overflow-x: hidden;
      white-space: pre-wrap; word-break: break-word; overflow-wrap: anywhere; margin: 0; width: 100%;
    }}
    a {{ color: #38bdf8; text-decoration: none; }}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>Naukri Profile Automation & Scheduler Portal (Windows)</h1>
      <span style="color:#94a3b8;font-size:13px;">{engine_badge} | Server Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</span>
    </div>
    <div>
      <a href="/" class="btn-sm btn-gray" style="padding:8px 14px;">Refresh Dashboard</a>
    </div>
  </div>

  <div class="grid">
    <!-- Left Column: Registration & Configuration Form -->
    <div class="card">
      <h2>{'Edit Naukri Profile' if edit_prof else 'Register / Update Naukri Profile'}</h2>
      <form method="POST" action="/save_profile" enctype="multipart/form-data">
        <label>Candidate Full Name</label>
        <input type="text" name="full_name" value="{f_name}" required>

        <label>Naukri Login Email</label>
        <input type="email" name="naukri_email" value="{f_email}" required>

        <label>Naukri Password {'(Leave blank to keep existing)' if edit_prof else ''}</label>
        <input type="password" name="naukri_password" placeholder="Enter Naukri password" {'required' if not edit_prof else ''}>

        <label>Browser Mode (Headless / Akamai Bypass)</label>
        <select name="browser_mode">
          <option value="invisible_background" {sel(f_bmode, 'invisible_background')}>Invisible Background Chrome (Off-Screen — Best Akamai Bypass)</option>
          <option value="headless_new" {sel(f_bmode, 'headless_new')}>Chrome New Headless (--headless=new)</option>
          <option value="visible_chrome" {sel(f_bmode, 'visible_chrome')}>Visible Chrome Window (Use for 1-time OTP)</option>
        </select>

        <label>Run Frequency (Scheduler)</label>
        <select name="frequency">
          <option value="1h" {sel(f_freq, '1h')}>Every 1 Hour (Recommended)</option>
          <option value="2h" {sel(f_freq, '2h')}>Every 2 Hours</option>
          <option value="4h" {sel(f_freq, '4h')}>Every 4 Hours</option>
          <option value="6h" {sel(f_freq, '6h')}>Every 6 Hours</option>
          <option value="9am_1pm" {sel(f_freq, '9am_1pm')}>Twice Daily (9:00 AM & 1:00 PM)</option>
          <option value="daily_9am" {sel(f_freq, 'daily_9am')}>Once Daily (9:00 AM)</option>
          <option value="paused" {sel(f_freq, 'paused')}>Paused (Manual Trigger Only)</option>
        </select>

        <label>Profile Refresh Action</label>
        <select name="update_mode">
          <option value="both" {sel(f_mode, 'both')}>Update Headline + Re-Upload Resume (Both)</option>
          <option value="headline" {sel(f_mode, 'headline')}>Update Resume Headline Only</option>
          <option value="resume" {sel(f_mode, 'resume')}>Re-Upload Resume File Only</option>
        </select>

        <label>Upload Resume (PDF / DOCX)</label>
        <input type="file" name="resume_file" accept=".pdf,.doc,.docx">

        <div style="border-top:1px solid #334155; margin: 12px 0; padding-top: 12px;">
          <label style="display:flex; align-items:center; gap:8px; cursor:pointer; color:#38bdf8;">
            <input type="checkbox" name="auto_apply" value="1" {'checked' if f_auto else ''} style="width:auto;margin:0;">
            Also Auto-Apply to Matching Jobs on Naukri
          </label>
        </div>

        <label>Target Job Search Keywords (Comma-separated)</label>
        <input type="text" name="target_keywords" value="{f_kw}">

        <div class="row-2">
          <div>
            <label>Preferred Location</label>
            <input type="text" name="location" value="{f_loc}">
          </div>
          <div>
            <label>Experience (Years)</label>
            <input type="number" name="exp_years" value="{f_exp}">
          </div>
        </div>

        <div class="row-2">
          <div>
            <label>Current CTC (LPA)</label>
            <input type="text" name="current_ctc" value="{f_cctc}">
          </div>
          <div>
            <label>Expected CTC (LPA)</label>
            <input type="text" name="expected_ctc" value="{f_ectc}">
          </div>
        </div>

        <div class="row-2">
          <div>
            <label>Notice Period (Days)</label>
            <input type="number" name="notice_period" value="{f_np}">
          </div>
          <div>
            <label>Max Jobs / Run</label>
            <input type="number" name="max_apply" value="{f_max}">
          </div>
        </div>

        <button type="submit" class="btn-primary">Save Profile & Activate Schedule</button>
      </form>
    </div>

    <!-- Right Column: Active Profiles, Logs & Applied Jobs -->
    <div class="right-col">
      <div class="card">
        <h2>Registered Naukri Profiles & Active Schedules</h2>
        <div style="overflow-x:auto;">
          <table>
            <thead>
              <tr>
                <th>Candidate & Resume</th>
                <th>Schedule & Mode</th>
                <th>Timestamps</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {profiles_html}
            </tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <h2>{selected_log_title}</h2>
        <pre class="log-box">{selected_log_text}</pre>
      </div>

      <div class="card">
        <h2>Recent Auto-Applied Jobs</h2>
        <div style="overflow-x:auto;">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Time</th>
                <th>Profile</th>
                <th>Job Title</th>
                <th>Company</th>
                <th>Location</th>
                <th>Link</th>
              </tr>
            </thead>
            <tbody>
              {jobs_html}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Naukri Multi-User Automation Web App")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    args = parser.parse_args()

    init_db()
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()

    server = HTTPServer(("0.0.0.0", args.port), NaukriWebAppHandler)
    print("=" * 65)
    print(f"Naukri Automation Web App is running on: http://localhost:{args.port}")
    print(f"Driver Engine: {'Patchright' if USING_PATCHRIGHT else 'Playwright'}")
    print("=" * 65)
    server.serve_forever()


if __name__ == "__main__":
    main()
