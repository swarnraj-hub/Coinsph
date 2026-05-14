import os
import time
import hmac
import struct
import base64
import hashlib

from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright
import boto3


# ─────────────────────────────────────────────────────────────
# ENV
# ─────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────
# VALIDATION
# ─────────────────────────────────────────────────────────────

required_env = [
    "COINSPH_EMAIL",
    "COINSPH_PASSWORD",
    "COINSPH_TOTP_SECRET",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "S3_BUCKET",
]

missing = [x for x in required_env if not os.getenv(x)]

if missing:
    raise RuntimeError(
        f"Missing required environment variables: {', '.join(missing)}"
    )


# ─────────────────────────────────────────────────────────────
# DEBUG
# ─────────────────────────────────────────────────────────────

def save_debug(page, tag):

    try:
        os.makedirs("artifacts", exist_ok=True)

        ts = int(time.time())

        png = f"artifacts/{tag}_{ts}.png"
        html = f"artifacts/{tag}_{ts}.html"

        page.screenshot(path=png, full_page=True)

        with open(html, "w", encoding="utf-8") as f:
            f.write(page.content())

        print(f"[*] Screenshot: {png}")
        print(f"[*] HTML saved: {html}")

    except Exception as e:
        print(f"[!] Debug save failed: {e}")


# ─────────────────────────────────────────────────────────────
# TOTP
# ─────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────
# S3
# ─────────────────────────────────────────────────────────────

def upload_to_s3(local_path, s3_key):

    print(f"[*] Uploading {local_path}")

    client = boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
        region_name=AWS_REGION,
    )

    client.upload_file(local_path, S3_BUCKET, s3_key)

    print(f"[✓] Uploaded to s3://{S3_BUCKET}/{s3_key}")


# ─────────────────────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────────────────────

def login(page):

    print("[*] Opening login page")

    page.goto(
        f"{BASE_URL}/login",
        wait_until="networkidle",
        timeout=120000,
    )

    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except:
        pass

    page.wait_for_timeout(5000)

    print("[*] Waiting for React hydration")

    try:
        page.wait_for_function(
            """
            () => {
                return document.querySelectorAll('input').length > 0
            }
            """,
            timeout=30000
        )
    except:
        pass

    save_debug(page, "login_page")

    print(f"[*] Current URL: {page.url}")

    # ─────────────────────────────────────────────────────────
    # COOKIE / POPUPS
    # ─────────────────────────────────────────────────────────

    for txt in [
        "Accept",
        "Allow",
        "Allow All",
        "Continue",
        "I Accept",
        "Got it",
        "Accept All",
    ]:
        try:
            page.get_by_role("button", name=txt).click(timeout=3000)

            print(f"[✓] Popup clicked: {txt}")

            page.wait_for_timeout(1500)

        except:
            pass

    # ─────────────────────────────────────────────────────────
    # EMAIL TAB
    # ─────────────────────────────────────────────────────────

    for sel in [
        "button:has-text('Email')",
        "[role='tab']:has-text('Email')",
        "text=Email",
        "div:has-text('Email')",
    ]:
        try:
            page.locator(sel).first.click(timeout=5000)

            print(f"[✓] Email tab clicked via {sel}")

            page.wait_for_timeout(2000)

            break

        except:
            continue

    # ─────────────────────────────────────────────────────────
    # EMAIL FIELD
    # ─────────────────────────────────────────────────────────

    email_found = False

    for i in range(3):

        print(f"[*] Email search attempt {i+1}/3")

        try:

            page.wait_for_timeout(3000)

            inputs = page.locator("input")

            count = inputs.count()

            print(f"[*] Total inputs found: {count}")

            for n in range(count):

                try:

                    el = inputs.nth(n)

                    typ = el.get_attribute("type")
                    name = el.get_attribute("name")
                    placeholder = el.get_attribute("placeholder")

                    print(
                        f"[*] Input {n}: "
                        f"type={typ} "
                        f"name={name} "
                        f"placeholder={placeholder}"
                    )

                    if typ in ["email", "text", None]:

                        el.click(timeout=2000)

                        el.fill(EMAIL)

                        print(f"[✓] Email entered in input #{n}")

                        email_found = True

                        break

                except Exception as e:
                    print(f"[!] Input scan failed: {e}")

            if email_found:
                break

        except Exception as e:
            print(f"[!] Email attempt failed: {e}")

    # iframe fallback
    if not email_found:

        print("[*] Trying iframe search")

        for frame in page.frames:

            try:

                inputs = frame.locator("input")

                count = inputs.count()

                for n in range(count):

                    try:

                        el = inputs.nth(n)

                        typ = el.get_attribute("type")

                        if typ in ["email", "text", None]:

                            el.fill(EMAIL)

                            print("[✓] Email filled inside iframe")

                            email_found = True

                            break

                    except:
                        continue

                if email_found:
                    break

            except:
                continue

    if not email_found:

        save_debug(page, "email_not_found")

        raise RuntimeError("Email field not found")

    page.wait_for_timeout(3000)

    save_debug(page, "after_email")

    # ─────────────────────────────────────────────────────────
    # NEXT BUTTON
    # ─────────────────────────────────────────────────────────

    next_clicked = False

    for txt in [
        "Next",
        "Continue",
        "Login",
        "Log in",
    ]:
        try:

            page.get_by_role("button", name=txt).click(timeout=5000)

            print(f"[✓] Clicked {txt}")

            next_clicked = True

            break

        except:
            continue

    if not next_clicked:
        page.keyboard.press("Enter")

    page.wait_for_timeout(5000)

    # ─────────────────────────────────────────────────────────
    # PASSWORD FIELD
    # ─────────────────────────────────────────────────────────

    pw_found = False

    for i in range(3):

        try:

            page.wait_for_timeout(2000)

            pw_inputs = page.locator("input[type='password']")

            count = pw_inputs.count()

            print(f"[*] Password inputs found: {count}")

            for n in range(count):

                try:

                    el = pw_inputs.nth(n)

                    el.wait_for(state="visible", timeout=10000)

                    el.click()

                    el.fill(PASSWORD)

                    print(f"[✓] Password entered in field #{n}")

                    pw_found = True

                    break

                except Exception as e:
                    print(f"[!] Password field failed: {e}")

            if pw_found:
                break

        except Exception as e:
            print(f"[!] Password search failed: {e}")

    if not pw_found:

        save_debug(page, "password_not_found")

        raise RuntimeError("Password field not found")

    page.wait_for_timeout(2000)

    # ─────────────────────────────────────────────────────────
    # LOGIN BUTTON
    # ─────────────────────────────────────────────────────────

    login_clicked = False

    for txt in [
        "Login",
        "Log in",
        "Sign in",
        "Next",
    ]:
        try:

            page.get_by_role("button", name=txt).click(timeout=5000)

            print(f"[✓] Clicked {txt}")

            login_clicked = True

            break

        except:
            continue

    if not login_clicked:
        page.keyboard.press("Enter")

    page.wait_for_timeout(8000)

    save_debug(page, "after_password")

    # ─────────────────────────────────────────────────────────
    # OTP
    # ─────────────────────────────────────────────────────────

    code = get_totp(SECRET)

    print(f"[*] TOTP: {code}")

    otp_ok = False

    try:

        inputs = page.locator("input[maxlength='1']")

        if inputs.count() >= 6:

            for i, d in enumerate(code):

                inputs.nth(i).fill(d)

            otp_ok = True

    except:
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

            except:
                continue

    if not otp_ok:

        save_debug(page, "otp_not_found")

        raise RuntimeError("OTP field not found")

    print("[✓] OTP entered")

    for txt in [
        "Verify",
        "Confirm",
        "Submit",
        "Next",
    ]:
        try:

            page.get_by_role("button", name=txt).click(timeout=5000)

            print(f"[✓] Clicked {txt}")

            break

        except:
            continue

    page.wait_for_timeout(10000)

    save_debug(page, "after_otp")

    print(f"[✓] Current URL: {page.url}")


# ─────────────────────────────────────────────────────────────
# EXPORT CSV
# ─────────────────────────────────────────────────────────────

def export_csv(page):

    print("[*] Export flow")

    today = datetime.now()

    start = today - timedelta(days=10)

    # navigation

    for txt in [
        "Orders",
        "Spot",
        "Trade History",
    ]:

        clicked = False

        for _ in range(3):

            try:

                page.get_by_text(txt, exact=True).first.click(timeout=10000)

                print(f"[✓] Clicked {txt}")

                clicked = True

                page.wait_for_timeout(3000)

                break

            except:
                continue

        if not clicked:

            save_debug(page, f"nav_fail_{txt}")

            raise RuntimeError(f"Could not click {txt}")

    # export

    export_clicked = False

    for sel in [
        "button:has-text('Export')",
        "div:has-text('Export')",
        "span:has-text('Export')",
    ]:
        try:

            page.locator(sel).first.click(timeout=10000)

            print("[✓] Export clicked")

            export_clicked = True

            break

        except:
            continue

    if not export_clicked:

        save_debug(page, "export_not_found")

        raise RuntimeError("Export button not found")

    page.wait_for_timeout(3000)

    # customize

    customize_clicked = False

    for sel in [
        "button:has-text('Customize')",
        "div:has-text('Customize')",
    ]:
        try:

            page.locator(sel).first.click(timeout=10000)

            print("[✓] Customize clicked")

            customize_clicked = True

            break

        except:
            continue

    if not customize_clicked:

        save_debug(page, "customize_not_found")

        raise RuntimeError("Customize button not found")

    page.wait_for_timeout(3000)

    # final export

    with page.expect_download(timeout=120000) as dl_info:

        final_clicked = False

        for sel in [
            "button:has-text('Export')",
            "button.mui-11wlovc",
        ]:
            try:

                page.locator(sel).last.click(timeout=10000)

                print("[✓] Final export clicked")

                final_clicked = True

                break

            except:
                continue

        if not final_clicked:

            save_debug(page, "final_export_not_found")

            raise RuntimeError("Final export button not found")

    download = dl_info.value

    os.makedirs("downloads", exist_ok=True)

    filename = (
        f"coinsph_trade_history_"
        f"{start.strftime('%Y-%m-%d')}_"
        f"{today.strftime('%Y-%m-%d')}.csv"
    )

    local_path = os.path.join("downloads", filename)

    download.save_as(local_path)

    print(f"[✓] Downloaded: {local_path}")

    return local_path


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def run():

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            slow_mo=50,
            channel="chromium",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-gpu",
                "--window-size=1920,1080",
                "--start-maximized",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Asia/Manila",
            java_script_enabled=True,
            bypass_csp=True,
            accept_downloads=True,
        )

        page = context.new_page()

        page.set_default_timeout(30000)

        page.set_default_navigation_timeout(120000)

        try:

            login(page)

            local_file = export_csv(page)

            s3_key = (
                f"{S3_PREFIX}/"
                f"{os.path.basename(local_file)}"
            )

            upload_to_s3(local_file, s3_key)

            print("\n[✓] COMPLETED SUCCESSFULLY")

            print(f"[✓] S3 Path: s3://{S3_BUCKET}/{s3_key}")

        except Exception as e:

            print(f"\n[x] FLOW FAILED: {e}")

            save_debug(page, "fatal_error")

            raise

        finally:

            browser.close()


# ─────────────────────────────────────────────────────────────
# RETRY
# ─────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":

    run_with_retry()
