import os
import time
import hmac
import base64
import struct
import hashlib
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright
import boto3


# ─────────────────────────────────────────────
# ENV
# ─────────────────────────────────────────────

EMAIL = os.getenv("COINSPH_EMAIL")
PASSWORD = os.getenv("COINSPH_PASSWORD")
SECRET = os.getenv("COINSPH_TOTP_SECRET")

AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")

S3_BUCKET = os.getenv("S3_BUCKET")
S3_PREFIX = os.getenv("S3_PREFIX", "coinsph_fx/raw")
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-1")

BASE_URL = "https://www.coins.ph/en-ph"

MAX_ATTEMPTS = 3


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def ensure_artifacts():
    os.makedirs("artifacts", exist_ok=True)


def save_debug(page, name):
    ensure_artifacts()

    ts = int(time.time())

    png = f"artifacts/{name}_{ts}.png"
    html = f"artifacts/{name}_{ts}.html"

    try:
        page.screenshot(path=png, full_page=True)
        print(f"[*] Screenshot: {png}")
    except Exception as e:
        print(f"[!] Screenshot failed: {e}")

    try:
        with open(html, "w", encoding="utf-8") as f:
            f.write(page.content())
        print(f"[*] HTML saved: {html}")
    except Exception as e:
        print(f"[!] HTML save failed: {e}")


def get_totp(secret):
    pad = len(secret) % 8

    if pad:
        secret += "=" * (8 - pad)

    key = base64.b32decode(secret.upper())

    msg = struct.pack(">Q", int(time.time() // 30))

    h = hmac.new(key, msg, hashlib.sha1).digest()

    offset = h[-1] & 0x0F

    code = (
        struct.unpack(">I", h[offset:offset + 4])[0]
        & 0x7FFFFFFF
    ) % 1000000

    return f"{code:06d}"


# ─────────────────────────────────────────────
# S3
# ─────────────────────────────────────────────

def upload_to_s3(local_path, s3_key):

    print(f"[*] Uploading -> s3://{S3_BUCKET}/{s3_key}")

    client = boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
        region_name=AWS_REGION,
    )

    client.upload_file(local_path, S3_BUCKET, s3_key)

    print("[✓] Upload completed")


# ─────────────────────────────────────────────
# WAIT HELPERS
# ─────────────────────────────────────────────

def wait_for_password(page):

    selectors = [
        "input[type='password']",
        "input[name='password']",
        "input[placeholder*='Password']",
    ]

    for _ in range(30):

        for sel in selectors:
            try:
                el = page.locator(sel).first

                if el.is_visible():
                    print(f"[✓] Password field found: {sel}")
                    return el

            except Exception:
                pass

        page.wait_for_timeout(1000)

    return None


# ─────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────

def login(page):

    print("[*] Opening login page")

    page.goto(
        f"{BASE_URL}/login",
        wait_until="networkidle",
        timeout=120000,
    )

    page.wait_for_timeout(5000)

    save_debug(page, "login_page")

    print(f"[*] Current URL: {page.url}")

    # EMAIL TAB

    for sel in [
        "button:has-text('Email')",
        "a:has-text('Email')",
        "[role='tab']:has-text('Email')",
    ]:
        try:
            page.locator(sel).first.click(timeout=5000)
            print("[✓] Email tab selected")
            break
        except Exception:
            continue

    page.wait_for_timeout(3000)

    # EMAIL FIELD

    email_ok = False

    for sel in [
        "input[type='email']",
        "input[name='email']",
        "input[type='text']",
    ]:
        try:
            el = page.locator(sel).first

            el.wait_for(state="visible", timeout=10000)

            el.click()

            el.fill(EMAIL)

            print(f"[✓] Email entered using {sel}")

            email_ok = True

            break

        except Exception:
            continue

    if not email_ok:
        save_debug(page, "email_not_found")
        raise RuntimeError("Email field not found")

    page.wait_for_timeout(2000)

    # NEXT BUTTON

    clicked = False

    for name in ["Next", "Continue", "Login", "Log in"]:
        try:
            page.get_by_role("button", name=name).click(timeout=5000)

            print(f"[✓] Clicked {name}")

            clicked = True

            break

        except Exception:
            continue

    if not clicked:
        save_debug(page, "next_button_missing")
        raise RuntimeError("Next/Login button not found")

    # WAIT FOR PASSWORD SCREEN

    print("[*] Waiting for password field")

    page.wait_for_load_state("networkidle")

    page.wait_for_timeout(8000)

    password_input = wait_for_password(page)

    if not password_input:
        save_debug(page, "password_not_found")
        raise RuntimeError("Password field not found")

    password_input.click()

    password_input.fill(PASSWORD)

    print("[✓] Password entered")

    page.wait_for_timeout(1000)

    # LOGIN BUTTON

    for name in ["Login", "Log in", "Sign in", "Next"]:
        try:
            page.get_by_role("button", name=name).click(timeout=5000)

            print(f"[✓] Clicked {name}")

            break

        except Exception:
            continue

    page.wait_for_timeout(5000)

    # OTP

    code = get_totp(SECRET)

    print(f"[*] TOTP: {code}")

    otp_ok = False

    try:
        inputs = page.locator("input[maxlength='1']")

        if inputs.count() >= 6:

            for i, d in enumerate(code):
                inputs.nth(i).fill(d)

            otp_ok = True

    except Exception:
        pass

    if not otp_ok:

        for sel in [
            "input[autocomplete='one-time-code']",
            "input[inputmode='numeric']",
            "input[maxlength='6']",
        ]:
            try:
                page.locator(sel).first.fill(code)

                otp_ok = True

                break

            except Exception:
                continue

    if not otp_ok:
        save_debug(page, "otp_not_found")
        raise RuntimeError("OTP field not found")

    print("[✓] OTP entered")

    # VERIFY

    for name in ["Verify", "Submit", "Confirm", "Next"]:
        try:
            page.get_by_role("button", name=name).click(timeout=5000)
            break
        except Exception:
            continue

    page.wait_for_timeout(10000)

    save_debug(page, "after_login")

    print(f"[*] Current URL after login: {page.url}")

    if "dashboard" not in page.url.lower():
        raise RuntimeError("Dashboard not reached")

    print("[✓] Logged in")


# ─────────────────────────────────────────────
# EXPORT
# ─────────────────────────────────────────────

def export_csv(page):

    print("[*] Starting export")

    page.wait_for_timeout(5000)

    with page.expect_download(timeout=120000) as dl:

        page.locator("text=Export").last.click()

    download = dl.value

    ensure_artifacts()

    filename = (
        f"coinsph_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )

    local_path = f"artifacts/{filename}"

    download.save_as(local_path)

    print(f"[✓] Downloaded: {local_path}")

    return local_path


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run():

    with sync_playwright() as p:

        browser = p.chromium.launch(

            headless=True,

            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-gpu",
                "--window-size=1920,1080",
            ],
        )

        context = browser.new_context(

            viewport={"width": 1920, "height": 1080},

            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),

            locale="en-US",
        )

        page = context.new_page()

        try:

            login(page)

            local_file = export_csv(page)

            s3_key = (
                f"{S3_PREFIX}/"
                f"{os.path.basename(local_file)}"
            )

            upload_to_s3(local_file, s3_key)

            print("\n[✓] SUCCESS")

        except Exception as e:

            print(f"\n[x] FLOW FAILED: {e}")

            save_debug(page, "fatal_error")

            raise

        finally:
            browser.close()


# ─────────────────────────────────────────────
# RETRY WRAPPER
# ─────────────────────────────────────────────

def run_with_retry():

    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):

        print("\n" + "=" * 60)
        print(f"ATTEMPT {attempt}/{MAX_ATTEMPTS}")
        print("=" * 60)

        try:
            run()
            return

        except Exception as e:

            last_error = e

            print(f"[!] Attempt failed: {e}")

            if attempt < MAX_ATTEMPTS:

                wait = attempt * 10

                print(f"[*] Retrying in {wait}s")

                time.sleep(wait)

    raise last_error


if __name__ == "__main__":
    run_with_retry()
