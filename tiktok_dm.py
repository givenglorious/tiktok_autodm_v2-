"""
TikTok Auto DM — REST API + Frontend
Features:
  - Cookie session: login once, reuse session on next runs
  - In-memory username list: shared across all devices in same session
  - Reset endpoint: clears stuck job status
  - Log export
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

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.keys import Keys
from webdriver_manager.chrome import ChromeDriverManager

# ── Paths ─────────────────────────────────────────────────
COOKIE_FILE = Path("tiktok_cookies.pkl")
CRED_FILE   = Path("saved_credentials.json")
LOG_FILE    = Path("tiktok_dm.log")

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

# ── FastAPI ───────────────────────────────────────────────
app = FastAPI(title="TikTok Auto DM API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Job State ────────────────────────────────────────────
job_status = {
    "running":      False,
    "login_failed": False,
    "success":      0,
    "failed":       0,
    "current":      "",
    "log":          []
}

# ── In-memory username list (shared across all devices) ───
_memory_usernames: list = []


# ── Request bodies ────────────────────────────────────────
class DMRequest(BaseModel):
    email: str
    password: str
    message: str
    target_usernames: List[str]

class SaveUsernamesRequest(BaseModel):
    usernames: List[str]

class SaveCredentialsRequest(BaseModel):
    email: str
    password: str


# ── Selenium ──────────────────────────────────────────────
class TikTokDMSender:

    def __init__(self, config: dict):
        self.config = config

    def _init_driver(self):
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument("--window-size=1280,900")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        return driver

    def _save_cookies(self, driver):
        with open(COOKIE_FILE, "wb") as f:
            pickle.dump(driver.get_cookies(), f)
        log.info(f"Cookies saved -> {COOKIE_FILE}")

    def _load_cookies(self, driver) -> bool:
        if not COOKIE_FILE.exists():
            return False
        log.info("Found saved cookies — attempting to restore session...")
        job_status["log"].append("Found saved session — trying to restore...")
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
            log.warning("Saved cookies expired — need to log in again.")
            job_status["log"].append("Saved session expired — logging in again...")
            COOKIE_FILE.unlink(missing_ok=True)
            return False
        log.info("Session restored!")
        job_status["log"].append("Session restored from saved cookies — login skipped!")
        return True

    def _login(self, driver) -> bool:
        wait = WebDriverWait(driver, 30)
        log.info("Opening TikTok login page...")
        job_status["log"].append("Logging in to TikTok...")
        driver.get("https://www.tiktok.com/login/phone-or-email/email")
        time.sleep(3)
        try:
            email_input = wait.until(EC.presence_of_element_located((By.NAME, "username")))
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
                job_status["log"].append("CAPTCHA triggered — cannot solve in headless mode.")
                time.sleep(5)
            if "login" in driver.current_url.lower():
                log.error("Login failed — still on login page.")
                return False
            log.info("Login successful!")
            self._save_cookies(driver)
            job_status["log"].append("Login successful! Session saved for future runs.")
            return True
        except Exception as e:
            log.error(f"Login error: {e}")
            return False

    def _send_dm(self, driver, username: str, message: str) -> bool:
        wait = WebDriverWait(driver, 20)
        log.info(f"Opening profile @{username}...")
        job_status["current"] = username
        try:
            driver.get(f"https://www.tiktok.com/@{username}")
            time.sleep(3)
            try:
                message_btn = wait.until(
                    EC.element_to_be_clickable((By.XPATH,
                        '//button[contains(@data-e2e,"message") or '
                        'contains(translate(text(),"MESSAGE","message"),"message")]'
                    ))
                )
            except:
                job_status["log"].append(f"SKIP: @{username} — DM not available")
                return False
            message_btn.click()
            time.sleep(3)
            msg_box = wait.until(
                EC.presence_of_element_located((By.XPATH,
                    '//div[@contenteditable="true"] | //textarea[@placeholder]'
                ))
            )
            msg_box.click()
            time.sleep(1)
            msg_box.send_keys(message)
            time.sleep(1)
            msg_box.send_keys(Keys.RETURN)
            time.sleep(2)
            log.info(f"Message sent to @{username}")
            return True
        except Exception as e:
            log.error(f"Failed to send to @{username}: {e}")
            return False

    def run(self):
        global job_status
        job_status = {
            "running":      True,
            "login_failed": False,
            "success":      0,
            "failed":       0,
            "current":      "",
            "log":          []
        }
        driver = self._init_driver()
        try:
            session_ok = self._load_cookies(driver)
            if not session_ok:
                if not self._login(driver):
                    job_status["login_failed"] = True
                    job_status["log"].append("Login failed — wrong email or password.")
                    return
            job_status["log"].append("Starting DM job...")
            for username in self.config["target_usernames"]:
                result = self._send_dm(driver, username, self.config["message"])
                if result:
                    job_status["success"] += 1
                    job_status["log"].append(f"OK: @{username}")
                else:
                    job_status["failed"] += 1
                    job_status["log"].append(f"FAILED: @{username}")
                time.sleep(5)
        finally:
            driver.quit()
            job_status["running"] = False
            job_status["current"] = ""
            if not job_status["login_failed"]:
                job_status["log"].append(
                    f"Done! Sent: {job_status['success']} | Failed: {job_status['failed']}"
                )


# ── Endpoints ─────────────────────────────────────────────

@app.get("/")
def root():
    return FileResponse("index.html")


@app.get("/status")
def get_status():
    return job_status


@app.post("/reset")
def reset_status():
    """Force reset stuck job status"""
    global job_status
    job_status = {
        "running":      False,
        "login_failed": False,
        "success":      0,
        "failed":       0,
        "current":      "",
        "log":          ["Status reset manually."]
    }
    return {"status": "reset"}


@app.post("/send-dm")
def send_dm(req: DMRequest):
    if job_status["running"]:
        return JSONResponse(status_code=409, content={"error": "A job is already running."})

    email    = req.email
    password = req.password
    if (not email or not password) and CRED_FILE.exists():
        try:
            saved    = json.loads(CRED_FILE.read_text(encoding="utf-8"))
            email    = email    or saved.get("email", "")
            password = password or saved.get("password", "")
        except Exception:
            pass

    config = {
        "email":            email,
        "password":         password,
        "message":          req.message,
        "target_usernames": req.target_usernames,
    }

    TikTokDMSender(config).run()

    if job_status["login_failed"]:
        return JSONResponse(
            status_code=401,
            content={"error": "login_failed", "message": "Wrong email or password."}
        )

    return {
        "status":  "completed",
        "success": job_status["success"],
        "failed":  job_status["failed"],
        "log":     job_status["log"]
    }


# ── Session ───────────────────────────────────────────────

@app.get("/session")
def session_info():
    if COOKIE_FILE.exists():
        import os
        from datetime import datetime
        saved_at = datetime.fromtimestamp(os.path.getmtime(COOKIE_FILE)).strftime("%Y-%m-%d %H:%M:%S")
        return {"has_session": True, "saved_at": saved_at}
    return {"has_session": False}

@app.delete("/session")
def clear_session():
    if COOKIE_FILE.exists():
        COOKIE_FILE.unlink()
        return {"status": "cleared"}
    return {"status": "no_session"}


# ── Username list (in-memory, shared across all devices) ──

@app.get("/usernames")
def get_usernames():
    return {"usernames": _memory_usernames}

@app.post("/usernames")
def save_usernames_endpoint(req: SaveUsernamesRequest):
    global _memory_usernames
    _memory_usernames = req.usernames
    log.info(f"Username list updated: {len(_memory_usernames)} entries")
    return {"status": "saved", "count": len(_memory_usernames)}

@app.delete("/usernames")
def clear_usernames():
    global _memory_usernames
    _memory_usernames = []
    return {"status": "cleared"}


# ── Credentials ───────────────────────────────────────────

@app.get("/credentials")
def get_credentials():
    if not CRED_FILE.exists():
        return {"has_credentials": False}
    try:
        data = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        return {
            "has_credentials": True,
            "email":    data.get("email", ""),
            "password": data.get("password", ""),
        }
    except Exception:
        return {"has_credentials": False}

@app.post("/credentials")
def save_credentials_endpoint(req: SaveCredentialsRequest):
    CRED_FILE.write_text(
        json.dumps({"email": req.email, "password": req.password}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
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
    if not LOG_FILE.exists():
        return JSONResponse(status_code=404, content={"error": "Log file not found."})
    return PlainTextResponse(
        content=LOG_FILE.read_text(encoding="utf-8"),
        headers={"Content-Disposition": "attachment; filename=tiktok_dm.log"}
    )