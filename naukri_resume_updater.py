"""
Naukri Resume Auto-Updater
---------------------------
Logs into your Naukri.com profile and re-uploads your resume on a schedule,
which refreshes your profile's "last updated" timestamp and bumps you higher
in recruiter search results.

Default schedule: once a week, every Monday at 9:00 AM. Edit RUN_DAY / RUN_TIME
below to change this.

SETUP
-----
1. Install dependencies:
     pip install selenium webdriver-manager schedule python-dotenv

2. Create a file named `.env` in the same folder as this script:

     RESUME_PATH=/full/path/to/your/resume.pdf
     NAUKRI_EMAIL=your_email@example.com
     NAUKRI_PASSWORD="your_password"
     SESSION_PROFILE_DIR=/full/path/to/browser_profile
     SHOW_BROWSER=false

   The script keeps its own Chrome profile at SESSION_PROFILE_DIR and reuses
   the logged-in session on every run, so your credentials are only used the
   first time and whenever the session later expires — not on every run.
   That matters: repeatedly signing in from scratch is what bot-detection
   notices. Delete that folder to force a fresh login.

   SESSION_PROFILE_DIR must NOT be your real Chrome profile — Chrome 136+
   (May 2025) refuses to automate the default User Data directory and dies at
   startup with "DevToolsActivePort file doesn't exist". A dedicated folder
   sidesteps that and leaves your everyday browsing alone.

   If Naukri ever shows a CAPTCHA, set SHOW_BROWSER=true and run once by hand:
   you get a visible window, you sign in yourself, and the saved session takes
   over from there.

   Never hardcode your email/password in this script itself — keep them in .env
   (and don't commit .env to any public repo).

3. Run it:
     python naukri_resume_updater.py

   Leave it running (e.g. in a terminal, tmux/screen session, or as a background
   service) and it will trigger updates at the scheduled times. It does NOT
   run tasks in the past relative to when you start it — if you start it after
   9 AM, today's 9 AM run is simply skipped, not run late.

NOTES / LIMITATIONS
--------------------
- Naukri has no public API for this, so this drives a real Chrome browser via
  Selenium and clicks through the UI. It runs headless unless SHOW_BROWSER is
  set. If Naukri changes their site layout, the selectors below may need
  updating.
- Automating login on Naukri is against most sites' general terms of service
  in a strict reading, even though "refresh my resume periodically" is a very
  common manual habit among job seekers. Use at your own discretion, on your
  own account.
- If Naukri shows a CAPTCHA at login, this script cannot solve it — you'll
  need to log in manually in a real browser occasionally, or reduce run
  frequency if you get flagged.
- This script must keep running continuously (it's a simple scheduler loop).
  For "set and forget," see the note at the bottom about cron.
"""

import os
import sys
import time
import logging
import argparse
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import schedule
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    ElementNotInteractableException,
    WebDriverException,
)

# webdriver-manager is optional. Selenium 4.6+ ships "Selenium Manager", which
# auto-downloads a chromedriver matching your installed Chrome. We only fall
# back to webdriver-manager if it's installed AND Selenium Manager isn't doing
# the job. Reusing your real Chrome profile especially wants the driver to
# match your installed Chrome, so we prefer Selenium Manager there.
try:
    from webdriver_manager.chrome import ChromeDriverManager
    _HAS_WDM = True
except ImportError:
    _HAS_WDM = False

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

# Anchor everything to the script's own folder, not the current working
# directory. Task Scheduler / cron usually start the process somewhere else
# entirely, which would otherwise mean "no .env found" and a log file
# scattered into whatever cwd the scheduler happened to pick.
SCRIPT_DIR = Path(__file__).resolve().parent

load_dotenv(SCRIPT_DIR / ".env")

NAUKRI_EMAIL = os.getenv("NAUKRI_EMAIL")
NAUKRI_PASSWORD = os.getenv("NAUKRI_PASSWORD")
RESUME_PATH = os.getenv("RESUME_PATH")

# --- Persistent browser session -------------------------------------------
# The script keeps its OWN Chrome profile folder and reuses it every run, so it
# logs in ONCE and then just re-uses the saved session cookies, the same way a
# human's browser stays signed in. The password is only used again when the
# session actually expires.
#
# This is not merely a convenience: signing in from scratch several times a day
# is exactly the pattern bot-detection looks for. Reusing a session means a
# handful of logins a year instead of hundreds.
#
# This must NOT be your real Chrome profile — Chrome 136+ refuses to automate
# that (see is_default_user_data_dir below), and a dedicated folder also keeps
# your everyday browsing untouched.
SESSION_PROFILE_DIR = os.getenv(
    "SESSION_PROFILE_DIR", str(SCRIPT_DIR / "browser_profile")
)
SESSION_PROFILE_NAME = "Default"

# SHOW_BROWSER=true in .env opens a visible window instead of running headless.
# Useful the first time, or to clear a CAPTCHA by hand.
SHOW_BROWSER = os.getenv("SHOW_BROWSER", "false").lower() == "true"


def is_default_user_data_dir(path: str) -> bool:
    """True if `path` is Chrome's real default User Data dir, which Chrome 136+
    blocks from automation."""
    home = Path.home()
    defaults = [
        Path(os.getenv("LOCALAPPDATA", home / "AppData/Local"))
        / "Google/Chrome/User Data",
        home / "Library/Application Support/Google/Chrome",
        home / ".config/google-chrome",
    ]
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return False
    return any(resolved == d.resolve(strict=False) for d in defaults)

# Runs once a week. Pick the day (monday/tuesday/.../sunday) and time ("HH:MM").
RUN_DAY = "monday"
RUN_TIME = "09:00"

# How long to wait for the upload to actually show up on the profile, and how
# long to let the page settle afterwards before quitting the browser. Tearing
# Chrome down too early aborts the in-flight upload request.
UPLOAD_TIMEOUT = 90
POST_UPLOAD_SETTLE = 5

LOGIN_URL = "https://www.naukri.com/nlogin/login"
PROFILE_URL = "https://www.naukri.com/mnjuser/profile"

# Naukri's profile page has several <input type="file"> elements (profile
# photo, resume, etc.). Every locator here must be resume-SPECIFIC: a generic
# //input[@type='file'] would always match and would pick the first file input
# in the DOM, which is typically the profile-photo uploader — i.e. it would
# push the PDF at the wrong field. If none of these match we fail loudly
# instead of guessing.
RESUME_INPUT_LOCATORS = [
    (By.ID, "attachCV"),
    (By.XPATH, "//input[@type='file' and contains(@id, 'CV')]"),
    (By.XPATH, "//input[@type='file' and contains(@name, 'resume')]"),
    (By.XPATH, "//input[@type='file' and contains(@id, 'resume')]"),
    (By.XPATH, "//div[contains(@class,'resumeUpload')]//input[@type='file']"),
]

# The log file is UTF-8, but the Windows console defaults to cp1252 and
# mangles non-ASCII (em-dashes in these messages) into "?". Make stdout UTF-8
# so console output matches the log file.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(SCRIPT_DIR / "naukri_updater.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# CORE LOGIC
# --------------------------------------------------------------------------

def build_driver() -> webdriver.Chrome:
    options = Options()

    profile_dir = Path(SESSION_PROFILE_DIR)
    if is_default_user_data_dir(str(profile_dir)):
        raise ValueError(
            f"SESSION_PROFILE_DIR points at Chrome's real profile folder "
            f"({profile_dir}). Chrome 136+ refuses to be automated there and "
            f"dies at startup with 'DevToolsActivePort file doesn't exist'. "
            f"Use a dedicated folder instead, e.g. "
            f"{SCRIPT_DIR / 'browser_profile'}."
        )
    # Created on first run; from then on it carries the logged-in session.
    profile_dir.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument(f"--profile-directory={SESSION_PROFILE_NAME}")

    if SHOW_BROWSER:
        options.add_argument("--start-maximized")
    else:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1366,768")
        # NOTE the leading "--": Selenium passes arguments through verbatim and
        # does NOT add the dashes for you. Without them Chrome treats this as a
        # positional URL to open, silently ignores the user-agent override, and
        # launches a junk tab.
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    # Prefer Selenium Manager (built into Selenium 4.6+): it picks a driver
    # matching your installed Chrome, which matters most when reusing your
    # real profile. Fall back to webdriver-manager only if that fails.
    try:
        return webdriver.Chrome(options=options)
    except Exception as e:
        if _HAS_WDM:
            log.warning(f"Selenium Manager failed ({e}); trying webdriver-manager.")
            service = Service(ChromeDriverManager().install())
            return webdriver.Chrome(service=service, options=options)
        log.error(
            "Chrome failed to start and webdriver-manager is not installed, so "
            "there is no fallback. If this looks like a driver-version problem, "
            "`pip install webdriver-manager` and re-run."
        )
        raise


def login(driver: webdriver.Chrome) -> bool:
    log.info("Logging in to Naukri...")
    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, 20)
    try:
        email_field = wait.until(EC.presence_of_element_located((By.ID, "usernameField")))
        password_field = driver.find_element(By.ID, "passwordField")
        email_field.send_keys(NAUKRI_EMAIL)
        password_field.send_keys(NAUKRI_PASSWORD)

        login_btn = driver.find_element(By.XPATH, "//button[@type='submit']")
        login_btn.click()

        # Wait until redirected to the logged-in area. Same path-based check as
        # above — the signed-out login URL carries "naukri.com/mnjuser" in its
        # ?URL= parameter, so a substring match would report a false success.
        wait.until(lambda d: on_logged_in_page(d))
        log.info("Login successful.")
        return True
    except TimeoutException:
        log.error(
            "Login did not complete in time. This usually means a CAPTCHA "
            "appeared, credentials are wrong, or Naukri changed their login "
            "page layout."
        )
        return False


def on_logged_in_page(driver: webdriver.Chrome) -> bool:
    """True only if the browser is actually on a signed-in Naukri page.

    Deliberately checks the URL *path*, not a substring of the whole URL: when
    you're signed out Naukri redirects to
    ``/nlogin/login?URL=//www.naukri.com/mnjuser/profile``, which contains
    "naukri.com/mnjuser" in its query string and would fool a naive
    `url_contains` check into reporting success on the login page.
    """
    parsed = urlparse(driver.current_url)
    return parsed.path.startswith("/mnjuser") and "nlogin" not in parsed.path


def xpath_literal(value: str) -> str:
    """Quote an arbitrary string for safe use as an XPath string literal.

    A filename containing an apostrophe would otherwise break the expression,
    so anything with a single quote gets assembled via concat().
    """
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"


def update_resume(driver: webdriver.Chrome) -> bool:
    if not RESUME_PATH or not os.path.isfile(RESUME_PATH):
        log.error(f"RESUME_PATH is not a valid file: {RESUME_PATH}")
        return False

    log.info("Navigating to profile page...")
    driver.get(PROFILE_URL)
    wait = WebDriverWait(driver, 25)

    # Confirm we actually landed on the logged-in profile page. If the session
    # has expired, Naukri quietly redirects to the login page — without this
    # check the file-input lookup below just times out and reports a bogus
    # "layout may have changed", sending you selector-hunting when the real
    # problem is that you need to sign in again.
    try:
        wait.until(lambda d: on_logged_in_page(d))
    except TimeoutException:
        log.error(
            f"Not logged in — Naukri redirected to {driver.current_url!r} instead "
            f"of the profile page. The session was not established."
        )
        return False

    file_input = None
    # Let the profile page settle so lazy-loaded sections exist.
    try:
        wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='file']")))
    except TimeoutException:
        log.error("No file input appeared on the profile page — layout may have changed.")
        return False

    for by, sel in RESUME_INPUT_LOCATORS:
        try:
            el = driver.find_element(by, sel)
            file_input = el
            log.info(f"Using resume upload input located by: {by} = {sel!r}")
            break
        except NoSuchElementException:
            continue

    if file_input is None:
        log.error(
            "Found file input(s) on the page, but none of them matched a resume "
            "upload field. Refusing to guess — uploading to an unidentified file "
            "input risks sending the PDF to the profile-photo field. Naukri's "
            "layout has probably changed; update resume_input_locators."
        )
        return False

    try:
        # Verify the upload actually landed, using the ONE piece of ground
        # truth on the page: the resume card's filename label. Everything else
        # lies. A transient "...successfully" toast appears within ~0.5s of
        # send_keys, long before the upload XHR finishes — trusting it meant
        # reporting success and then calling driver.quit() a couple of seconds
        # later, which aborted the in-flight upload and silently left the OLD
        # resume on the profile.
        #
        # So: wait for the displayed filename to actually become our file's
        # name, and give the upload a realistic amount of time to finish.
        expected = os.path.basename(RESUME_PATH)
        label = (
            By.XPATH,
            "//div[contains(@class,'truncate') and contains(@class,'exten')]",
        )

        def label_text() -> str:
            try:
                return driver.find_element(*label).text.strip()
            except (NoSuchElementException, WebDriverException):
                return ""

        before = label_text()
        log.info(f"Resume currently on profile: {before or '(none shown)'}")

        driver.execute_script("arguments[0].scrollIntoView(true);", file_input)
        file_input.send_keys(RESUME_PATH)

        try:
            WebDriverWait(driver, UPLOAD_TIMEOUT).until(
                lambda d: label_text() == expected
            )
        except TimeoutException:
            current = label_text()
            if current and current != before:
                log.warning(
                    f"Profile now shows {current!r}, which is neither the old "
                    f"resume nor {expected!r}. Check your profile manually."
                )
            else:
                log.error(
                    f"Upload did not take effect after {UPLOAD_TIMEOUT}s — the "
                    f"profile still shows {current or '(nothing)'!r}, not "
                    f"{expected!r}. Naukri may have rejected the file (check "
                    f"size/format limits) or changed its upload flow."
                )
            return False

        log.info(f"Filename on profile is now: {expected}")
        # Let any trailing profile-timestamp write finish before we tear the
        # browser down.
        time.sleep(POST_UPLOAD_SETTLE)

        # The filename check alone is NOT enough on repeat runs: you re-upload
        # the same file every week, so the label already reads the expected
        # name before we touch anything and the wait above passes instantly,
        # proving nothing. The actual goal is refreshing the profile's
        # "last updated" stamp, so verify that directly, after a reload so we
        # are reading the server's state and not a stale page.
        driver.refresh()
        try:
            WebDriverWait(driver, 30).until(lambda d: on_logged_in_page(d))
            stamp = WebDriverWait(driver, 30).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(text(),'Profile last updated')]")
                )
            ).text.strip()
        except TimeoutException:
            log.warning(
                "Uploaded the resume, but could not re-read the "
                "'Profile last updated' stamp to confirm it refreshed. "
                "Check your profile manually this once."
            )
            return False

        log.info(f"Profile stamp now reads: {stamp!r}")
        if "today" in stamp.lower():
            log.info("Confirmed: profile timestamp refreshed to today.")
            return True

        log.error(
            f"Resume was sent, but the profile stamp still reads {stamp!r} "
            f"instead of 'Today' — the update did not actually register."
        )
        return False
    except ElementNotInteractableException as e:
        # Hidden file inputs usually still accept send_keys, but not always —
        # this was previously uncaught and lost the specific diagnostic.
        log.error(
            f"The resume upload input was found but would not accept the file "
            f"path (it may be hidden behind a click-to-open dialog now): {e}"
        )
        return False
    except (TimeoutException, NoSuchElementException, WebDriverException) as e:
        log.error(f"Could not interact with the resume upload element: {e}")
        return False


def ensure_logged_in(driver: webdriver.Chrome) -> bool:
    """Reuse the saved session if it is still valid; log in only if it isn't.

    Every run starts by simply opening the profile page. If the persistent
    profile still holds a valid Naukri session we are already there and no
    credentials are touched at all — which is both faster and far less
    conspicuous than authenticating from scratch on every single run.
    """
    log.info("Checking saved session...")
    driver.get(PROFILE_URL)
    try:
        WebDriverWait(driver, 20).until(lambda d: on_logged_in_page(d))
        log.info("Saved session is still valid — no login needed.")
        return True
    except TimeoutException:
        pass

    log.info("Saved session expired or missing; logging in with credentials.")
    if not NAUKRI_EMAIL or not NAUKRI_PASSWORD:
        log.error(
            "No saved session, and NAUKRI_EMAIL / NAUKRI_PASSWORD are not set "
            "in .env — cannot sign in. Either add them, or run once with "
            "SHOW_BROWSER=true and sign in by hand to seed the session."
        )
        return False
    return login(driver)


def run_update_job():
    log.info("=" * 60)
    log.info(f"Starting scheduled resume update at {datetime.now()}")

    driver = None
    try:
        driver = build_driver()

        if ensure_logged_in(driver):
            success = update_resume(driver)
            log.info("Update succeeded." if success else "Update failed.")
        else:
            log.error("Skipping resume update because login failed.")
    except Exception as e:
        log.exception(f"Unexpected error during update job: {e}")
    finally:
        if driver:
            driver.quit()


# --------------------------------------------------------------------------
# SCHEDULER
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Naukri resume auto-updater")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single update immediately and exit (use this with Windows "
             "Task Scheduler / cron). Without it, the script stays open and "
             "runs on its own internal weekly schedule.",
    )
    args = parser.parse_args()

    if args.once:
        log.info("Running in --once mode: single update, then exit.")
        run_update_job()
        log.info("Done. Exiting.")
        return

    log.info(f"Naukri resume updater starting. Scheduled: every {RUN_DAY} at {RUN_TIME}")
    getattr(schedule.every(), RUN_DAY.lower()).at(RUN_TIME).do(run_update_job)

    log.info("Scheduler is running. Press Ctrl+C to stop.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        log.info("Stopped by user.")


if __name__ == "__main__":
    main()

# --------------------------------------------------------------------------
# ALTERNATIVE: run via cron instead of keeping this process alive
# --------------------------------------------------------------------------
# If you'd rather not keep a Python process running all week, remove the
# scheduler loop above, keep only run_update_job(), and add this to your
# crontab (`crontab -e`) instead:
#
#   0 9 * * 1 /usr/bin/python3 /full/path/to/naukri_resume_updater.py
#
# That runs it every Monday at 9am via the OS scheduler, which is more
# reliable for long-running unattended use than a Python loop.