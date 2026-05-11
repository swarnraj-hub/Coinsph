"""
OpenFX — Trade History Export Automation
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Designed for n8n Execute Command node.
Dates are DYNAMIC — passed via env vars or CLI args.

n8n Code node example:
  const start = $json.start_date;   // "2026-04-24"
  const end   = $json.end_date;     // "2026-05-11"
  return [{ json: {
    command: "python openfx_download_trade.py",
    env: { OPENFX_START_DATE: start, OPENFX_END_DATE: end }
  }}];

CLI usage:
  python openfx_download_trade.py --start 2026-04-24 --end 2026-05-11

Env vars (all optional with defaults):
  OPENFX_EMAIL          default: amitkumar@tazapay.com
  OPENFX_PASSWORD       default: from script
  OPENFX_TOTP_SECRET    default: read from totp_secret.txt
  OPENFX_START_DATE     format:  YYYY-MM-DD  (default: 1st of current month)
  OPENFX_END_DATE       format:  YYYY-MM-DD  (default: today)
  OPENFX_HEADLESS       true/false (default: false)
  OPENFX_SESSION_FILE   default: openfx_session.pkl
  OPENFX_SCREENSHOT_DIR default: screenshots

Output: JSON to stdout  { success, message, start_date, end_date, error }
Exit 0 = success, Exit 1 = failure
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import argparse
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

# ── Parse CLI args (override env vars) ───────────────────────────────────────
parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--start", default=None)
parser.add_argument("--end",   default=None)
args, _ = parser.parse_known_args()

# ── Config ────────────────────────────────────────────────────────────────────
EMAIL        = os.getenv("OPENFX_EMAIL",    "amitkumar@tazapay.com")
PASSWORD     = os.getenv("OPENFX_PASSWORD", "Sep*19912021")
SESSION_FILE = os.getenv("OPENFX_SESSION_FILE", "openfx_session.pkl")
SCREENSHOT_DIR = os.getenv("OPENFX_SCREENSHOT_DIR", "screenshots")
HEADLESS     = os.getenv("OPENFX_HEADLESS", "false").lower() == "true"
BASE_URL     = "https://app.openfx.com"
TRADE_URL    = f"{BASE_URL}/trade"

# TOTP secret — read from file, then env var, then hardcoded fallback
_secret_file = Path("totp_secret.txt")
TOTP_SECRET  = (
    os.getenv("OPENFX_TOTP_SECRET")
    or (_secret_file.read_text().strip() if _secret_file.exists() else None)
    or "IVFGC63TJNYDA6L3EVSDK23OENCD4V2INZXXQYK2G4SE4PDYKQRVCW3WKUZEWLBXKZPG64R6GMXFAJLSPNBSMJJYJVPHQOKKEFTHC2A"
)

# ── Dynamic dates ─────────────────────────────────────────────────────────────
def _parse_date(val, fallback):
    if not val:
        return fallback
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(val, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognised date format: {val!r}. Use YYYY-MM-DD.")

today          = date.today()
first_of_month = today.replace(day=1)

_raw_start = args.start or os.getenv("OPENFX_START_DATE")
_raw_end   = args.end   or os.getenv("OPENFX_END_DATE")

START_DATE = _parse_date(_raw_start, first_of_month)
END_DATE   = _parse_date(_raw_end,   today)

if START_DATE > END_DATE:
    START_DATE, END_DATE = END_DATE, START_DATE

MONTH_NAMES = {
    1:"January", 2:"February", 3:"March",    4:"April",
    5:"May",     6:"June",     7:"July",     8:"August",
    9:"September",10:"October",11:"November",12:"December",
}

Path(SCREENSHOT_DIR).mkdir(exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def shot(driver, name):
    p = f"{SCREENSHOT_DIR}/{name}_{datetime.now().strftime('%H%M%S')}.png"
    driver.save_screenshot(p)
    return p


def get_totp():
    t = pyotp.TOTP(TOTP_SECRET)
    remaining = t.interval - (int(time.time()) % t.interval)
    if remaining < 5:
        print(f"[INFO] TOTP expiring in {remaining}s — waiting...", file=sys.stderr)
        time.sleep(remaining + 1)
    code = t.now()
    print(f"[INFO] TOTP: {code}", file=sys.stderr)
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


def wait_cloudflare(driver, timeout=120):
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
                print("\n⚠️  Cloudflare: please click the checkbox in the browser!\n", file=sys.stderr)
                warned = True
            except Exception:
                pass
        time.sleep(0.5)
    return False


def do_full_login(driver):
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
        p = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']")))
        p.click(); p.clear(); p.send_keys(PASSWORD)
    except Exception:
        pass

    time.sleep(1)
    print("[INFO] Waiting for Cloudflare...", file=sys.stderr)
    if not wait_cloudflare(driver):
        return False

    driver.find_element(By.CSS_SELECTOR, "[data-testid='sign-in-continue-button']").click()
    time.sleep(4)

    for sel in ["input[maxlength='6']", "input[placeholder*='code' i]", "input[placeholder*='otp' i]"]:
        try:
            f = WebDriverWait(driver, 5).until(EC.visibility_of_element_located((By.CSS_SELECTOR, sel)))
            f.click(); f.clear(); f.send_keys(get_totp())
            time.sleep(1)
            try:
                WebDriverWait(driver, 4).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']"))).click()
            except Exception:
                f.send_keys("\n")
            time.sleep(5)
            break
        except TimeoutException:
            continue

    save_session(driver)
    return True


# ── Calendar helpers ──────────────────────────────────────────────────────────

def get_visible_months(driver):
    visible = []
    try:
        all_els = driver.find_elements(By.XPATH,
            "//*[contains(text(),'2025') or contains(text(),'2026') or contains(text(),'2027')]")
        for el in all_els:
            txt = el.text.strip()
            for mn, name in MONTH_NAMES.items():
                for yr in range(2024, 2028):
                    if f"{name} {yr}" in txt:
                        visible.append((name, yr))
    except Exception:
        pass
    return list(dict.fromkeys(visible))


def navigate_calendar_to(driver, target_month, target_year, max_clicks=24):
    for _ in range(max_clicks):
        visible = get_visible_months(driver)
        print(f"[INFO] Calendar visible: {visible}", file=sys.stderr)
        if any(m == target_month and y == target_year for m, y in visible):
            return True

        if visible:
            first_name, first_year = visible[0]
            first_num  = next(k for k, v in MONTH_NAMES.items() if v == first_name)
            target_num = next(k for k, v in MONTH_NAMES.items() if v == target_month)
            go_forward = (target_year * 12 + target_num) > (first_year * 12 + first_num)
        else:
            go_forward = True

        arrow_xpath = (
            "//button[@aria-label='Go to next month' or @aria-label='Next month']"
            if go_forward else
            "//button[@aria-label='Go to previous month' or @aria-label='Previous month']"
        )
        try:
            driver.find_element(By.XPATH, arrow_xpath).click()
        except Exception:
            try:
                arrows = driver.find_elements(By.XPATH,
                    "//button[.//*[name()='svg']][not(contains(@aria-label,'Filter'))]")
                if arrows:
                    arrows[-1 if go_forward else 0].click()
            except Exception:
                pass
        time.sleep(0.5)
    return False


def click_day_in_calendar(driver, month_name, year, day):
    label = f"{month_name} {year}"

    month_el = None
    for xpath in [f"//*[normalize-space(text())='{label}']", f"//*[contains(text(),'{label}')]"]:
        try:
            els = driver.find_elements(By.XPATH, xpath)
            for el in els:
                try:
                    el.find_element(By.XPATH,
                        "ancestor::*[@role='dialog' or contains(@class,'modal') or contains(@class,'calendar')]")
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
            var monthEl = arguments[0];
            var day     = arguments[1];
            var container = monthEl.parentElement;
            for (var i = 0; i < 12; i++) {
                if (!container || container.tagName === 'BODY') break;
                var candidates = Array.from(
                    container.querySelectorAll('button, td, [role="button"], [role="gridcell"]')
                ).filter(function(el) {
                    var t = el.textContent.trim();
                    return t === String(day)
                        && !el.disabled
                        && !el.getAttribute('disabled')
                        && !el.classList.contains('outside')
                        && !el.classList.contains('disabled')
                        && !el.classList.contains('rdp-day_outside');
                });
                if (candidates.length > 0) { return candidates[0]; }
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
        f"//table[.//thead//*[normalize-space(text())='{label}']]//td[normalize-space(text())='{day}'][not(@disabled)]",
        f"//*[normalize-space(text())='{label}']/following::button[normalize-space(text())='{day}'][not(@disabled)][1]",
        f"//*[normalize-space(text())='{label}']/following::td[normalize-space(text())='{day}'][not(@disabled)][1]",
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

    print(f"[INFO] Date range : {START_DATE}  ->  {END_DATE}", file=sys.stderr)

    options = uc.ChromeOptions()
    options.add_argument("--window-size=1400,900")
    options.add_argument("--no-sandbox")
    if HEADLESS:
        options.add_argument("--headless=new")

    driver = uc.Chrome(options=options, use_subprocess=True)
    driver.implicitly_wait(5)

    try:
        # 1. Session / Login
        result["step"] = "login"
        if Path(SESSION_FILE).exists():
            load_session(driver)
        else:
            if not do_full_login(driver):
                result["error"] = "Login failed"
                print(json.dumps(result)); return

        # 2. Navigate to Trade
        result["step"] = "navigate"
        driver.get(TRADE_URL)
        time.sleep(3)

        if "sign-in" in driver.current_url or "login" in driver.current_url.lower():
            print("[INFO] Session expired — re-logging in...", file=sys.stderr)
            if not do_full_login(driver):
                result["error"] = "Re-login failed"
                print(json.dumps(result)); return
            driver.get(TRADE_URL)
            time.sleep(3)

        result["screenshots"].append(shot(driver, "01_trade_page"))

        # 3. Click Download
        result["step"] = "download"
        driver.execute_script("window.scrollBy(0, 400)")
        time.sleep(0.8)

        download_btn = None
        for sel in ["[data-testid*='export' i]","[aria-label*='download' i]",
                    "[aria-label*='export' i]","[title*='download' i]","[title*='export' i]"]:
            try:
                download_btn = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
                break
            except TimeoutException:
                continue

        if not download_btn:
            for btn in driver.find_elements(By.CSS_SELECTOR, "button"):
                if any(k in btn.get_attribute("outerHTML").lower() for k in ["download","export","arrow-down"]):
                    download_btn = btn; break

        if not download_btn:
            result["error"] = "Download button not found"; print(json.dumps(result)); return

        driver.execute_script("arguments[0].scrollIntoView({block:'center'})", download_btn)
        time.sleep(0.3)
        download_btn.click()
        time.sleep(2)
        result["screenshots"].append(shot(driver, "02_download_clicked"))

        # 4. Select "Custom dates"
        result["step"] = "custom_dates"
        custom_el = None
        for sel in ["//*[text()='Custom dates']", "//*[contains(text(),'Custom dates')]", "//*[text()='Custom']"]:
            try:
                custom_el = WebDriverWait(driver, 4).until(EC.element_to_be_clickable((By.XPATH, sel))); break
            except Exception:
                continue

        if not custom_el:
            result["error"] = "'Custom dates' not found"; print(json.dumps(result)); return

        custom_el.click()
        time.sleep(1.5)
        result["screenshots"].append(shot(driver, "03_custom_selected"))

        # 5. Navigate calendar + click start date
        result["step"] = "start_date"
        start_month = MONTH_NAMES[START_DATE.month]
        end_month   = MONTH_NAMES[END_DATE.month]

        navigate_calendar_to(driver, start_month, START_DATE.year)
        time.sleep(0.5)

        if not click_day_in_calendar(driver, start_month, START_DATE.year, START_DATE.day):
            result["error"] = f"Could not click start date {START_DATE}"; print(json.dumps(result)); return
        time.sleep(0.8)
        result["screenshots"].append(shot(driver, "04_start_date"))

        # 6. Navigate to end month if different + click end date
        result["step"] = "end_date"
        if (END_DATE.year, END_DATE.month) != (START_DATE.year, START_DATE.month):
            navigate_calendar_to(driver, end_month, END_DATE.year)
            time.sleep(0.5)

        if not click_day_in_calendar(driver, end_month, END_DATE.year, END_DATE.day):
            result["error"] = f"Could not click end date {END_DATE}"; print(json.dumps(result)); return
        time.sleep(0.8)
        result["screenshots"].append(shot(driver, "05_end_date"))

        # 7. Click Export (wait for it to become enabled)
        result["step"] = "export"
        export_btn = None
        deadline = time.time() + 6
        while time.time() < deadline and not export_btn:
            for sel in [
                "//*[@role='dialog' or contains(@class,'modal')]//button[normalize-space(text())='Export']",
                "//button[normalize-space(text())='Export'][not(@disabled)]",
                "//*[text()='Export'][not(@disabled)]",
            ]:
                try:
                    el = driver.find_element(By.XPATH, sel)
                    if not el.get_attribute("disabled"):
                        export_btn = el; break
                except Exception:
                    pass
            if not export_btn:
                time.sleep(0.5)

        if not export_btn:
            shot(driver, "export_btn_disabled")
            result["error"] = "Export button disabled — date selection may have failed"
            print(json.dumps(result)); return

        driver.execute_script("arguments[0].scrollIntoView({block:'center'})", export_btn)
        time.sleep(0.3)
        export_btn.click()
        print("[INFO] Export clicked!", file=sys.stderr)
        time.sleep(3)
        result["screenshots"].append(shot(driver, "06_exported"))

        result["success"] = True
        result["message"] = f"Export triggered: {START_DATE.strftime('%d %b %Y')} -> {END_DATE.strftime('%d %b %Y')}"

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
