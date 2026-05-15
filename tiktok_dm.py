"""
TikTok Auto DM — REST API + Frontend
Features:
  - Headless Chrome (no pop-up window)
  - Cookie session: login once, reuse session on next runs
  - Saved username list: persisted to disk so you don't retype every time
  - Log export: GET /export-log returns a downloadable .txt file
"""

import sys
import json
import time
import logging
import pickle
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List

import threading
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.keys import Keys
from webdriver_manager.chrome import ChromeDriverManager

# ── Paths ─────────────────────────────────────────────────
# DATA_DIR env var diset ke /data (Railway Volume) supaya file tidak hilang saat redeploy.
# Kalau tidak ada env var (local), pakai direktori saat ini.
import os
import subprocess
BASE_DIR      = Path(os.environ.get("DATA_DIR", "."))
BASE_DIR.mkdir(parents=True, exist_ok=True)

COOKIE_FILE   = BASE_DIR / "tiktok_cookies.pkl"    # saved session cookies
USERNAME_FILE = BASE_DIR / "saved_usernames.json"  # persisted username list
CRED_FILE     = BASE_DIR / "saved_credentials.json"# saved email + password
LOG_FILE      = BASE_DIR / "tiktok_dm.log"

# ── Logging ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(
            open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
        )
    ]
)
log = logging.getLogger(__name__)

# ── Chromium bootstrap (Railway) ─────────────────────────
def _ensure_chromium():
    """
    Install Chromium jika belum ada di sistem (Railway/Linux tanpa Nix).
    Hanya berjalan sekali saat startup.
    """
    candidates = [
        "/run/current-system/sw/bin/chromium",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
    ]
    if any(Path(p).exists() for p in candidates):
        return  # sudah ada, skip

    log.info("Chromium tidak ditemukan — mencoba install via apt...")
    try:
        subprocess.run(
            ["apt-get", "install", "-y", "chromium", "chromium-driver"],
            check=True, capture_output=True
        )
        log.info("Chromium berhasil diinstall via apt.")
    except Exception as e:
        log.warning(f"apt install chromium gagal: {e} — akan coba ChromeDriverManager.")

_ensure_chromium()

# ── FastAPI ───────────────────────────────────────────────
app = FastAPI(title="TikTok Auto DM API")

@app.on_event("startup")
def startup_reset():
    """Reset job_status saat app startup — cegah stuck dari deploy sebelumnya."""
    global job_status
    job_status["running"] = False
    job_status["current"] = ""
    log.info("Startup: job_status direset ke idle.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Job State ────────────────────────────────────────────
job_status = {
    "running": False,
    "login_failed": False,
    "success": 0,
    "failed": 0,
    "current": "",
    "log": []
}


# ── Request bodies ────────────────────────────────────────
class DMRequest(BaseModel):
    email: str
    password: str
    message: str
    target_usernames: List[str]
    save_usernames: bool = True   # persist username list to disk

class SaveUsernamesRequest(BaseModel):
    usernames: List[str]

class SaveCredentialsRequest(BaseModel):
    email: str
    password: str


# ── Selenium ──────────────────────────────────────────────
class TikTokDMSender:

    def __init__(self, config: dict):
        self.config = config

    # ── Driver ────────────────────────────────────────────
    def _init_driver(self):
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument("--window-size=1280,900")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )

        # ── Deteksi Chromium via `which` (paling akurat di semua environment) ──
        def _which(cmd):
            try:
                result = subprocess.run(
                    ["which", cmd], capture_output=True, text=True, check=True
                )
                path = result.stdout.strip()
                return path if path else None
            except Exception:
                return None

        chromium_path     = _which("chromium") or _which("chromium-browser") or _which("google-chrome") or _which("google-chrome-stable")
        chromedriver_path = _which("chromedriver")

        log.info(f"Chromium binary  : {chromium_path or 'NOT FOUND'}")
        log.info(f"ChromeDriver path: {chromedriver_path or 'NOT FOUND'}")
        job_status["log"].append(f"Chromium: {chromium_path or 'NOT FOUND'}")
        job_status["log"].append(f"ChromeDriver: {chromedriver_path or 'NOT FOUND'}")

        if chromium_path and chromedriver_path:
            log.info("Menggunakan Chromium sistem (Railway/Linux mode)")
            options.binary_location = chromium_path
            service = Service(chromedriver_path)
        else:
            log.info("Pakai ChromeDriverManager (local/fallback mode)")
            service = Service(ChromeDriverManager().install())

        driver = webdriver.Chrome(service=service, options=options)
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        log.info("Chrome driver berhasil diinisialisasi.")
        job_status["log"].append("✓ Browser berhasil distart.")
        return driver

    # ── Cookie helpers ────────────────────────────────────
    def _save_cookies(self, driver):
        with open(COOKIE_FILE, "wb") as f:
            pickle.dump(driver.get_cookies(), f)
        log.info(f"Cookies saved → {COOKIE_FILE}")

    def _load_cookies(self, driver) -> bool:
        """
        Restore saved cookies and verify the session is still active.
        Returns True if session is valid (no login needed).
        """
        if not COOKIE_FILE.exists():
            return False

        log.info("Found saved cookies — attempting to restore session...")
        job_status["log"].append("🔑 Found saved session — trying to restore...")
        driver.get("https://www.tiktok.com")
        time.sleep(2)

        with open(COOKIE_FILE, "rb") as f:
            cookies = pickle.load(f)

        for cookie in cookies:
            try:
                driver.add_cookie(cookie)
            except Exception:
                pass

        driver.refresh()
        time.sleep(4)

        if "login" in driver.current_url.lower():
            log.warning("Saved cookies are expired — need to log in again.")
            job_status["log"].append("⚠ Saved session expired — logging in again...")
            COOKIE_FILE.unlink(missing_ok=True)
            return False

        log.info("Session restored — login skipped!")
        job_status["log"].append("✓ Session restored from saved cookies — login skipped!")
        return True

    # ── Login ─────────────────────────────────────────────
    def _login(self, driver) -> bool:
        wait = WebDriverWait(driver, 30)
        log.info("Opening TikTok login page...")
        job_status["log"].append("🔐 Logging in to TikTok...")
        driver.get("https://www.tiktok.com/login/phone-or-email/email")
        time.sleep(3)

        try:
            email_input = wait.until(
                EC.presence_of_element_located((By.NAME, "username"))
            )
            email_input.clear()
            email_input.send_keys(self.config["email"])
            time.sleep(1)

            pass_input = driver.find_element(By.XPATH, '//input[@type="password"]')
            pass_input.clear()
            pass_input.send_keys(self.config["password"])
            time.sleep(1)

            login_btn = driver.find_element(By.XPATH, '//button[@data-e2e="login-button"]')
            login_btn.click()
            log.info("Waiting for login response...")
            time.sleep(6)

            if "captcha" in driver.current_url.lower():
                job_status["log"].append(
                    "⚠ CAPTCHA triggered in headless mode — cannot solve. "
                    "Try clearing session and running again."
                )
                time.sleep(5)

            if "login" in driver.current_url.lower():
                log.error("Login failed — still on login page.")
                return False

            log.info("Login successful! Saving cookies...")
            self._save_cookies(driver)
            job_status["log"].append("✓ Login successful! Session saved for future runs.")
            return True

        except Exception as e:
            log.error(f"Login error: {e}")
            return False

    # ── Send DM ───────────────────────────────────────────
    def _send_dm(self, driver, username: str, message: str) -> bool:
        wait = WebDriverWait(driver, 20)
        log.info(f"Opening profile @{username}...")
        job_status["current"] = username

        try:
            driver.get(f"https://www.tiktok.com/@{username}")
            time.sleep(3)

            message_btn = wait.until(
                EC.element_to_be_clickable((By.XPATH,
                    '//button[contains(@data-e2e,"message") or '
                    'contains(translate(text(),"MESSAGE","message"),"message")]'
                ))
            )
            message_btn.click()
            time.sleep(3)

            msg_box = wait.until(
                EC.presence_of_element_located((By.XPATH,
                    '//div[@contenteditable="true"] | //textarea[@placeholder]'
                ))
            )
            msg_box.click()
            time.sleep(1)

            # Gunakan JavaScript untuk set value — menghindari error BMP pada emoji
            # send_keys() tidak support karakter di luar Unicode BMP (emoji 4-byte)
            driver.execute_script(
                """
                const el = arguments[0];
                const msg = arguments[1];
                el.focus();
                // Untuk contenteditable div (TikTok DM box)
                if (el.isContentEditable) {
                    el.innerText = msg;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                } else {
                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    ).set;
                    nativeInputValueSetter.call(el, msg);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                }
                """,
                msg_box, message
            )
            time.sleep(1)
            msg_box.send_keys(Keys.RETURN)
            time.sleep(2)

            log.info(f"Message sent to @{username}")
            return True

        except Exception as e:
            log.error(f"Failed to send to @{username}: {e}")
            return False

    # ── Main run ──────────────────────────────────────────
    def run(self):
        global job_status

        job_status = {
            "running": True,
            "login_failed": False,
            "success": 0,
            "failed": 0,
            "current": "",
            "log": []
        }

        driver = None
        try:
            job_status["log"].append("🚀 Memulai browser...")
            driver = self._init_driver()

            # Try restoring cookies first; fall back to full login
            session_ok = self._load_cookies(driver)

            if not session_ok:
                if not self._login(driver):
                    job_status["login_failed"] = True
                    job_status["log"].append(
                        "✗ Login failed — wrong email or password. Please re-enter your credentials."
                    )
                    job_status["running"] = False
                    return

            job_status["log"].append("Starting DM job...")

            if self.config.get("save_usernames"):
                _save_usernames(self.config["target_usernames"])

            for username in self.config["target_usernames"]:
                result = self._send_dm(driver, username, self.config["message"])
                if result:
                    job_status["success"] += 1
                    job_status["log"].append(f"OK: @{username}")
                else:
                    job_status["failed"] += 1
                    job_status["log"].append(f"FAILED: @{username}")

                time.sleep(5)

        except Exception as e:
            log.error(f"Unexpected error di run(): {e}")
            job_status["log"].append(f"✗ Error: {e}")
        finally:
            try:
                if driver:
                    driver.quit()
            except Exception:
                pass
            job_status["running"] = False
            job_status["current"] = ""
            if not job_status["login_failed"]:
                job_status["log"].append(
                    f"Done! Sent: {job_status['success']} | Failed: {job_status['failed']}"
                )


# ── Username list helpers ─────────────────────────────────

def _save_usernames(usernames: list):
    with open(USERNAME_FILE, "w", encoding="utf-8") as f:
        json.dump(usernames, f, ensure_ascii=False, indent=2)
    log.info(f"Username list saved → {USERNAME_FILE} ({len(usernames)} entries)")

def _load_usernames() -> list:
    if not USERNAME_FILE.exists():
        return []
    with open(USERNAME_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Endpoints ─────────────────────────────────────────────

@app.get("/")
def root():
    return FileResponse("index.html")


@app.post("/send-dm")
def send_dm(req: DMRequest):
    if job_status["running"]:
        return JSONResponse(status_code=409, content={"error": "A job is already running."})

    # Auto-fill from saved credentials if not provided
    email    = req.email
    password = req.password
    if (not email or not password) and CRED_FILE.exists():
        try:
            saved = json.loads(CRED_FILE.read_text(encoding="utf-8"))
            email    = email    or saved.get("email", "")
            password = password or saved.get("password", "")
        except Exception:
            pass

    config = {
        "email":            email,
        "password":         password,
        "message":          req.message,
        "target_usernames": req.target_usernames,
        "save_usernames":   req.save_usernames,
    }

    thread = threading.Thread(target=TikTokDMSender(config).run, daemon=True)
    thread.start()
    thread.join()  # tunggu selesai supaya response tetap sinkron

    if job_status["login_failed"]:
        return JSONResponse(
            status_code=401,
            content={
                "error": "login_failed",
                "message": "Wrong email or password. Please re-enter your credentials.",
            }
        )

    return {
        "status":  "completed",
        "success": job_status["success"],
        "failed":  job_status["failed"],
        "log":     job_status["log"]
    }


@app.get("/status")
def get_status():
    return job_status


# ── Session (cookie) management ───────────────────────────

@app.get("/session")
def session_info():
    """Check whether a saved cookie session exists."""
    if COOKIE_FILE.exists():
        import os
        from datetime import datetime
        saved_at = datetime.fromtimestamp(os.path.getmtime(COOKIE_FILE)).strftime("%Y-%m-%d %H:%M:%S")
        return {"has_session": True, "saved_at": saved_at}
    return {"has_session": False}

@app.delete("/session")
def clear_session():
    """Delete saved cookies — forces a fresh login next run."""
    if COOKIE_FILE.exists():
        COOKIE_FILE.unlink()
        return {"status": "cleared"}
    return {"status": "no_session"}


# ── Username list endpoints ───────────────────────────────

@app.get("/usernames")
def get_usernames():
    return {"usernames": _load_usernames()}

@app.post("/usernames")
def save_usernames_endpoint(req: SaveUsernamesRequest):
    _save_usernames(req.usernames)
    return {"status": "saved", "count": len(req.usernames)}

@app.delete("/usernames")
def clear_usernames():
    if USERNAME_FILE.exists():
        USERNAME_FILE.unlink()
    return {"status": "cleared"}


# ── Credentials (server-side) ─────────────────────────────

@app.get("/credentials")
def get_credentials():
    """Return saved email (password masked). Used to pre-fill the form."""
    if not CRED_FILE.exists():
        return {"has_credentials": False}
    try:
        data = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        return {
            "has_credentials": True,
            "email": data.get("email", ""),
            "password": data.get("password", ""),   # sent over localhost only
        }
    except Exception:
        return {"has_credentials": False}

@app.post("/credentials")
def save_credentials_endpoint(req: SaveCredentialsRequest):
    CRED_FILE.write_text(
        json.dumps({"email": req.email, "password": req.password}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    log.info(f"Credentials saved → {CRED_FILE}")
    return {"status": "saved"}

@app.delete("/credentials")
def clear_credentials():
    if CRED_FILE.exists():
        CRED_FILE.unlink()
        return {"status": "cleared"}
    return {"status": "no_credentials"}


# ── Log export ────────────────────────────────────────────

@app.get("/export-log")
def export_log():
    """Download the full log file as a .txt attachment."""
    if not LOG_FILE.exists():
        return JSONResponse(status_code=404, content={"error": "Log file not found."})
    return PlainTextResponse(
        content=LOG_FILE.read_text(encoding="utf-8"),
        headers={"Content-Disposition": "attachment; filename=tiktok_dm.log"}
    )

# ── Force reset ────────────────────────────────────────────

@app.post("/reset")
def reset_job():
    """
    Paksa reset job_status ke idle.
    Gunakan jika ada 'job hantu' yang bikin endpoint /send-dm selalu 409.
    """
    global job_status
    job_status = {
        "running": False,
        "login_failed": False,
        "success": 0,
        "failed": 0,
        "current": "",
        "log": []
    }
    log.info("Job status di-reset paksa via /reset endpoint.")
    return {"status": "reset", "message": "Job status berhasil direset."}