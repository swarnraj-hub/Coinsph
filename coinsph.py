import hmac
import hashlib
import base64
import struct
import time
import os
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright
import boto3
from botocore.exceptions import BotoCoreError, ClientError


# ─────────────────────────────────────────────────────────────
# ENV VARIABLES
# ─────────────────────────────────────────────────────────────

EMAIL    = os.getenv("COINSPH_EMAIL")
PASSWORD = os.getenv("COINSPH_PASSWORD")
SECRET   = os.getenv("COINSPH_TOTP_SECRET")

BASE_URL = "https://www.coins.ph/en-ph"

MAX_ATTEMPTS = 3

AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
S3_BUCKET      = os.getenv("S3_BUCKET")
S3_PREFIX      = os.getenv("S3_PREFIX", "coinsph_fx/raw")
AWS_REGION     = os.getenv("AWS_REGION", "ap-southeast-1")


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
    raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")


# ─────────────────────────────────────────────────────────────
# S3 Upload
# ─────────────────────────────────────────────────────────────

def upload_to_s3(local_path, s3_key):
    print(f"[*] Uploading {local_path} -> s3://{S3_BUCKET}/{s3_key}")
    client = boto3.client(
        "s3",
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
        region_name=AWS_REGION,
    )
    client.upload_file(local_path, S3_BUCKET, s3_key)
    print(f"[✓] Uploaded to s3://{S3_BUCKET}/{s3_key}")


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def save_screenshot(page, tag):
    try:
        os.makedirs("artifacts", exist_ok=True)
        fname = f"artifacts/{tag}_{int(time.time())}.png"
        page.screenshot(path=fname, full_page=True)
        print(f"[*] Screenshot saved: {fname}")
    except Exception:
        pass


def get_totp(secret):
    pad = len(secret) % 8
    if pad:
        secret += "=" * (8 - pad)
    key    = base64.b32decode(secret.upper())
    msg    = struct.pack(">Q", int(time.time() // 30))
    h      = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code   = (struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


# ─────────────────────────────────────────────────────────────
# Login
# ─────────────────────────────────────────────────────────────

def login(page):
    print("[*] Opening login page...")
    page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    # ── Email tab ──────────────────────────────────────────────
    for sel in ["button:has-text('Email')", "a:has-text('Email')",
                "[role='tab']:has-text('Email')"]:
        try:
            page.locator(sel).first.click(timeout=3000)
            print("[✓] Email tab selected")
            page.wait_for_timeout(500)
            break
        except Exception:
            continue

    # ── Email field ────────────────────────────────────────────
    email_ok = False
    for sel in ["input[type='email']", "input[name='email']", "input[type='text']"]:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=8000)
            el.fill(EMAIL)
            email_ok = True
            print("[✓] Email entered")
            break
        except Exception:
            continue
    if not email_ok:
        raise RuntimeError("Email field not found")

    # ── Next ───────────────────────────────────────────────────
    for name in ["Next", "Continue", "Login", "Log in"]:
        try:
            page.get_by_role("button", name=name).click(timeout=4000)
            print(f"[✓] Clicked {name}")
            break
        except Exception:
            continue
    page.wait_for_timeout(2000)

    # ── Password ───────────────────────────────────────────────
    pw_ok = False
    for sel in ["input[type='password']", "input[name='password']"]:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=8000)
            el.fill(PASSWORD)
            pw_ok = True
            print("[✓] Password entered")
            break
        except Exception:
            continue
    if not pw_ok:
        raise RuntimeError("Password field not found")

    # ── Login button ───────────────────────────────────────────
    for name in ["Login", "Log in", "Sign in", "Next"]:
        try:
            page.get_by_role("button", name=name).click(timeout=4000)
            print(f"[✓] Clicked {name}")
            break
        except Exception:
            continue
    page.wait_for_timeout(3000)

    # ── OTP ────────────────────────────────────────────────────
    code = get_totp(SECRET)
    print(f"[*] TOTP: {code}")

    # Wait up to 30s for any OTP input to appear
    otp_appeared = False
    for _ in range(15):
        count = page.locator("input[maxlength='1']").count()
        if count >= 6:
            otp_appeared = True
            break
        for sel in ["input[autocomplete='one-time-code']",
                    "input[maxlength='6']",
                    "input[inputmode='numeric']"]:
            try:
                if page.locator(sel).first.is_visible():
                    otp_appeared = True
                    break
            except Exception:
                pass
        if otp_appeared:
            break
        page.wait_for_timeout(2000)

    if not otp_appeared:
        save_screenshot(page, "otp_not_found")
        raise RuntimeError("OTP field not found")

    # Fill OTP
    otp_ok = False
    try:
        inputs = page.locator("input[maxlength='1']")
        if inputs.count() >= 6:
            for i, d in enumerate(code):
                inputs.nth(i).fill(d)
                page.wait_for_timeout(50)
            otp_ok = True
            print("[✓] OTP entered (individual boxes)")
    except Exception:
        pass

    if not otp_ok:
        for sel in ["input[autocomplete='one-time-code']",
                    "input[maxlength='6']",
                    "input[inputmode='numeric']"]:
            try:
                page.locator(sel).first.fill(code)
                otp_ok = True
                print("[✓] OTP entered (single field)")
                break
            except Exception:
                continue

    if not otp_ok:
        raise RuntimeError("Could not fill OTP")

    page.wait_for_timeout(1000)

    # ── Verify button ──────────────────────────────────────────
    for name in ["Verify", "Submit", "Confirm", "Next"]:
        try:
            page.get_by_role("button", name=name).click(timeout=4000)
            print(f"[✓] Clicked {name}")
            break
        except Exception:
            continue

    page.wait_for_timeout(5000)

    if "dashboard" not in page.url.lower():
        save_screenshot(page, "login_failed")
        raise RuntimeError(f"Dashboard not reached: {page.url}")

    print("[✓] Logged in")


# ─────────────────────────────────────────────────────────────
# Navigate
# ─────────────────────────────────────────────────────────────

def navigate_to_trade_history(page):
    print("[*] Navigating to Trade History")

    # Close any popup first
    page.wait_for_timeout(1500)
    for sel in ["button[aria-label='Close']", "button[aria-label='close']",
                "button:has-text('Maybe Later')", "button:has-text('Skip')",
                "button:has-text('Got it')"]:
        try:
            el = page.locator(sel).first
            if el.is_visible():
                el.click()
                page.wait_for_timeout(800)
                break
        except Exception:
            continue

    # Sidebar: Orders → Spot → Trade History tab
    steps = [
        ("Orders",        "sidebar"),
        ("Spot",          "sidebar"),
        ("Trade History", "tab"),
    ]

    for text, kind in steps:
        success = False
        for attempt in range(3):
            try:
                if kind == "sidebar":
                    # Restrict to left sidebar (x < 220)
                    clicked = page.evaluate(f"""
                        (() => {{
                            for (const el of document.querySelectorAll('*')) {{
                                const r = el.getBoundingClientRect();
                                if (r.x > 220 || r.width === 0 || r.height === 0) continue;
                                const direct = [...el.childNodes]
                                    .filter(n => n.nodeType === 3)
                                    .map(n => n.textContent.trim())
                                    .filter(t => t.length > 0).join('');
                                if (direct === '{text}') {{ el.click(); return true; }}
                            }}
                            for (const el of document.querySelectorAll('*')) {{
                                const r = el.getBoundingClientRect();
                                if (r.x > 220 || r.width === 0 || r.height === 0) continue;
                                if (r.height < 60 && (el.innerText||'').trim() === '{text}') {{
                                    el.click(); return true;
                                }}
                            }}
                            return false;
                        }})()
                    """)
                    if not clicked:
                        page.get_by_text(text, exact=True).first.click(timeout=5000)
                else:
                    for sel in [
                        f"button:has-text('{text}')",
                        f"[role='tab']:has-text('{text}')",
                        f"a:has-text('{text}')",
                    ]:
                        try:
                            el = page.locator(sel).first
                            el.wait_for(state="visible", timeout=4000)
                            el.click()
                            break
                        except Exception:
                            continue

                page.wait_for_timeout(2000)
                success = True
                print(f"[✓] Clicked {text}")
                break
            except Exception as e:
                print(f"[!] {text} attempt {attempt+1}/3: {e}")
                page.wait_for_timeout(1500)

        if not success:
            save_screenshot(page, f"nav_failed_{text.replace(' ','_')}")
            raise RuntimeError(f"Could not click: {text}")

    page.wait_for_timeout(2000)

    # Wait for table to load
    try:
        page.locator("table tbody tr").first.wait_for(state="visible", timeout=15000)
        print("[✓] Trade History table loaded")
    except Exception:
        page.wait_for_timeout(3000)
        print("[!] Table wait timed out — continuing")


# ─────────────────────────────────────────────────────────────
# Date picker JS (finds cell coords, Python does the click)
# ─────────────────────────────────────────────────────────────

_FIND_DAY_JS = """
    ([dayNum, ariaLabels]) => {
        for (const label of ariaLabels) {
            const el = document.querySelector('[aria-label="' + label + '"]');
            if (el) {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && !el.hasAttribute('disabled') &&
                    el.getAttribute('aria-disabled') !== 'true')
                    return { x: r.left + r.width/2, y: r.top + r.height/2,
                             via: 'aria:' + label };
            }
        }

        const hdrs = [...document.querySelectorAll('*')].filter(el => {
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            if (r.width > window.innerWidth * 0.4) return false;
            return /^[A-Z][a-z]+ \\d{4}$/.test((el.innerText || '').trim());
        }).sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);

        let midX;
        if (hdrs.length >= 2) {
            const r0 = hdrs[0].getBoundingClientRect();
            const r1 = hdrs[1].getBoundingClientRect();
            midX = (r0.right + r1.left) / 2;
        } else if (hdrs.length === 1) {
            midX = hdrs[0].getBoundingClientRect().right + 80;
        } else {
            midX = window.innerWidth / 2;
        }

        const dayStr = String(dayNum);
        const dayPad = dayStr.padStart(2, '0');
        const SKIP   = ['outside','other','prev','next','gray','grey',
                        'muted','disabled','inactive'];

        const allDayCells = [...document.querySelectorAll(
            '[role="gridcell"], td, button, div, span'
        )].filter(c => {
            const r = c.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            const t = (c.innerText || '').trim();
            return t === dayStr || t === dayPad;
        });

        const leftCells = allDayCells.filter(c => {
            const r = c.getBoundingClientRect();
            if (r.left >= midX) return false;
            if (c.hasAttribute('disabled')) return false;
            if (c.getAttribute('aria-disabled') === 'true') return false;
            const cls = (c.className || '').toString().toLowerCase();
            return !SKIP.some(s => cls.includes(s));
        }).sort((a, b) => {
            const ra = a.getBoundingClientRect();
            const rb = b.getBoundingClientRect();
            return ra.top !== rb.top ? ra.top - rb.top : ra.left - rb.left;
        });

        if (!leftCells.length)
            return { error: true, day: dayNum, midX: midX.toFixed(0),
                     totalCells: allDayCells.length, headers: hdrs.length };

        const r = leftCells[0].getBoundingClientRect();
        return { x: r.left + r.width/2, y: r.top + r.height/2, via: 'grid',
                 pos: '('+r.left.toFixed(0)+','+r.top.toFixed(0)+')',
                 midX: midX.toFixed(0), headers: hdrs.length,
                 matches: leftCells.length };
    }
"""


# ─────────────────────────────────────────────────────────────
# Export CSV
# ─────────────────────────────────────────────────────────────

def export_csv(page):
    print("[*] Starting export flow")

    today = datetime.now()
    start = today - timedelta(days=10)
    sm    = start.strftime("%B")
    tm    = today.strftime("%B")

    # ── Click Export button ────────────────────────────────────
    export_ok = False
    for attempt in range(1, 4):
        try:
            sel = (
                "#__next > main > div > div.MuiBox-root.mui-1r03rwi > "
                "div.MuiBox-root.mui-1wqjpyd > div.MuiBox-root.mui-0 > "
                "div > div > div > div.MuiBox-root.mui-130f8nx > div > div"
            )
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=4000)
            el.click()
            export_ok = True
            print("[✓] Export clicked via exact selector")
            break
        except Exception:
            pass
        try:
            coords = page.evaluate("""
                () => {
                    for (const el of document.querySelectorAll('div, span')) {
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const txt = [...el.childNodes]
                            .filter(n => n.nodeType === Node.TEXT_NODE)
                            .map(n => n.textContent.trim()).join('');
                        if (txt === 'Export')
                            return { x: r.left + r.width/2, y: r.top + r.height/2 };
                    }
                    return null;
                }
            """)
            if coords:
                page.mouse.click(coords['x'], coords['y'])
                export_ok = True
                print("[✓] Export clicked via text search")
                break
        except Exception:
            pass
        print(f"[!] Export click attempt {attempt}/3 failed — retrying")
        page.wait_for_timeout(1000)

    if not export_ok:
        save_screenshot(page, "export_not_found")
        raise RuntimeError("Export button not found")
    page.wait_for_timeout(1500)

    # ── Click Customize ────────────────────────────────────────
    customize_ok = False
    for attempt in range(1, 4):
        try:
            page.wait_for_function(
                """() => [...document.querySelectorAll('*')].some(e =>
                    e.getBoundingClientRect().width > 0 &&
                    (e.innerText || '').trim() === 'Customize'
                )""",
                timeout=6000
            )
            result = page.evaluate("""
                () => {
                    for (const el of document.querySelectorAll('*')) {
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const direct = [...el.childNodes]
                            .filter(n => n.nodeType === Node.TEXT_NODE)
                            .map(n => n.textContent.trim()).join('');
                        if (direct === 'Customize') { el.click(); return true; }
                    }
                    for (const el of document.querySelectorAll('*')) {
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        if ((el.innerText || '').trim() === 'Customize') {
                            el.click(); return true;
                        }
                    }
                    return false;
                }
            """)
            if result:
                customize_ok = True
                print("[✓] Customize clicked")
                break
        except Exception as e:
            print(f"[!] Customize attempt {attempt}/3: {e}")
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
            # Re-open export modal
            try:
                coords = page.evaluate("""
                    () => {
                        for (const el of document.querySelectorAll('div, span')) {
                            const r = el.getBoundingClientRect();
                            if (r.width === 0 || r.height === 0) continue;
                            const t = [...el.childNodes]
                                .filter(n => n.nodeType === Node.TEXT_NODE)
                                .map(n => n.textContent.trim()).join('');
                            if (t === 'Export')
                                return {x: r.left+r.width/2, y: r.top+r.height/2};
                        }
                        return null;
                    }
                """)
                if coords:
                    page.mouse.click(coords['x'], coords['y'])
                    page.wait_for_timeout(1000)
            except Exception:
                pass

    if not customize_ok:
        save_screenshot(page, "customize_not_found")
        raise RuntimeError("Customize button not found")
    page.wait_for_timeout(1500)

    # ── Click calendar icon ────────────────────────────────────
    cal_ok = False
    for attempt in range(1, 4):
        try:
            page.wait_for_function(
                """() => [...document.querySelectorAll('svg')].some(s =>
                    s.getAttribute('viewBox') === '0 0 34 34' &&
                    s.getBoundingClientRect().width > 0
                )""",
                timeout=6000
            )
            coords = page.evaluate("""
                () => {
                    for (const svg of document.querySelectorAll('svg')) {
                        const r = svg.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        if (svg.getAttribute('viewBox') === '0 0 34 34')
                            return { x: r.left + r.width/2, y: r.top + r.height/2 };
                    }
                    return null;
                }
            """)
            if coords:
                page.mouse.click(coords['x'], coords['y'])
                cal_ok = True
                print("[✓] Calendar icon clicked")
                break
        except Exception as e:
            print(f"[!] Calendar attempt {attempt}/3: {e}")
            page.wait_for_timeout(1000)

    if not cal_ok:
        save_screenshot(page, "calendar_not_found")
        raise RuntimeError("Calendar icon not found")
    page.wait_for_timeout(1000)

    # ── Select date range ──────────────────────────────────────
    try:
        page.wait_for_function(
            r"""() => [...document.querySelectorAll('*')].some(c =>
                c.getBoundingClientRect().width > 0 &&
                /^\d{1,2}$/.test((c.innerText || '').trim())
            )""",
            timeout=7000
        )
        print("[✓] Calendar grid ready")
    except Exception:
        page.wait_for_timeout(2000)

    start_labels = [
        f"{sm} {start.day}, {start.year}",
        f"{sm} {start.day:02d}, {start.year}",
        start.strftime("%Y-%m-%d"),
        f"{start.day:02d} {sm} {start.year}",
    ]
    end_labels = [
        f"{tm} {today.day}, {today.year}",
        f"{tm} {today.day:02d}, {today.year}",
        today.strftime("%Y-%m-%d"),
        f"{today.day:02d} {tm} {today.year}",
    ]

    print(f"[*] Selecting {start.strftime('%d-%b-%Y')} -> {today.strftime('%d-%b-%Y')}")

    def click_day(day_num, aria_labels, label):
        for attempt in range(1, 4):
            res = page.evaluate(_FIND_DAY_JS, [day_num, aria_labels])
            if res and not res.get('error') and 'x' in res:
                page.mouse.click(res['x'], res['y'])
                print(f"[✓] {label} day {day_num} — via={res.get('via')} "
                      f"pos={res.get('pos')} midX={res.get('midX')}")
                return True
            print(f"[!] {label} day {day_num} not found attempt {attempt}/3 — "
                  f"midX={res.get('midX') if res else '?'} "
                  f"cells={res.get('totalCells') if res else '?'}")
            page.wait_for_timeout(500)
        return False

    ok1 = click_day(start.day, start_labels, "START")
    page.wait_for_timeout(800)
    ok2 = click_day(today.day, end_labels, "END")
    page.wait_for_timeout(600)

    if not (ok1 and ok2):
        save_screenshot(page, "date_selection_failed")
        raise RuntimeError("Date range selection failed")

    # ── Confirm ────────────────────────────────────────────────
    confirmed = False
    for sel in ["button:has-text('Confirm')", "button:has-text('Apply')",
                "button:has-text('OK')", "button:has-text('Done')"]:
        try:
            el = page.locator(sel).last
            el.wait_for(state="visible", timeout=3000)
            el.click()
            confirmed = True
            print("[✓] Confirm clicked")
            break
        except Exception:
            continue

    if not confirmed:
        try:
            coords = page.evaluate("""
                () => {
                    const words = ['Confirm','Apply','OK','Done'];
                    const btn = [...document.querySelectorAll('button')].find(b =>
                        b.getBoundingClientRect().width > 0 &&
                        !b.hasAttribute('disabled') &&
                        words.some(w => (b.innerText||'').trim() === w)
                    );
                    if (!btn) return null;
                    const r = btn.getBoundingClientRect();
                    return { x: r.left+r.width/2, y: r.top+r.height/2 };
                }
            """)
            if coords:
                page.mouse.click(coords['x'], coords['y'])
                confirmed = True
                print("[✓] Confirm clicked via mouse")
        except Exception:
            pass

    page.wait_for_timeout(1000)

    # ── Final Export (capture download) ───────────────────────
    print("[*] Waiting for file download...")
    os.makedirs("artifacts", exist_ok=True)

    filename = (
        f"coinsph_trade_history_"
        f"{start.strftime('%Y-%m-%d')}_"
        f"{today.strftime('%Y-%m-%d')}.csv"
    )
    local_path = os.path.join("artifacts", filename)

    with page.expect_download(timeout=120000) as dl_info:
        final_ok = False
        for sel in ["button.mui-11wlovc",
                    "button:has-text('Export')"]:
            try:
                el = page.locator(sel).last
                el.wait_for(state="visible", timeout=5000)
                el.click()
                final_ok = True
                print("[✓] Final Export clicked")
                break
            except Exception:
                continue

        if not final_ok:
            try:
                coords = page.evaluate("""
                    () => {
                        const btns = [...document.querySelectorAll('button')].filter(b => {
                            const r = b.getBoundingClientRect();
                            return r.width > 0 && !b.hasAttribute('disabled') &&
                                   (b.innerText||'').trim() === 'Export';
                        });
                        if (!btns.length) return null;
                        const r = btns[btns.length-1].getBoundingClientRect();
                        return { x: r.left+r.width/2, y: r.top+r.height/2 };
                    }
                """)
                if coords:
                    page.mouse.click(coords['x'], coords['y'])
                    final_ok = True
                    print("[✓] Final Export clicked via mouse")
            except Exception:
                pass

        if not final_ok:
            raise RuntimeError("Final Export button not found")

    download = dl_info.value
    download.save_as(local_path)
    print(f"[✓] Downloaded: {local_path} ({os.path.getsize(local_path):,} bytes)")
    return local_path


# ─────────────────────────────────────────────────────────────
# Main Flow
# ─────────────────────────────────────────────────────────────

def run():
    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=False,   # MUST be False — headless=True triggers CAPTCHA on coins.ph
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1920,1080",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Asia/Manila",
        )

        page = context.new_page()

        # Hide all automation signals that trigger bot/CAPTCHA detection
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
            window.chrome = { runtime: {} };
        """)

        try:
            login(page)
            navigate_to_trade_history(page)
            local_file = export_csv(page)
            s3_key = f"{S3_PREFIX}/{os.path.basename(local_file)}"
            upload_to_s3(local_file, s3_key)
            print("\n[✓] COMPLETED SUCCESSFULLY")
            print(f"[✓] S3 Path: s3://{S3_BUCKET}/{s3_key}")
        except Exception as e:
            print(f"\n[x] FLOW FAILED: {e}")
            save_screenshot(page, "fatal_error")
            raise
        finally:
            browser.close()


# ─────────────────────────────────────────────────────────────
# Retry Wrapper
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
# Entry
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_with_retry()
