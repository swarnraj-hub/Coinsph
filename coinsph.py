import os
import time
import hmac
import base64
import struct
import hashlib
from datetime import datetime, timedelta

import boto3

from playwright.sync_api import sync_playwright


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
# VALIDATION
# ─────────────────────────────────────────────

required = [
    "COINSPH_EMAIL",
    "COINSPH_PASSWORD",
    "COINSPH_TOTP_SECRET",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "S3_BUCKET",
]

missing = [x for x in required if not os.getenv(x)]

if missing:
    raise RuntimeError(f"Missing env vars: {missing}")


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def ensure_artifacts():
    os.makedirs("artifacts", exist_ok=True)


def save_screenshot(page, name):
    try:
        ensure_artifacts()

        path = f"artifacts/{name}_{int(time.time())}.png"

        page.screenshot(path=path, full_page=True)

        print(f"[*] Screenshot: {path}")

    except Exception as e:
        print(f"[!] Screenshot failed: {e}")


def get_totp(secret):
    secret = secret.strip().replace(" ", "")

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


def upload_to_s3(local_file, s3_key):
    print(f"[*] Uploading to s3://{S3_BUCKET}/{s3_key}")

    client = boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
        region_name=AWS_REGION,
    )

    client.upload_file(local_file, S3_BUCKET, s3_key)

    print("[✓] S3 upload complete")


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

    page.wait_for_timeout(10000)

    save_screenshot(page, "login_page")

    print(f"[*] Current URL: {page.url}")

    content = page.content().lower()

    if "just a moment" in content:
        print("[!] Cloudflare detected")
        page.wait_for_timeout(20000)

    popup_selectors = [
        "button:has-text('Accept')",
        "button:has-text('Allow')",
        "button:has-text('Close')",
        "[aria-label='Close']",
    ]

    for sel in popup_selectors:
        try:
            btn = page.locator(sel).first

            if btn.is_visible():
                btn.click(timeout=2000)
                print(f"[*] Closed popup: {sel}")

        except:
            pass

    # Email tab
    for sel in [
        "button:has-text('Email')",
        "a:has-text('Email')",
        "[role='tab']:has-text('Email')",
    ]:
        try:
            page.locator(sel).first.click(timeout=5000)
            print("[✓] Email tab selected")
            break
        except:
            pass

    page.wait_for_timeout(3000)

    email_selectors = [
        "input[type='email']",
        "input[name='email']",
        "input[placeholder*='Email']",
        "input[placeholder*='email']",
        "input[type='text']",
    ]

    email_ok = False

    # Main page
    for sel in email_selectors:
        try:
            el = page.locator(sel).first

            el.wait_for(state="visible", timeout=15000)

            el.click()

            el.fill(EMAIL)

            print(f"[✓] Email entered using {sel}")

            email_ok = True

            break

        except:
            continue

    # iframe fallback
    if not email_ok:

        print("[*] Trying iframe search")

        for frame in page.frames:

            for sel in email_selectors:

                try:
                    el = frame.locator(sel).first

                    el.wait_for(state="visible", timeout=5000)

                    el.fill(EMAIL)

                    print(f"[✓] iframe email success: {sel}")

                    email_ok = True

                    break

                except:
                    continue

            if email_ok:
                break

    if not email_ok:
        save_screenshot(page, "email_not_found")
        raise RuntimeError("Email field not found")

    page.keyboard.press("Enter")

    page.wait_for_timeout(5000)

    # Password
    pw_ok = False

    for sel in [
        "input[type='password']",
        "input[name='password']",
    ]:
        try:
            el = page.locator(sel).first

            el.wait_for(state="visible", timeout=15000)

            el.fill(PASSWORD)

            pw_ok = True

            print("[✓] Password entered")

            break

        except:
            continue

    if not pw_ok:
        save_screenshot(page, "password_not_found")
        raise RuntimeError("Password field not found")

    # Login button
    for name in ["Login", "Log in", "Sign in", "Continue", "Next"]:
        try:
            page.get_by_role("button", name=name).click(timeout=5000)
            print(f"[✓] Clicked {name}")
            break
        except:
            continue

    page.wait_for_timeout(5000)

    # OTP
    code = get_totp(SECRET)

    print(f"[*] OTP: {code}")

    otp_ok = False

    try:
        inputs = page.locator("input[maxlength='1']")

        if inputs.count() >= 6:

            for i, digit in enumerate(code):
                inputs.nth(i).fill(digit)

            otp_ok = True

    except:
        pass

    if not otp_ok:

        for sel in [
            "input[autocomplete='one-time-code']",
            "input[maxlength='6']",
            "input[inputmode='numeric']",
        ]:
            try:
                page.locator(sel).first.fill(code)

                otp_ok = True

                break

            except:
                continue

    if not otp_ok:
        save_screenshot(page, "otp_not_found")
        raise RuntimeError("OTP field not found")

    print("[✓] OTP entered")

    for name in ["Verify", "Submit", "Confirm", "Next"]:
        try:
            page.get_by_role("button", name=name).click(timeout=5000)
            break
        except:
            continue

    page.wait_for_timeout(10000)

    save_screenshot(page, "after_login")

    print("[✓] Login flow completed")


# ─────────────────────────────────────────────
# NAVIGATION
# ─────────────────────────────────────────────

def navigate(page):
    print("[*] Navigating")

    keywords = [
        "Orders",
        "Spot",
        "Trade History",
    ]

    for text in keywords:

        success = False

        for _ in range(3):

            try:
                page.get_by_text(text, exact=True).first.click(timeout=10000)

                page.wait_for_timeout(3000)

                print(f"[✓] Clicked {text}")

                success = True

                break

            except:
                continue

        if not success:
            save_screenshot(page, f"nav_fail_{text}")
            raise RuntimeError(f"Could not click {text}")

    page.wait_for_timeout(5000)


# ─────────────────────────────────────────────
# EXPORT
# ─────────────────────────────────────────────

def export_csv(page):
    print("[*] Exporting CSV")

    today = datetime.now()

    start = today - timedelta(days=10)

    export_ok = False

    for sel in [
        "button:has-text('Export')",
        "div:has-text('Export')",
        "span:has-text('Export')",
    ]:
        try:
            page.locator(sel).first.click(timeout=10000)

            export_ok = True

            print("[✓] Export clicked")

            break

        except:
            continue

    if not export_ok:
        save_screenshot(page, "export_missing")
        raise RuntimeError("Export button not found")

    page.wait_for_timeout(3000)

    with page.expect_download(timeout=120000) as dl_info:

        final_ok = False

        for sel in [
            "button:has-text('Export')",
            "button.mui-11wlovc",
        ]:
            try:
                page.locator(sel).last.click(timeout=10000)

                final_ok = True

                print("[✓] Final export clicked")

                break

            except:
                continue

        if not final_ok:
            raise RuntimeError("Final export click failed")

    download = dl_info.value

    ensure_artifacts()

    filename = (
        f"coinsph_trade_history_"
        f"{start.strftime('%Y-%m-%d')}_"
        f"{today.strftime('%Y-%m-%d')}.csv"
    )

    local_path = os.path.join("artifacts", filename)

    download.save_as(local_path)

    print(f"[✓] Downloaded: {local_path}")

    return local_path


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run():

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=False,
            slow_mo=300,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--start-maximized",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        try:

            login(page)

            navigate(page)

            csv_file = export_csv(page)

            s3_key = (
                f"{S3_PREFIX}/"
                f"{os.path.basename(csv_file)}"
            )

            upload_to_s3(csv_file, s3_key)

            print("\n[✓] COMPLETED SUCCESSFULLY")

        except Exception as e:

            print(f"\n[x] FLOW FAILED: {e}")

            save_screenshot(page, "fatal_error")

            raise

        finally:
            browser.close()


# ─────────────────────────────────────────────
# RETRY
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
