"""
OpenFX — Trade History Export Automation
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Date range is calculated automatically in this script:
  END_DATE   = today
  START_DATE = today minus 10 days

n8n Execute Command node — just run the script, no date args needed:
  Command  : python
  Arguments: /full/path/to/openfx_download_trade.py

Optional env vars:
  OPENFX_EMAIL          default: amitkumar@tazapay.com
  OPENFX_PASSWORD       default: hardcoded
  OPENFX_TOTP_SECRET    default: read from totp_secret.txt
  OPENFX_HEADLESS       true/false  (default: false)
  OPENFX_SESSION_FILE   default: openfx_session.pkl
  OPENFX_SCREENSHOT_DIR default: screenshots

Output (JSON to stdout):
  { "success": true/false, "message": "...", "start_date": "...",
    "end_date": "...", "error": "", "screenshots": [...] }

Exit 0 = success, Exit 1 = failure
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import os
import pickle
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pyotp
import undetected_chromedriver as uc
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# ── Config ────────────────────────────────────────────────────────────────────
EMAIL          = os.getenv("OPENFX_EMAIL",    "amitkumar@tazapay.com")
PASSWORD       = os.getenv("OPENFX_PASSWORD", "Sep*19912021")
SESSION_FILE   = os.getenv("OPENFX_SESSION_FILE",   "openfx_session.pkl")
SCREENSHOT_DIR = os.getenv("OPENFX_SCREENSHOT_DIR", "screenshots")
HEADLESS       = os.getenv("OPENFX_HEADLESS", "false").lower() == "true"
BASE_URL       = "https://app.openfx.com"
TRADE_URL      = f"{BASE_URL}/trade"

_secret_file = Path("totp_secret.txt")
TOTP_SECRET  = (
    os.getenv("OPENFX_TOTP_SECRET")
    or (_secret_file.read_text().strip() if _secret_file.exists() else None)
    or "IVFGC63TJNYDA6L3EVSDK23OENCD4V2INZXXQYK2G4SE4PDYKQRVCW3WKUZEWLBXKZPG64R6GMXFAJLSPNBSMJJYJVPHQOKKEFTHC2A"
)

# ── Date range: always last 10 days up to today ───────────────────────────────
END_DATE   = date.today()
START_DATE = END_DATE - timedelta(days=10)

MONTH_NAMES = {
    1: "January",  2: "February",  3: "March",     4: "April",
    5: "May",      6: "June",      7: "July",       8: "August",
    9: "September",10: "October",  11: "November",  12: "December",
}

Path(SCREENSHOT_DIR).mkdir(exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def shot(driver, name: str) -> str:
    p = f"{SCREENSHOT_DIR}/{name}_{datetime.now().strftime('%H%M%S')}.png"
    driver.save_screenshot(p)
    return p


def get_totp() -> str:
    t = pyotp.TOTP(TOTP_SECRET)
    remaining = t.interval - (int(time.time()) % t.interval)
    if remaining < 5:
        print(f"[INFO] TOTP expiring in {remaining}s — waiting...", file=sys.stderr)
        time.sleep(remaining + 1)
    code = t.now()
    print(f"[INFO] TOTP code: {code}", file=sys.stderr)
    return code


def load_session(driver):
    driver.get(BASE_URL)
    time.sleep(2)
    for c in pickle.loads(Path(SESSION_FILE).read_bytes()):
        try:
            driver.add_cookie(c)
        except Exception:
            pass
    print(f"[INFO] Session loaded <- {SESSION_FILE}", file=sys.stderr)


def save_session(driver):
    Path(SESSION_FILE).write_bytes(pickle.dumps(driver.get_cookies()))
    print(f"[INFO] Session saved -> {SESSION_FILE}", file=sys.stderr)


def wait_cloudflare(driver, timeout=120) -> bool:
    deadline = time.time() + timeout
    warned   = False
    while time.time() < deadline:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, "[data-testid='sign-in-continue-button']")
            if not btn.get_attribute("disabled") and btn.get_attribute("aria-disabled") != "true":
                return True
        except Exception:
            pass
        if not warned:
            try:
                driver.find_element(By.XPATH, "//*[contains(text(),'Verify you are human')]")
                print("\n⚠️  Cloudflare — please click the checkbox in the browser!\n", file=sys.stderr)
                warned = True
            except Exception:
                pass
        time.sleep(0.5)
    return False


def do_full_login(driver) -> bool:
    driver.get(f"{BASE_URL}/sign-in")
    time.sleep(2)

    for sel in ["input[type='email']", "input[name='email']"]:
        try:
            f = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
            f.click(); f.clear(); f.send_keys(EMAIL)
            break
        except Exception:
            pass

    try:
        p = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']")))
        p.click(); p.clear(); p.send_keys(PASSWORD)
    except Exception:
        pass

    time.sleep(1)
    print("[INFO] Waiting for Cloudflare...", file=sys.stderr)
    if not wait_cloudflare(driver):
        return False

    driver.find_element(By.CSS_SELECTOR, "[data-testid='sign-in-continue-button']").click()
    time.sleep(4)

    # TOTP — Option A: 6 individual digit boxes
    otp_filled = False
    try:
        boxes = WebDriverWait(driver, 5).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "input[maxlength='1']")))
        if len(boxes) >= 6:
            code = get_totp()
            for i, digit in enumerate(code[:6]):
                boxes[i].click(); boxes[i].clear(); boxes[i].send_keys(digit)
                time.sleep(0.1)
            otp_filled = True
    except Exception:
        pass

    # TOTP — Option B: single OTP input
    if not otp_filled:
        for sel in ["input[maxlength='6']", "input[placeholder*='code' i]",
                    "input[placeholder*='otp' i]", "input[name*='otp']"]:
            try:
                f = WebDriverWait(driver, 4).until(EC.visibility_of_element_located((By.CSS_SELECTOR, sel)))
                f.click(); f.clear(); f.send_keys(get_totp())
                otp_filled = True
                break
            except TimeoutException:
                continue

    if otp_filled:
        time.sleep(1)
        try:
            WebDriverWait(driver, 4).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))).click()
        except Exception:
            try:
                driver.find_element(By.CSS_SELECTOR, "input").send_keys("\n")
            except Exception:
                pass
        time.sleep(5)

    save_session(driver)
    return True


# ── Calendar helpers ──────────────────────────────────────────────────────────

def get_visible_months(driver) -> list[tuple[str, int]]:
    visible = []
    try:
        els = driver.find_elements(
            By.XPATH,
            "//*[contains(text(),'2025') or contains(text(),'2026') or contains(text(),'2027')]",
        )
        for el in els:
            txt = el.text.strip()
            for num, name in MONTH_NAMES.items():
                for yr in range(2024, 2029):
                    if f"{name} {yr}" in txt:
                        visible.append((name, yr))
    except Exception:
        pass
    return list(dict.fromkeys(visible))


def navigate_calendar_to(driver, target_month: str, target_year: int, max_clicks=24) -> bool:
    target_abs = target_year * 12 + next(k for k, v in MONTH_NAMES.items() if v == target_month)

    for _ in range(max_clicks):
        visible = get_visible_months(driver)
        print(f"[INFO] Calendar visible: {visible}", file=sys.stderr)

        if any(m == target_month and y == target_year for m, y in visible):
            return True

        if visible:
            fn, fy = visible[0]
            first_abs  = fy * 12 + next(k for k, v in MONTH_NAMES.items() if v == fn)
            go_forward = target_abs > first_abs
        else:
            go_forward = True

        arrow = (
            "//button[@aria-label='Go to next month' or @aria-label='Next month' or @aria-label='next']"
            if go_forward else
            "//button[@aria-label='Go to previous month' or @aria-label='Previous month' or @aria-label='prev']"
        )
        try:
            driver.find_element(By.XPATH, arrow).click()
        except Exception:
            try:
                btns = driver.find_elements(
                    By.XPATH, "//button[.//*[name()='svg']][not(contains(@aria-label,'Filter'))]")
                if btns:
                    btns[-1 if go_forward else 0].click()
            except Exception:
                pass
        time.sleep(0.5)

    return False


def click_day_in_calendar(driver, month_name: str, year: int, day: int) -> bool:
    label    = f"{month_name} {year}"
    month_el = None

    for xpath in [
        f"//*[normalize-space(text())='{label}']",
        f"//*[contains(text(),'{label}')]",
    ]:
        try:
            els = driver.find_elements(By.XPATH, xpath)
            for el in els:
                try:
                    el.find_element(By.XPATH,
                        "ancestor::*[@role='dialog' or contains(@class,'modal') "
                        "or contains(@class,'calendar')]")
                    month_el = el
                    break
                except Exception:
                    month_el = el
            if month_el:
                break
        except Exception:
            pass

    if month_el:
        day_el = driver.execute_script("""
            var monthEl   = arguments[0];
            var targetDay = arguments[1];
            var container = monthEl.parentElement;
            for (var i = 0; i < 12; i++) {
                if (!container || container.tagName === 'BODY') break;
                var candidates = Array.from(
                    container.querySelectorAll(
                        'button, td, [role="button"], [role="gridcell"]'
                    )
                ).filter(function(el) {
                    var t = el.textContent.trim();
                    return t === String(targetDay)
                        && !el.disabled
                        && !el.getAttribute('disabled')
                        && !el.classList.contains('outside')
                        && !el.classList.contains('disabled')
                        && !el.classList.contains('rdp-day_outside');
                });
                if (candidates.length > 0) return candidates[0];
                container = container.parentElement;
            }
            return null;
        """, month_el, day)

        if day_el:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'})", day_el)
            time.sleep(0.3)
            day_el.click()
            print(f"[INFO] Clicked {label} {day}", file=sys.stderr)
            return True

    for xp in [
        f"//table[.//thead//*[normalize-space(text())='{label}']]"
        f"//td[normalize-space(text())='{day}'][not(@disabled)][not(contains(@class,'outside'))]",
        f"//*[normalize-space(text())='{label}']/following::button"
        f"[normalize-space(text())='{day}'][not(@disabled)][1]",
        f"//*[normalize-space(text())='{label}']/following::td"
        f"[normalize-space(text())='{day}'][not(@disabled)][1]",
    ]:
        try:
            el = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.XPATH, xp)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'})", el)
            time.sleep(0.2)
            el.click()
            print(f"[INFO] Clicked {label} {day} (XPath fallback)", file=sys.stderr)
            return True
        except Exception:
            continue

    print(f"[WARN] Could not click {label} {day}", file=sys.stderr)
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    result = {
        "success":     False,
        "step":        "",
        "start_date":  START_DATE.isoformat(),
        "end_date":    END_DATE.isoformat(),
        "error":       "",
        "screenshots": [],
    }

    print(f"[INFO] Date range  : {START_DATE}  ->  {END_DATE}", file=sys.stderr)

    options = uc.ChromeOptions()
    options.add_argument("--window-size=1400,900")
    options.add_argument("--no-sandbox")
    if HEADLESS:
        options.add_argument("--headless=new")

    driver = uc.Chrome(options=options, use_subprocess=True)
    driver.implicitly_wait(5)

    try:
        # ── 1. Login / restore session ────────────────────────────────────────
        result["step"] = "login"
        if Path(SESSION_FILE).exists():
            load_session(driver)
        else:
            if not do_full_login(driver):
                result["error"] = "Login failed"
                print(json.dumps(result)); return

        # ── 2. Navigate to Trade page ─────────────────────────────────────────
        result["step"] = "navigate"
        driver.get(TRADE_URL)
        time.sleep(3)

        if "sign-in" in driver.current_url or "login" in driver.current_url.lower():
            print("[INFO] Session expired — logging in again...", file=sys.stderr)
            if not do_full_login(driver):
                result["error"] = "Re-login failed"
                print(json.dumps(result)); return
            driver.get(TRADE_URL)
            time.sleep(3)

        result["screenshots"].append(shot(driver, "01_trade_page"))

        # ── 3. Click Download icon ────────────────────────────────────────────
        result["step"] = "download"
        driver.execute_script("window.scrollBy(0, 400)")
        time.sleep(0.8)

        download_btn = None
        for sel in ["[data-testid*='export' i]", "[aria-label*='download' i]",
                    "[aria-label*='export' i]", "[title*='download' i]", "[title*='export' i]"]:
            try:
                download_btn = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
                break
            except TimeoutException:
                continue

        if not download_btn:
            for btn in driver.find_elements(By.CSS_SELECTOR, "button"):
                html = btn.get_attribute("outerHTML").lower()
                if any(k in html for k in ["download", "export", "arrow-down"]):
                    download_btn = btn
                    break

        if not download_btn:
            result["error"] = "Download button not found on Trade page"
            print(json.dumps(result)); return

        driver.execute_script("arguments[0].scrollIntoView({block:'center'})", download_btn)
        time.sleep(0.3)
        download_btn.click()
        time.sleep(2)
        result["screenshots"].append(shot(driver, "02_download_clicked"))

        # ── 4. Click "Custom dates" ───────────────────────────────────────────
        result["step"] = "custom_dates"
        custom_el = None
        for xp in ["//*[text()='Custom dates']", "//*[contains(text(),'Custom dates')]",
                   "//*[text()='Custom']"]:
            try:
                custom_el = WebDriverWait(driver, 4).until(
                    EC.element_to_be_clickable((By.XPATH, xp)))
                break
            except Exception:
                continue

        if not custom_el:
            result["error"] = "'Custom dates' option not found in dropdown"
            print(json.dumps(result)); return

        custom_el.click()
        time.sleep(1.5)
        result["screenshots"].append(shot(driver, "03_custom_selected"))

        # ── 5. Navigate calendar to start month ───────────────────────────────
        result["step"] = "calendar_nav"
        start_month_name = MONTH_NAMES[START_DATE.month]
        end_month_name   = MONTH_NAMES[END_DATE.month]

        navigate_calendar_to(driver, start_month_name, START_DATE.year)
        time.sleep(0.5)

        # ── 6. Click start date ───────────────────────────────────────────────
        result["step"] = "start_date"
        if not click_day_in_calendar(driver, start_month_name, START_DATE.year, START_DATE.day):
            result["error"] = f"Could not click start date {START_DATE}"
            print(json.dumps(result)); return
        time.sleep(0.8)
        result["screenshots"].append(shot(driver, "04_start_date"))

        # Navigate to end month if different
        if (END_DATE.year, END_DATE.month) != (START_DATE.year, START_DATE.month):
            navigate_calendar_to(driver, end_month_name, END_DATE.year)
            time.sleep(0.5)

        # ── 7. Click end date ─────────────────────────────────────────────────
        result["step"] = "end_date"
        if not click_day_in_calendar(driver, end_month_name, END_DATE.year, END_DATE.day):
            result["error"] = f"Could not click end date {END_DATE}"
            print(json.dumps(result)); return
        time.sleep(0.8)
        result["screenshots"].append(shot(driver, "05_end_date"))

        # ── 8. Click Export (wait up to 6s for it to become enabled) ─────────
        result["step"] = "export"
        export_btn = None
        deadline = time.time() + 6
        while time.time() < deadline and not export_btn:
            for xp in [
                "//*[@role='dialog' or contains(@class,'modal')]"
                "//button[normalize-space(text())='Export']",
                "//button[normalize-space(text())='Export'][not(@disabled)]",
                "//*[normalize-space(text())='Export'][not(@disabled)]",
            ]:
                try:
                    el = driver.find_element(By.XPATH, xp)
                    if not el.get_attribute("disabled"):
                        export_btn = el
                        break
                except Exception:
                    pass
            if not export_btn:
                time.sleep(0.5)

        if not export_btn:
            result["screenshots"].append(shot(driver, "export_btn_disabled"))
            result["error"] = "Export button not found or disabled — date selection may have failed"
            print(json.dumps(result)); return

        driver.execute_script("arguments[0].scrollIntoView({block:'center'})", export_btn)
        time.sleep(0.3)
        export_btn.click()
        print("[INFO] Export clicked!", file=sys.stderr)
        time.sleep(3)
        result["screenshots"].append(shot(driver, "06_exported"))

        result["success"] = True
        result["message"] = (
            f"Export triggered: "
            f"{START_DATE.strftime('%d %b %Y')} -> {END_DATE.strftime('%d %b %Y')}"
        )

    except Exception as e:
        result["error"] = str(e)
        try:
            result["screenshots"].append(shot(driver, "error"))
        except Exception:
            pass
    finally:
        time.sleep(2)
        try:
            driver.quit()
        except Exception:
            pass

    print(json.dumps(result, indent=2))
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
